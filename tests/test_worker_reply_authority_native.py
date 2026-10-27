# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Actual identity/ACL/journal controls; no worker, model, subprocess or listener."""

from __future__ import annotations

import asyncio
import contextvars
from contextlib import suppress
from uuid import UUID

import pytest
from sqlalchemy import text
from test_identity import fixture as oidc_fixture
from test_identity import request as identity_request

from firefly_forge.agent_roles import WorkerFeedback, WorkerRequest, WorkerResponse
from firefly_forge.identity_persistence import ForgeIdentityUserRepository
from firefly_forge.journal import Journal
from firefly_forge.models import RepositoryInput, RunInput
from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied, ProjectAccess
from firefly_forge.project_models import ProjectDefinition
from firefly_forge.projects import Projects


async def pending(store, tmp_path, *, publish=False):
    identity, _, _, _, token = await oidc_fixture(store)
    actor = await identity.authenticate(identity_request(bearer="Bearer " + token("forge-api", roles=["forge-user"])))
    assert actor is not None and actor.issuer == identity.config.issuer and actor.subject == "user-1"
    assert not ProjectAccess.is_admin(actor)
    repo = await store.add_repository(RepositoryInput(name="Question source", url=str(tmp_path)))
    project = await Projects(store).configure(
        ProjectDefinition(
            name="Owner answers",
            description="Bounded operator clarification",
            topology="monorepo",
            owners=["Owner"],
            components=[dict(id="api", name="API", repository_id=repo["id"], responsibility="Serve requests")],
        )
    )
    await ProjectAccess(store).set_membership(project["id"], actor, "maintainer")
    run = await store.create_run(
        RunInput(
            repository_id=repo["id"],
            project_id=project["id"],
            component_id="api",
            operation="build",
            brief="Build alert routing after owner clarification",
            publish=publish,
        )
    )
    journal = Journal(store)
    await journal.ensure_plan(run["id"])
    await journal.worker_feedback(
        run["id"],
        WorkerFeedback(
            id="feedback-1",
            role_id="implementer",
            task_id="implement",
            attempt=1,
            disposition="clarification",
            summary="Owner decision required",
            request=WorkerRequest(
                id="question-1",
                kind="product_definition",
                question="Which owner receives alerts?",
                context="The source leaves the owner unspecified.",
            ),
        ),
    )
    return run, journal, actor, identity, token


def answer():
    return WorkerResponse(id="answer-1", actor="untrusted caller label", answer="The support owner receives alerts")


async def test_signed_actor_provenance_exact_replay_and_distinct_actor_refusal(store, tmp_path):
    run, journal, actor, identity, issue = await pending(store, tmp_path)
    context = CURRENT_PRINCIPAL.set(actor)
    try:
        first = await journal.worker_response(run["id"], "question-1", answer())
        assert first["payload"]["responding_actor"] == {
            "version": 1,
            "user_id": actor.id.hex,
            "authentication_method": "oidc_bearer",
        }
        assert first["payload"]["actor"] == answer().actor
        assert await journal.worker_response(run["id"], "question-1", answer()) == first
        other = await identity.authenticate(
            identity_request(bearer="Bearer " + issue("forge-api", sub="user-2", roles=["forge-user"]))
        )
        assert other is not None and other.id != actor.id
        await ProjectAccess(store).set_membership(run["project_id"], other, "maintainer")
        CURRENT_PRINCIPAL.set(other)
        with pytest.raises(AccessDenied, match="another authenticated actor"):
            await journal.worker_response(run["id"], "question-1", answer())
        assert (await journal.worker_messages(run["id"]))[-1] == first
        assert (await store.get_run(run["id"]))["status"] == "paused"
        assert Journal.verify_chain(await journal.entries(run["id"]))
    finally:
        CURRENT_PRINCIPAL.reset(context)
        await identity.client.aclose()


@pytest.mark.parametrize("lost", ["execute", "release", "disabled", "context", "cancel"])
async def test_actual_late_writer_admission_loss_has_no_event(store, tmp_path, monkeypatch, lost):
    run, journal, actor, identity, _ = await pending(store, tmp_path, publish=True)
    context = CURRENT_PRINCIPAL.set(actor)
    admit = store.admit_credentials

    async def lose(value):
        await admit(value)
        if lost in {"execute", "release"}:
            await ProjectAccess(store).set_membership(
                run["project_id"], actor, "viewer" if lost == "execute" else "developer"
            )
        elif lost == "disabled":
            async with store._unit():
                row = await ForgeIdentityUserRepository().find_by_id(actor.id)
                row.enabled = False
                await ForgeIdentityUserRepository().save(row)
        elif lost == "context":
            CURRENT_PRINCIPAL.set(None)
        else:
            await store.update(run["id"], cancel_request={"status": "requested"})

    monkeypatch.setattr(store, "admit_credentials", lose)
    try:
        with pytest.raises((AccessDenied, ValueError)):
            await journal.worker_response(run["id"], "question-1", answer())
        async with store._unit():
            assert not [
                row for row in await journal.records.find_for_run(UUID(run["id"])) if row.kind == "worker_response"
            ]
        assert (await store.get_run(run["id"]))["worker_request"]["status"] == "pending"
    finally:
        CURRENT_PRINCIPAL.reset(context)
        await identity.client.aclose()


async def test_same_id_legacy_event_is_authorized_without_backfill(store, tmp_path):
    run, journal, actor, identity, _ = await pending(store, tmp_path)
    async with store._unit():
        original = await journal._append(
            run["id"],
            "implement",
            "worker-response:answer-1",
            "worker_response",
            {**answer().model_dump(), "request_id": "question-1"},
        )
        await store.update(run["id"], worker_request={"id": "question-1", "status": "answered", "task_id": "implement"})
    context = CURRENT_PRINCIPAL.set(actor)
    try:
        assert await journal.worker_response(run["id"], "question-1", answer()) == original
        assert "responding_actor" not in (await journal.entries(run["id"]))[-1]["payload"]
    finally:
        CURRENT_PRINCIPAL.reset(context)
        await identity.client.aclose()


async def test_writer_serializes_actual_membership_revoke_until_commit(store, store_factory, tmp_path, monkeypatch):
    run, journal, actor, identity, _ = await pending(store, tmp_path)
    other = await store_factory(tmp_path / "second" / "state.db")
    reached = asyncio.Event()
    revocation = None
    pid = None
    append = journal._append

    async def revoke():
        nonlocal pid
        async with other._unit() as session:
            pid = await session.scalar(text("SELECT pg_backend_pid()"))
            reached.set()
            await ProjectAccess(other).set_membership(run["project_id"], actor, None)

    async def observe_lock(*args):
        nonlocal revocation
        revocation = asyncio.create_task(revoke(), context=contextvars.Context())
        await asyncio.wait_for(reached.wait(), 3)
        async with store._unit() as session:
            waiting = None
            for _ in range(100):
                waiting = await session.scalar(
                    text("SELECT wait_event_type FROM pg_stat_activity WHERE pid=:pid"), {"pid": pid}
                )
                if waiting == "Lock" or revocation.done():
                    break
                await asyncio.sleep(0.01)
            assert waiting == "Lock" and not revocation.done()
        return await append(*args)

    monkeypatch.setattr(journal, "_append", observe_lock)
    context = CURRENT_PRINCIPAL.set(actor)
    try:
        # Once commit releases locks, revocation can win the separate permission-to-return check.
        with suppress(AccessDenied):
            await journal.worker_response(run["id"], "question-1", answer())
        await asyncio.wait_for(revocation, 3)
        CURRENT_PRINCIPAL.set(None)
        entries = await journal.entries(run["id"])
        assert len([event for event in entries if event["kind"] == "worker_response"]) == 1
        assert Journal.verify_chain(entries)
        CURRENT_PRINCIPAL.set(actor)
        with pytest.raises(AccessDenied):
            await journal.worker_messages(run["id"])
    finally:
        CURRENT_PRINCIPAL.reset(context)
        if revocation is not None and not revocation.done():
            revocation.cancel()
            await asyncio.gather(revocation, return_exceptions=True)
        await identity.client.aclose()


@pytest.mark.parametrize("surface", ["entries", "worker_messages"])
@pytest.mark.parametrize("loss", ["read", "credential"])
async def test_both_message_read_surfaces_recheck_after_admission(store, tmp_path, monkeypatch, surface, loss):
    run, journal, actor, identity, _ = await pending(store, tmp_path)
    context = CURRENT_PRINCIPAL.set(actor)
    try:
        await journal.worker_response(run["id"], "question-1", answer())
        before = await journal.entries(run["id"])
        admit = store.admit_credentials

        async def lose(value):
            await admit(value)
            if loss == "read":
                await ProjectAccess(store).set_membership(run["project_id"], actor, None)
            else:
                store.credential_registry = lambda: {"FIXTURE_API_TOKEN": answer().answer}

        monkeypatch.setattr(store, "admit_credentials", lose)
        with pytest.raises((AccessDenied, ValueError)):
            await getattr(journal, surface)(run["id"])
        # Verify retained bytes directly; no authenticated read or re-digest bypass is asserted.
        async with store._unit():
            rows = await journal.records.find_for_run(UUID(run["id"]))
        assert [row.data for row in rows] == before
    finally:
        CURRENT_PRINCIPAL.reset(context)
        await identity.client.aclose()


@pytest.mark.parametrize("loss", ["credential", "disabled"])
def test_actual_api_generic_journal_get_rechecks_admission(tmp_path, postgres_schema, loss):
    from native_client_knowledge_fixtures import activate_native_client_target
    from starlette.testclient import TestClient

    from firefly_forge.api import create_application
    from firefly_forge.identity import IdentityService

    token = "owned-a05-http-fixture-bootstrap-token"
    with TestClient(
        create_application(tmp_path / "api", token, role="api"),
        base_url="http://testserver",
        headers={"Authorization": "Bearer " + token, "Origin": "http://testserver"},
        client=("127.0.0.1", 52550),
    ) as http:
        assert http.post("/api/v1/auth/login", json={"token": token}).status_code == 200
        store = http.app.state.store
        http.portal.call(activate_native_client_target, store, tmp_path / "framework-prerequisite")
        run, journal, _, identity, _ = http.portal.call(pending, store, tmp_path)
        try:
            endpoint = f"/api/v1/runs/{run['id']}/worker-requests/question-1/respond"
            posted = http.post(endpoint, json=answer().model_dump())
            assert posted.status_code == 200
            retained = posted.json()
            assert retained["payload"]["responding_actor"]["authentication_method"] == "local_bootstrap"
            assert http.post(endpoint, json=answer().model_dump()).json() == retained
            reader = http.portal.call(
                http.app.state.runtime.context.get_bean(IdentityService).authenticate,
                identity_request(bearer="Bearer " + token),
            )
            assert reader is not None and reader.authentication_method == "local_bootstrap"

            async def lose(value):
                if loss == "credential":
                    store.credential_registry = lambda: {"FIXTURE_API_TOKEN": answer().answer}
                else:
                    async with store._unit():
                        row = await ForgeIdentityUserRepository().find_by_id(reader.id)
                        row.enabled = False
                        await ForgeIdentityUserRepository().save(row)

            store.vault_credential_admission = lose
            result = http.get(f"/api/v1/runs/{run['id']}/journal")
            assert result.status_code == (409 if loss == "credential" else 403)
            assert answer().answer not in result.text

            async def raw():
                async with store._unit():
                    return [
                        row.data
                        for row in await journal.records.find_for_run(UUID(run["id"]))
                        if row.kind == "worker_response"
                    ]

            assert http.portal.call(raw) == [retained]
        finally:
            store.vault_credential_admission = None
            http.portal.call(identity.client.aclose)


async def test_credential_registered_after_append_rolls_back_event_and_projection(store, tmp_path, monkeypatch):
    run, journal, actor, identity, _ = await pending(store, tmp_path)
    context = CURRENT_PRINCIPAL.set(actor)
    before = await journal.entries(run["id"])
    append = journal._append

    async def register(*args):
        event = await append(*args)
        store.credential_registry = lambda: {"FIXTURE_API_TOKEN": answer().answer}
        return event

    monkeypatch.setattr(journal, "_append", register)
    try:
        with pytest.raises(ValueError, match="credential"):
            await journal.worker_response(run["id"], "question-1", answer())
        assert (await store.get_run(run["id"]))["worker_request"]["status"] == "pending"
        store.credential_registry = dict
        assert await journal.entries(run["id"]) == before
    finally:
        CURRENT_PRINCIPAL.reset(context)
        await identity.client.aclose()


async def test_postcommit_execute_loss_refuses_return_but_keeps_exact_authorized_read(store, tmp_path, monkeypatch):
    run, journal, actor, identity, _ = await pending(store, tmp_path)
    context = CURRENT_PRINCIPAL.set(actor)
    admit = store.admit_credentials

    async def demote(value):
        await admit(value)
        if isinstance(value, dict) and value.get("kind") == "worker_response":
            await ProjectAccess(store).set_membership(run["project_id"], actor, "viewer")

    monkeypatch.setattr(store, "admit_credentials", demote)
    try:
        with pytest.raises(AccessDenied):
            await journal.worker_response(run["id"], "question-1", answer())
        retained = [entry for entry in await journal.worker_messages(run["id"]) if entry["kind"] == "worker_response"]
        assert len(retained) == 1 and retained[0]["payload"]["answer"] == answer().answer
        assert (await store.get_run(run["id"]))["worker_request"]["status"] == "answered"
    finally:
        CURRENT_PRINCIPAL.reset(context)
        await identity.client.aclose()


async def test_worker_read_suspends_and_restores_actual_caller_unit(store, tmp_path):
    from pyfly.data.transaction import current_unit_of_work

    run, journal, actor, identity, _ = await pending(store, tmp_path)
    context = CURRENT_PRINCIPAL.set(actor)
    seen = []

    async def admit(value):
        seen.append(current_unit_of_work())

    store.vault_credential_admission = admit
    try:
        async with store._unit():
            caller = current_unit_of_work()
            await journal._run(run["id"])
            entries = await journal.worker_messages(run["id"])
            assert seen == [None] and current_unit_of_work() is caller
            assert len(entries) == 1 and entries[0]["kind"] == "worker_feedback"
            with pytest.raises(ValueError, match="caller transaction"):
                await journal.worker_response(run["id"], "question-1", answer())
    finally:
        store.vault_credential_admission = None
        CURRENT_PRINCIPAL.reset(context)
        await identity.client.aclose()
