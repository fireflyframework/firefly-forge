# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Fictional signed-OIDC PostgreSQL consumed-final-outcome API cases.

The producer uses public MockTransport, a test database, and a deterministic sandbox. Fixture-only
qualification seams do not qualify an actual provider or retained run.
"""

import copy
import time
from functools import partial, wraps
from types import SimpleNamespace
from uuid import uuid4

import pytest
import test_identity_write_clients
import test_invalid_read_continuity_native as historical
from checkpoint_continuation_native_fixtures import native_checkpoint_cut
from final_outcome_native_fixtures import final_input, seed_consumed_final
from pydantic_ai import models

from firefly_forge import invalid_read_continuities as native
from firefly_forge.agentic_checkpoint_invocation import runtime_contract
from firefly_forge.engine import Engine
from firefly_forge.execution_requests import ExecutionRequests
from firefly_forge.final_outcome_continuations import ADMITTED, HANDOFF
from firefly_forge.final_outcome_controllers import FinalOutcomeContinuationController
from firefly_forge.identity_persistence import ForgeIdentityUserRepository
from firefly_forge.invalid_read_lease import ContinuityLeasePolicies
from firefly_forge.journal import Journal
from firefly_forge.runtime_roles import RuntimeRoles
from firefly_forge.store import Store, identifier
from firefly_forge.workspace_tools import WorkspaceTools

native_identity_writes = test_identity_write_clients.native_identity_writes


@models.override_allow_model_requests(True)
def test_native_final_outcome_api_auth_csrf_exact_admission_and_api_role_never_dispatch(
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
    assert service.final_outcomes.store is store and service.final_outcomes.requests is requests
    assert service.final_outcomes.engine is engine and service.final_outcomes.journal is journal
    assert requests.invalid_read_continuities is service and store.execution_requests is requests
    assert context.get_bean(FinalOutcomeContinuationController).continuities is service
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
            failed = await seed_consumed_final(case, patch)
            run = copy.deepcopy(await worker.get_run(case.run_id))
            return (
                case.run_id,
                failed,
                run,
                case.posts,
                case.command_calls,
                case.checkpoint_writes,
                case.checkpoint_reads,
            )
        finally:
            try:
                await source.aclose()
            finally:
                patch.undo()
                await worker.close()

    assert http.portal is not None
    run_id, failed, original_run, posts, commands, writes, reads = http.portal.call(seed_worker)
    assert failed["status"] == "needs_reconciliation" and failed["generation"] and failed["owner_pid"]
    assert (
        len(posts) == 4
        and len(commands) == 11
        and writes == [("existing.py", "VALUE = 2\n")]
        and reads == ["existing.py"]
    )
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
    monkeypatch.setattr(engine, "continue_agent_checkpoint", forbidden_dispatch)

    def forbidden_callback(original):
        @wraps(original)
        async def callback(*args, **kwargs):
            return await forbidden_dispatch(*args, **kwargs)

        return callback

    for name in ("read_file", "write_file"):
        monkeypatch.setattr(WorkspaceTools, name, forbidden_callback(getattr(WorkspaceTools, name)))

    async def no_network(*args, **kwargs):
        raise AssertionError("Fictional API cases must not use a real provider transport")

    def no_sync_network(*args, **kwargs):
        raise AssertionError("Fictional API cases must not use a real provider transport")

    import httpx
    import httpx2

    for module in (httpx, httpx2):
        monkeypatch.setattr(module.AsyncHTTPTransport, "handle_async_request", no_network)
        monkeypatch.setattr(module.HTTPTransport, "handle_request", no_sync_network)
    request_id = failed["id"]
    path = f"runs/{run_id}/final-outcome-continuations/requests/{request_id}"
    candidate_path = path + "/candidate"
    recovery_path = path + "/actions/" + uuid4().hex
    read_path = path + "/status"
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
    policy = {"version": 1, "enabled": True, "allow_pre_admission_recovery": True, "max_window_seconds": 180}
    configured = http.put("invalid-read-continuity/policy", json=policy)
    assert configured.status_code == 200, configured.text
    assert configured.json()["settings"].get("allow_final_outcome_continuation", False) is False
    before = http.get(read_path)
    assert before.status_code == 200 and before.json()["status"] == "not_recovered"
    assert before.json()["action_id"] is None and before.json()["witness_digest"] is None

    case = SimpleNamespace(store=store, requests=requests, journal=journal, run_id=run_id)

    async def custody():
        return await native_checkpoint_cut(case, request_id)

    original = http.portal.call(custody)
    assert original["count"] == 1 and original["ticket"] == failed and len(original["events"]) == 6
    held = http.get(candidate_path)
    assert held.status_code == 200 and held.json()["admission"] == "HOLD", held.text
    assert "final_outcome_continuation_disabled" in held.json()["holds"]
    assert http.post(recovery_path, json=final_input(held.json()).model_dump()).status_code == 409
    assert http.portal.call(custody) == original
    configured = http.put("invalid-read-continuity/policy", json={**policy, "allow_final_outcome_continuation": True})
    assert configured.status_code == 200, configured.text
    candidate = http.get(candidate_path)
    assert candidate.status_code == 200 and candidate.json()["admission"] == "READY", candidate.text
    assert candidate.json()["execution_authorized"] is False
    body = final_input(candidate.json()).model_dump()

    def routes_refuse(status, *, write_status=None):
        assert http.get(candidate_path).status_code == status
        refused = http.post(recovery_path, json=body)
        assert refused.status_code == (status if write_status is None else write_status)
        if write_status is not None:
            assert refused.json() == {"error": "CSRF token missing"}
        assert http.get(read_path).status_code == status
        assert http.portal.call(custody) == original

    http.headers.pop("Authorization", None)
    http.cookies.clear()
    routes_refuse(401, write_status=403)
    http.cookies.clear()
    refused = http.post(recovery_path, json=body)
    assert refused.status_code == 401 and refused.json() == {"detail": "Sign in to access the factory"}
    assert http.portal.call(custody) == original
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
    assert http.get(candidate_path).status_code == 409
    assert http.post(recovery_path, json=body).status_code == 409
    assert http.portal.call(custody) == original
    http.portal.call(partial(store.update, run_id, component_id=original_run["component_id"]))

    for key in (
        "acknowledge_already_consumed_final_output",
        "acknowledge_no_completed_effect_replay",
        "acknowledge_same_request_attempt_and_budgets",
        "acknowledge_finite_ownership_lease",
    ):
        for value in (False, 1, "true", None):
            refused = http.post(recovery_path, json={**body, key: value})
            assert refused.status_code == 422, refused.text
        incomplete = {k: v for k, v in body.items() if k != key}
        assert http.post(recovery_path, json=incomplete).status_code == 422
    for invalid in (
        {**body, "extra": True},
        {**body, "version": True},
        {**body, "valid_for_seconds": "180"},
        {**body, "valid_for_seconds": True},
        {**body, "valid_for_seconds": 0},
        {**body, "valid_for_seconds": 7201},
        {**body, "candidate_digest": "not-a-digest"},
    ):
        refused = http.post(recovery_path, json=invalid)
        assert refused.status_code == 422, refused.text
    assert http.post(recovery_path, json={**body, "candidate_digest": "f" * 64}).status_code == 409
    assert http.post(recovery_path, json={**body, "valid_for_seconds": 181}).status_code == 409
    assert http.portal.call(custody) == original

    candidate = http.get(candidate_path)
    assert candidate.status_code == 200 and candidate.json()["admission"] == "READY", candidate.text
    body = final_input(candidate.json()).model_dump()
    admitted = http.post(recovery_path, json=body)
    assert admitted.status_code == 201, admitted.text
    receipt = admitted.json()
    assert receipt["request_id"] == request_id and receipt["status"] == "queued"
    assert receipt["action_id"] == recovery_path.rsplit("/", 1)[-1]
    assert receipt["original_admission_seal"] == failed["admission_seal"]
    assert http.get(read_path).json() == receipt
    queued = http.portal.call(custody)
    assert queued["count"] == 1 and queued["native"][:-1] == original["native"][:-1]
    for key in ("generation", "owner_pid", "admission_seal", "checkpoint", "checkpoint_digest", "binding"):
        assert queued["ticket"][key] == failed[key]
    assert queued["events"][:-1] == original["events"] and queued["events"][-1]["kind"] == ADMITTED
    assert queued["journal"][:-1] == original["journal"] and queued["journal"][-1]["kind"] == HANDOFF
    assert queued["commands"] == original["commands"]
    repeated = http.post(recovery_path, json=body)
    assert repeated.status_code == 201 and repeated.json() == receipt
    assert http.post(path + "/actions/" + uuid4().hex, json=body).status_code == 409
    assert http.post(recovery_path, json={**body, "valid_for_seconds": 1}).status_code == 409
    assert http.get(candidate_path).status_code == 409
    assert http.portal.call(custody) == queued
    for action, argument in ((service.execute, request_id), (service.final_outcomes.execute, queued["ticket"])):
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
    assert http.get(candidate_path).status_code == 401
    assert http.post(recovery_path, json=body).status_code == 401
    assert http.get(read_path).status_code == 401
    assert http.portal.call(custody) == queued
    assert (
        len(posts) == 4
        and len(commands) == 11
        and writes == [("existing.py", "VALUE = 2\n")]
        and reads == ["existing.py"]
    )
    assert requests.poller is None
    assert not engine.active and not dispatches and not provider_calls
