# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""PREPARED ONLY: fresh-schema PostgreSQL controls for the sole native operator.

No live Bridge/provider is used. HTTP is an in-process SDK transport. This module
is not part of the author's executed host qualification.
"""

import copy
import hashlib
from contextlib import asynccontextmanager
from uuid import UUID, uuid4

import httpx2
import pytest
from test_bridge_completion_callbacks import CAPABILITY
from test_identity import fixture as oidc_fixture
from test_identity import request as identity_request

from firefly_forge.bridge_completion_scope import bridge_completion_scope
from firefly_forge.bridge_completions import INTENT, BridgeCompletions
from firefly_forge.configuration import ProviderConfiguration
from firefly_forge.journal import Journal
from firefly_forge.models import RepositoryInput, RunInput
from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied, ProjectAccess
from firefly_forge.project_models import ProjectDefinition
from firefly_forge.projects import Projects
from firefly_forge.providers import BridgeProvider


@asynccontextmanager
async def prepared(store, tmp_path, *, review=False):
    identity, _, _, _, issue = await oidc_fixture(store)
    token = None
    try:
        actor = await identity.authenticate(
            identity_request(bearer="Bearer " + issue("forge-api", roles=["forge-admin"]))
        )
        assert actor is not None and await ProjectAccess(store).current_administrator(actor)
        token = CURRENT_PRINCIPAL.set(actor)
        repo = await store.add_repository(RepositoryInput(name="Offline recovery", url=str(tmp_path)))
        project = await Projects(store).configure(
            ProjectDefinition(
                name="Recovery",
                description="Native evidence only",
                topology="monorepo",
                owners=["Owner"],
                components=[dict(id="api", name="API", repository_id=repo["id"], responsibility="Retain evidence")],
            )
        )
        store.configuration.update(
            "bridge",
            ProviderConfiguration(
                values={
                    "endpoint": "http://localhost:19191",
                    "token": "fictional-native-token",
                    "completion_recovery": "required",
                }
            ),
        )
        run = await store.create_run(
            RunInput(
                repository_id=repo["id"],
                project_id=project["id"],
                component_id="api",
                agent="bridge",
                brief="Native recovery control",
                assurance={"review_agent": "bridge" if review else None},
            )
        )
        journal = Journal(store)
        await journal.ensure_plan(run["id"])
        intent = await journal.intent(run["id"], "review" if review else "implement")
        if review:
            await store.update(
                run["id"], review_config_revision=store.configuration.snapshot("bridge")["config_revision"]
            )
        await store.acquire_ownership()
        run = await store.get_run(run["id"])
        service = BridgeCompletions(store, journal)
        body = b'{"model":"bridge/default","messages":[{"role":"user","content":"Fictional native control"}]}'
        descriptor = {
            "request_id": "fbr1_" + uuid4().hex,
            "principal_id": "fixture-principal",
            "model": "bridge/default",
            "endpoint": "http://localhost:19191/v1",
            "wire_sha256": hashlib.sha256(body).hexdigest(),
            "wire_bytes": len(body),
            "image_revision": None,
        }
        yield (identity, actor, run, journal, service, service.scope(run, intent, review=review), descriptor, body)
    finally:
        try:
            await identity.client.aclose()
        finally:
            if token is not None:
                CURRENT_PRINCIPAL.reset(token)


@pytest.mark.parametrize("review", [False, True], ids=["implementation", "independent-review"])
async def test_native_intent_exact_replay_binds_task_and_frozen_configuration(store, tmp_path, review):
    async with prepared(store, tmp_path, review=review) as (
        identity,
        actor,
        run,
        journal,
        service,
        scope,
        descriptor,
        body,
    ):
        first = await scope.admit(descriptor, body)
        assert await scope.admit(descriptor, body) == first
        assert first["task_id"] == ("review" if review else "implement")
        field = "review_config_revision" if review else "config_revision"
        assert first["payload"]["config_revision"] == run[field]
        assert first["payload"]["task_key"] != descriptor["request_id"]
        changed = body + b" "
        with pytest.raises(ValueError, match="another request"):
            await scope.admit(
                {**descriptor, "wire_bytes": len(changed), "wire_sha256": hashlib.sha256(changed).hexdigest()}, changed
            )
        entries = await journal.entries(run["id"])
        assert len([row for row in entries if row["kind"] == INTENT]) == 1
        assert Journal.verify_chain(entries)


async def test_native_append_failure_rolls_back_completion_and_hold_together(store, tmp_path, monkeypatch):
    async with prepared(store, tmp_path) as (identity, _, run, journal, _, scope, descriptor, body):
        before = await journal.entries(run["id"])
        save = journal.records.save

        async def failing(row):
            await save(row)
            if row.kind == INTENT:
                raise ValueError("Owned rollback control")

        monkeypatch.setattr(journal.records, "save", failing)
        with pytest.raises(ValueError, match="rollback"):
            await scope.admit(descriptor, body)
        assert await journal.entries(run["id"]) == before
        assert "bridge_completion_hold" not in await store.get_run(run["id"])


async def test_native_reservation_refuses_before_any_body_eviction_or_new_identity(store, tmp_path):
    async with prepared(store, tmp_path) as (identity, _, run, journal, _, scope, descriptor, body):
        admitted = []
        for _ in range(100):
            candidate = {**descriptor, "request_id": "fbr1_" + uuid4().hex}
            try:
                admitted.append(await scope.admit(candidate, body))
            except ValueError as exc:
                assert "retention is full" in str(exc)
                break
        assert 0 < len(admitted) < 100
        entries = await journal.entries(run["id"])
        assert [entry for entry in entries if entry["kind"] == INTENT] == admitted
        assert Journal.verify_chain(entries)


async def test_native_ambient_transaction_refuses_before_capability_or_post(store, tmp_path):
    from test_bridge_completion_callbacks import call

    async with prepared(store, tmp_path) as (identity, _, run, journal, _, scope, descriptor, body):
        model = BridgeProvider(store.configuration.load_snapshot(run["config_revision"])).model()
        calls = []

        def respond(request):
            calls.append(request.method)
            return httpx2.Response(200, json={"completionRecovery": CAPABILITY})

        model.client._client._transport = httpx2.MockTransport(respond)
        model.client._client._mounts = {}
        try:
            async with store._unit():
                with bridge_completion_scope(scope), pytest.raises(ValueError, match="active caller transaction"):
                    await call(model)
            assert calls == []
            assert not any(entry["kind"] == INTENT for entry in await journal.entries(run["id"]))
        finally:
            await model.client.close()


@pytest.mark.parametrize("loss", ["disabled", "credential", "context"])
async def test_native_exact_read_rechecks_fresh_identity_and_credential_context(store, tmp_path, monkeypatch, loss):
    from pyfly.data.relational.sqlalchemy import LockMode

    from firefly_forge.identity_persistence import ForgeIdentityUserRepository

    async with prepared(store, tmp_path) as (identity, actor, run, journal, service, scope, descriptor, body):
        first = await scope.admit(descriptor, body)
        admit = store.admit_credentials

        async def late(value):
            await admit(value)
            if loss == "disabled":
                async with store._unit():
                    row = await ForgeIdentityUserRepository().find_by_id(actor.id, lock=LockMode.PESSIMISTIC_WRITE)
                    row.enabled = False
                    await ForgeIdentityUserRepository().save(row)
            elif loss == "context":
                CURRENT_PRINCIPAL.set(None)
            else:
                store.credential_registry = lambda: {"registered_late_token": descriptor["principal_id"]}

        monkeypatch.setattr(store, "admit_credentials", late)
        context = CURRENT_PRINCIPAL.set(actor)
        try:
            with pytest.raises((ValueError, AccessDenied)):
                await service.exact(run["id"], descriptor["request_id"])
            async with store._unit():
                row = await journal.records.bridge_record(UUID(run["id"]), descriptor["request_id"], INTENT)
            assert row.data == first
        finally:
            CURRENT_PRINCIPAL.reset(context)


@pytest.mark.parametrize("status", ["cancelled", "interrupted"])
async def test_native_known_admission_refusal_is_not_quota_and_cannot_restart_graph(store, tmp_path, status):
    from firefly_forge.controllers import RunService

    async with prepared(store, tmp_path) as (identity, _, run, journal, _, scope, descriptor, body):
        first = await scope.admit(descriptor, body)
        observation = {
            "completion_request_id": descriptor["request_id"],
            "intent_digest": first["digest"],
            "principal_id": descriptor["principal_id"],
            "admission": "admission_refused",
            "code": "completion_recovery_full",
        }
        error = await journal.fail(
            run["id"],
            first["payload"]["task_key"],
            "bridge_admission_refused",
            "Explicit no-admission",
            observation=observation,
        )
        assert error["payload"]["automatic"] is False and error["payload"]["retry_at"] is None
        assert "job_id" not in observation and "retry_after" not in observation
        hold = copy.deepcopy((await store.get_run(run["id"]))["bridge_completion_hold"])
        await store.finish(run["id"], status)
        for agent in (None, "claude"):
            with pytest.raises(ValueError, match="graph checkpoint"):
                await journal.resume(run["id"], agent)
        with pytest.raises(ValueError, match="graph checkpoint"):
            await RunService(store, None).retry(run["id"])
        assert (await store.get_run(run["id"]))["bridge_completion_hold"] == hold


async def test_native_sdk_transport_sees_committed_original_intent_before_one_post(store, tmp_path):
    from test_bridge_completion_callbacks import call

    from firefly_forge.providers import ProviderOutcomeUnknown

    async with prepared(store, tmp_path) as (identity, _, run, journal, _, scope, _, _):
        model = BridgeProvider(store.configuration.load_snapshot(run["config_revision"])).model()
        requests = []

        async def respond(request):
            if request.method == "GET":
                return httpx2.Response(200, json={"completionRecovery": CAPABILITY})
            requests.append(request)
            async with store._unit():
                retained = await journal.records.bridge_record(
                    UUID(run["id"]), request.headers["idempotency-key"], INTENT
                )
                assert retained is not None
                assert (
                    retained.data["payload"]["descriptor"]["wire_sha256"] == hashlib.sha256(request.content).hexdigest()
                )
            return httpx2.Response(503, json={"error": {"code": "unavailable", "message": "Unknown effect"}})

        model.client._client._transport = httpx2.MockTransport(respond)
        model.client._client._mounts = {}
        try:
            with bridge_completion_scope(scope), pytest.raises(ProviderOutcomeUnknown):
                await call(model)
            assert len(requests) == 1
            assert (await store.get_run(run["id"]))["bridge_completion_hold"]["request_id"] == requests[0].headers[
                "idempotency-key"
            ]
        finally:
            await model.client.close()


def test_native_role_api_registers_bounded_list_and_exact_read_routes(tmp_path, postgres_schema):
    from starlette.testclient import TestClient

    from firefly_forge.api import create_application
    from firefly_forge.store import Store

    token = "owned-completion-recovery-api-bootstrap-fixture"
    with TestClient(
        create_application(tmp_path / "api", token, role="api"),
        base_url="http://testserver",
        headers={"Authorization": "Bearer " + token, "Origin": "http://testserver"},
        client=("127.0.0.1", 52551),
    ) as http:
        assert http.post("/api/v1/auth/login", json={"token": token}).status_code == 200

        async def seed_worker():
            worker = Store(http.app.state.store.path)
            await worker.initialize()
            try:
                async with prepared(worker, tmp_path) as (identity, _, run, _, _, scope, descriptor, body):
                    receipt = await scope.admit(descriptor, body)
                    return (run["id"], descriptor["request_id"], receipt)
            finally:
                await worker.close()

        run_id, request_id, receipt = http.portal.call(seed_worker)
        exact = http.get(f"/api/v1/runs/{run_id}/bridge-requests/{request_id}")
        assert exact.status_code == 200 and exact.json() == receipt
        page = http.get(f"/api/v1/runs/{run_id}/bridge-requests")
        assert page.status_code == 200 and page.json() == {"version": 1, "items": [receipt], "next_after": None}
        missing = http.get(f"/api/v1/runs/{run_id}/bridge-requests/fbr1_{'e' * 32}")
        assert missing.status_code == 404
