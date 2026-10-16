# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Prepared PostgreSQL controls; no Engine, provider, subprocess, or listener."""

import pytest
from test_identity import fixture as oidc_fixture
from test_identity import request as identity_request

from firefly_forge.agent_roles import WorkerFeedback, WorkerRequest, WorkerResponse
from firefly_forge.journal import Journal
from firefly_forge.models import RepositoryInput, RunInput
from firefly_forge.project_access import CURRENT_PRINCIPAL, ProjectAccess
from firefly_forge.project_models import ProjectDefinition
from firefly_forge.projects import Projects


async def prepared(store, tmp_path):
    identity, _, _, _, issue = await oidc_fixture(store)
    actor = await identity.authenticate(identity_request(bearer="Bearer " + issue("forge-api", roles=["forge-user"])))
    assert actor is not None and actor.authentication_method == "oidc_bearer"
    repository = await store.add_repository(RepositoryInput(name="Message source", url=str(tmp_path)))
    project = await Projects(store).configure(
        ProjectDefinition(
            name="Message identity bounds",
            description="Fictional storage and replay checks",
            topology="monorepo",
            owners=["Owner"],
            components=[dict(id="api", name="API", repository_id=repository["id"], responsibility="Verify messages")],
        )
    )
    await ProjectAccess(store).set_membership(project["id"], actor, "maintainer")
    run = await store.create_run(
        RunInput(
            repository_id=repository["id"],
            project_id=project["id"],
            component_id="api",
            operation="test",
            frontend="none",
            brief="Verify bounded worker message identifiers without execution",
        )
    )
    journal = Journal(store)
    await journal.ensure_plan(run["id"])
    return run, journal, actor, identity


def feedback(identifier):
    return WorkerFeedback(
        id=identifier,
        role_id="qa",
        task_id="verify",
        attempt=1,
        disposition="clarification",
        summary="Keep the fictional result?",
        request=WorkerRequest(
            id="q" * 80, kind="verification", question="Keep the result?", context="Owned native storage control"
        ),
    )


async def test_native_long_feedback_replay_preserves_paused_chain(store, tmp_path):
    run, journal, _, identity = await prepared(store, tmp_path)
    value = feedback("live-poll-feedback-" + "a" * 32)
    try:
        first = await journal.worker_feedback(run["id"], value)
        assert first["idempotency_key"] == "c858fb8af9e01f0469c250951623b9fa2061061d99ef5b77b71fff09141eac99"
        assert first["payload"]["id"] == value.id and first["payload"]["request"]["id"] == "q" * 80
        entries = await journal.entries(run["id"])
        assert await journal.worker_feedback(run["id"], value) == first
        with pytest.raises(ValueError, match="different content"):
            await journal.worker_feedback(run["id"], value.model_copy(update={"summary": "Changed decision"}))
        assert await journal.entries(run["id"]) == entries and Journal.verify_chain(entries)
        assert len([entry for entry in entries if entry["kind"] == "worker_feedback"]) == 1
        current = await store.get_run(run["id"])
        assert current["status"] == "paused" and current["worker_request"]["status"] == "pending"
        assert current["execution_actor"] == run["execution_actor"]
    finally:
        await identity.client.aclose()


async def test_native_maximum_response_replay_preserves_id_actor_and_paused_chain(store, tmp_path):
    run, journal, actor, identity = await prepared(store, tmp_path)
    token = CURRENT_PRINCIPAL.set(actor)
    try:
        question = await journal.worker_feedback(run["id"], feedback("f" * 80))
        answer = WorkerResponse(id="f" * 80, actor="Caller label", answer="Keep the fictional result")
        first = await journal.worker_response(run["id"], "q" * 80, answer)
        assert question["idempotency_key"] == "f7ded868c08f25454cbd49abcc8024050cfc3d5c115b25c351b52334ac60533e"
        assert first["idempotency_key"] == "e99c469aedeaa9f0c9b7f472bfa8fd83d0068f61e97500197e25a0ae165baf85"
        assert first["payload"]["id"] == answer.id and first["payload"]["request_id"] == "q" * 80
        assert first["payload"]["responding_actor"]["user_id"] == actor.id.hex
        entries = await journal.entries(run["id"])
        assert await journal.worker_response(run["id"], "q" * 80, answer) == first
        with pytest.raises(ValueError, match="different content"):
            await journal.worker_response(run["id"], "q" * 80, answer.model_copy(update={"answer": "Changed decision"}))
        assert await journal.entries(run["id"]) == entries and Journal.verify_chain(entries)
        assert len([entry for entry in entries if entry["kind"] == "worker_response"]) == 1
        assert not any(entry["kind"] == "resume" for entry in entries)
        current = await store.get_run(run["id"])
        assert current["status"] == "paused" and current["worker_request"]["status"] == "answered"
        assert current["execution_actor"] == run["execution_actor"]
    finally:
        CURRENT_PRINCIPAL.reset(token)
        await identity.client.aclose()
