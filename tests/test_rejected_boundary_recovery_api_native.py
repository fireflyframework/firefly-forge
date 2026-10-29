# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""PREPARED, UNRUN: signed OIDC and native custody for claimed-refusal recovery.

Only the root database operator may execute this case in a fresh _test schema.
The inherited producer uses public MockTransport and a deterministic sandbox.
Qualification seams are fixture-only; this case does not qualify an actual run.
"""

import copy
import time
from functools import partial
from uuid import uuid4

import pytest
import test_identity_write_clients
import test_invalid_read_continuity_native as historical
from provider_rejection_native_fixtures import rejection_input, reviewed_rejection, seed_claimed_refusal
from pydantic_ai import models

from firefly_forge import invalid_read_continuities as native
from firefly_forge.agentic_checkpoint_invocation import runtime_contract
from firefly_forge.engine import Engine
from firefly_forge.execution_requests import ExecutionRequests
from firefly_forge.identity_persistence import ForgeIdentityUserRepository
from firefly_forge.invalid_read_continuity_controllers import (
    ContinuityLeasePolicyController,
    InvalidReadContinuityController,
)
from firefly_forge.invalid_read_lease import ContinuityLeasePolicies
from firefly_forge.journal import Journal
from firefly_forge.provider_rejection_recoveries import REJECTION_ADMITTED, REJECTION_RECOVERY
from firefly_forge.runtime_roles import RuntimeRoles
from firefly_forge.store import Store, identifier

native_identity_writes = test_identity_write_clients.native_identity_writes


@models.override_allow_model_requests(True)
def test_native_claimed_refusal_api_requires_original_current_admin_and_never_dispatches(
    native_identity_writes, tmp_path, monkeypatch
):
    http, _, _, issue, provider_calls, _, _ = native_identity_writes
    context = http.app.state.runtime.context
    store = context.get_bean(Store)
    engine = context.get_bean(Engine)
    journal = context.get_bean(Journal)
    requests = context.get_bean(ExecutionRequests)
    service = context.get_bean(native.InvalidReadContinuities)
    policies = context.get_bean(ContinuityLeasePolicies)
    assert store is http.app.state.store
    assert service.store is policies.store is requests.store is engine.store is journal.store is store
    assert service.engine is engine and service.journal is journal and service.requests is requests
    assert service.rejections.store is store and service.rejections.requests is requests
    assert requests.invalid_read_continuities is service and store.execution_requests is requests
    assert context.get_bean(InvalidReadContinuityController).continuities is service
    assert context.get_bean(ContinuityLeasePolicyController).policies is policies
    assert requests.runtime is context.get_bean(RuntimeRoles)
    assert requests.runtime.role == "api" and not requests.runtime.executes_jobs

    async def seed_worker():
        worker = Store(store.path)
        await worker.initialize()
        patch = pytest.MonkeyPatch()

        def worker_engine(worker_store, ignored_workspace, *args, **kwargs):
            return Engine(worker_store, engine.workspaces, *args, **kwargs)

        def worker_runtime(worker_store):
            assert worker_store.path == store.path
            return RuntimeRoles(worker_store, role="worker")

        patch.setattr(historical, "Engine", worker_engine)
        patch.setattr(historical, "RuntimeRoles", worker_runtime)
        source = historical.native_continuity.__wrapped__(worker, tmp_path, patch)
        try:
            case = await anext(source)
            failed = await seed_claimed_refusal(case)
            evidence = await reviewed_rejection(case, failed)
            run = copy.deepcopy(await worker.get_run(case.run_id))
            return case.run_id, failed, evidence, run, case.posts, case.command_calls
        finally:
            try:
                await source.aclose()
            finally:
                patch.undo()
                await worker.close()

    assert http.portal is not None
    run_id, failed, evidence, original_run, posts, commands = http.portal.call(seed_worker)
    assert failed["status"] == "needs_reconciliation" and failed["generation"] and failed["owner_pid"]
    assert len(posts) == 2 and len(commands) == 11
    monkeypatch.setattr(native, "LEGACY_RUNTIME_CONTRACT", runtime_contract())
    monkeypatch.setattr(
        native,
        "current_profile_seal",
        lambda: {"profile_id": "CONTROLLED-API-FIXTURE-NOT-QUALIFIED", "digest": "8" * 64},
    )
    dispatches = []

    async def forbidden_dispatch(*args, **kwargs):
        dispatches.append((args, kwargs))
        raise AssertionError("The API role must not reserve or execute a worker")

    monkeypatch.setattr(engine, "reserve_agent_restore", forbidden_dispatch)
    monkeypatch.setattr(engine, "execute_run", forbidden_dispatch)
    request_id = failed["id"]
    path = f"runs/{run_id}/invalid-read-continuity/requests/{request_id}"
    candidate_path = path + "/rejected-boundary-candidate"
    recovery_path = path + "/rejected-boundary-recoveries/" + uuid4().hex
    read_path = path + "/rejected-boundary-recovery"
    issued_at = int(time.time())

    def original_authorization(roles=None):
        nonlocal issued_at
        issued_at += 1
        return "Bearer " + issue("forge-api", roles=roles, iat=issued_at)

    admin = original_authorization()
    http.headers["Authorization"] = admin
    session = http.get("auth/session")
    assert session.status_code == 200, session.text
    original_actor_id = session.json()["user"]["id"]
    assert original_actor_id == failed["request"]["actor"]["user_id"]
    configured = http.put(
        "invalid-read-continuity/policy",
        json={"version": 1, "enabled": True, "allow_pre_admission_recovery": True, "max_window_seconds": 180},
    )
    assert configured.status_code == 200, configured.text
    before = http.get(read_path)
    assert before.status_code == 200 and before.json()["status"] == "not_recovered"
    assert before.json()["recovery_id"] is None and before.json()["recovery_record_digest"] is None
    candidate = http.post(candidate_path, json=evidence.model_dump())
    assert candidate.status_code == 200, candidate.text
    assert candidate.json()["admission"] == "READY", candidate.json().get("holds")
    body = rejection_input(candidate.json(), evidence).model_dump()

    async def custody():
        async with store._unit():
            row = await requests.records.find_by_id(identifier(request_id))
            assert row is not None
            count = await requests.records.count_for_run(identifier(run_id), "invalid_read")
            native_row = (row.id.hex, row.run_id.hex, row.kind, row.request_key, row.status)
            ticket = copy.deepcopy(row.data)
        return {
            "count": count,
            "native": native_row,
            "ticket": ticket,
            "events": await requests.events(request_id),
            "journal": await journal.entries(run_id),
            "commands": await journal.sandbox_executions(run_id),
        }

    original = http.portal.call(custody)
    assert original["count"] == 1 and original["ticket"] == failed

    def routes_refuse(status):
        assert http.post(candidate_path, json=evidence.model_dump()).status_code == status
        assert http.post(recovery_path, json=body).status_code == status
        assert http.get(read_path).status_code == status
        assert http.portal.call(custody) == original

    http.headers.pop("Authorization", None)
    http.cookies.clear()
    routes_refuse(401)
    http.headers["Authorization"] = "Bearer " + issue("forge-api", sub="another-current-administrator")
    routes_refuse(403)
    http.headers["Authorization"] = admin
    membership = f"projects/{original_run['project_id']}/members/{original_actor_id}"
    assert http.put(membership, json={"role": "developer"}).status_code == 200
    http.headers["Authorization"] = original_authorization(["forge-worker"])
    routes_refuse(403)
    admin = original_authorization()
    http.headers["Authorization"] = admin
    assert http.put(membership, json={"role": None}).status_code == 200
    http.headers["Authorization"] = original_authorization(["forge-worker"])
    routes_refuse(403)
    admin = original_authorization()
    http.headers["Authorization"] = admin

    http.portal.call(partial(store.update, run_id, project_id=None))
    routes_refuse(403)
    http.portal.call(partial(store.update, run_id, project_id=original_run["project_id"]))
    http.portal.call(partial(store.update, run_id, component_id="different-project-component"))
    for route, value in ((candidate_path, evidence.model_dump()), (recovery_path, body)):
        refused = http.post(route, json=value)
        assert refused.status_code == 409, refused.text
    assert http.portal.call(custody) == original
    http.portal.call(partial(store.update, run_id, component_id=original_run["component_id"]))

    for invalid in (
        {**evidence.model_dump(), "extra": True},
        {**evidence.model_dump(), "version": True},
        {**evidence.model_dump(), "admissions": False},
        {**evidence.model_dump(), "jobs": "0"},
        {**evidence.model_dump(), "provider_dispatches": 1},
        {**evidence.model_dump(), "tool_callbacks": False},
        {
            **evidence.model_dump(),
            "validation_epoch": {**evidence.validation_epoch.model_dump(), "source_tree": "9" * 40},
        },
    ):
        refused = http.post(candidate_path, json=invalid)
        assert refused.status_code == 422, refused.text
    for key in (
        "acknowledge_reviewed_provider_epoch",
        "acknowledge_no_original_response_or_tool_replay",
        "acknowledge_same_request_attempt_and_budgets",
        "acknowledge_finite_ownership_lease",
    ):
        for value in (False, 1, "true", None):
            refused = http.post(recovery_path, json={**body, key: value})
            assert refused.status_code == 422, refused.text
    for invalid in ({**body, "extra": True}, {**body, "valid_for_seconds": "180"}):
        refused = http.post(recovery_path, json=invalid)
        assert refused.status_code == 422, refused.text
    stale = http.post(recovery_path, json={**body, "candidate_digest": "f" * 64})
    assert stale.status_code == 409, stale.text
    assert http.portal.call(custody) == original

    candidate = http.post(candidate_path, json=evidence.model_dump())
    assert candidate.status_code == 200 and candidate.json()["admission"] == "READY", candidate.text
    body = rejection_input(candidate.json(), evidence).model_dump()
    admitted = http.post(recovery_path, json=body)
    assert admitted.status_code == 201, admitted.text
    receipt = admitted.json()
    assert receipt["request_id"] == request_id and receipt["status"] == "queued"
    assert http.get(read_path).json() == receipt
    queued = http.portal.call(custody)
    assert queued["count"] == 1 and queued["native"][:-1] == original["native"][:-1]
    assert queued["ticket"]["generation"] == failed["generation"]
    assert queued["ticket"]["owner_pid"] == failed["owner_pid"]
    assert queued["ticket"]["admission_seal"] == failed["admission_seal"]
    assert queued["ticket"]["checkpoint_digest"] == failed["checkpoint_digest"]
    assert queued["events"][:-1] == original["events"] and queued["events"][-1]["kind"] == REJECTION_ADMITTED
    assert queued["journal"][:-1] == original["journal"] and queued["journal"][-1]["kind"] == REJECTION_RECOVERY
    assert queued["commands"] == original["commands"]
    repeated = http.post(recovery_path, json=body)
    assert repeated.status_code == 201 and repeated.json() == receipt
    assert http.post(path + "/rejected-boundary-recoveries/" + uuid4().hex, json=body).status_code == 409
    assert http.post(recovery_path, json={**body, "valid_for_seconds": 1}).status_code == 409
    assert http.post(candidate_path, json=evidence.model_dump()).status_code == 409
    assert http.portal.call(custody) == queued
    for action, argument in ((service.execute, request_id), (service.rejections.execute, queued["ticket"])):
        with pytest.raises(RuntimeError, match="API role"):
            http.portal.call(action, argument)
    assert http.get(read_path).json() == receipt and http.portal.call(custody) == queued

    async def suspend_original_actor():
        async with store._unit():
            identities = ForgeIdentityUserRepository()
            actor = await identities.find_by_id(identifier(original_actor_id))
            assert actor is not None and actor.enabled
            actor.enabled = False
            await identities.save(actor)

    http.portal.call(suspend_original_actor)
    for route, value in ((candidate_path, evidence.model_dump()), (recovery_path, body)):
        assert http.post(route, json=value).status_code == 401
    assert http.get(read_path).status_code == 401
    assert http.portal.call(custody) == queued
    assert len(posts) == 2 and len(commands) == 11 and not engine.active and not dispatches and not provider_calls
