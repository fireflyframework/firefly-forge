# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""PREPARED, UNRUN: real PyFly/SQL/OIDC route custody; no live qualification or model.

The existing identity fixture validates signed OIDC tokens through native JWKS.
The historical worker seed uses only its public MockTransport. Its runtime and
profile pins are controlled fixture seams, never actual registered qualification.
Only the sole database operator may run this case in its isolated _test database.
"""

import copy
import traceback
from uuid import uuid4

import pytest
import test_identity_write_clients
import test_invalid_read_continuity_native as historical
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
from firefly_forge.runtime_roles import RuntimeRoles
from firefly_forge.store import Store, identifier

native_identity_writes = test_identity_write_clients.native_identity_writes


@models.override_allow_model_requests(True)
def test_native_unavailable_observation_api_requires_policy_v2_ack_and_same_actor(
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
            runtime = RuntimeRoles(worker_store, role="worker")
            assert runtime.store is worker_store and runtime.role == "worker"
            return runtime

        original_result = historical.result

        def response_with_unretained_source_call(*args, **kwargs):
            response = original_result(*args, **kwargs)
            if kwargs.get("tool"):
                response["choices"][0]["message"]["tool_calls"].append(
                    {
                        "id": "original-unretained-source-read",
                        "type": "function",
                        "function": {"name": "list_files", "arguments": "{}"},
                    }
                )
            return response

        patch.setattr(historical, "result", response_with_unretained_source_call)
        patch.setattr(historical, "Engine", worker_engine)
        patch.setattr(historical, "RuntimeRoles", worker_runtime)
        source = historical.native_continuity.__wrapped__(worker, tmp_path, patch)
        try:
            case = await anext(source)
            frozen = copy.deepcopy(await worker.get_run(case.run_id))
            return case.run_id, frozen, case.posts
        finally:
            try:
                await source.aclose()
            finally:
                patch.undo()
                await worker.close()

    assert http.portal is not None
    run_id, original, posts = http.portal.call(seed_worker)
    monkeypatch.setattr(native, "LEGACY_RUNTIME_CONTRACT", runtime_contract())
    monkeypatch.setattr(
        native,
        "current_profile_seal",
        lambda: {"profile_id": "CONTROLLED-API-FIXTURE-NOT-QUALIFIED", "digest": "8" * 64},
    )
    base = f"runs/{run_id}/invalid-read-continuity"
    policy_route = "invalid-read-continuity/policy"
    policy = {
        "version": 1,
        "enabled": True,
        "allow_unavailable_source_observations": False,
        "max_window_seconds": 60,
        "max_tickets_per_run": 51,
        "max_pending_tickets": 501,
    }
    original_posts = len(posts)
    assert original_posts == 1 and not engine.active
    admin_token = issue("forge-api")

    http.headers.pop("Authorization", None)
    assert http.get(base + "/candidate").status_code == 401
    assert http.get(policy_route).status_code == 401

    http.headers["Authorization"] = "Bearer " + issue(
        "forge-api", roles=["forge-worker"], sub="continuity-current-operator"
    )
    session = http.get("auth/session")
    assert session.status_code == 200, session.text
    operator = session.json()["user"]["id"]
    operator_token = http.headers["Authorization"]
    assert http.get(base + "/candidate").status_code == 403
    assert http.get(policy_route).status_code == 403
    assert http.put(policy_route, json=policy).status_code == 403

    http.headers["Authorization"] = "Bearer " + admin_token
    configured = http.put(policy_route, json=policy)
    assert configured.status_code == 200, configured.text
    assert configured.json()["settings"]["enabled"] is True
    assert configured.json()["settings"]["allow_unavailable_source_observations"] is False
    assert configured.json()["settings"]["max_tickets_per_run"] == 51
    assert configured.json()["settings"]["max_pending_tickets"] == 501
    membership = f"projects/{original['project_id']}/members/{operator}"
    assert http.put(membership, json={"role": "developer"}).status_code == 200
    http.headers["Authorization"] = operator_token
    assert http.get(base + "/candidate").status_code == 409
    policy["allow_unavailable_source_observations"] = True
    assert http.put(policy_route, json=policy).status_code == 403
    http.headers["Authorization"] = "Bearer " + admin_token
    enabled = http.put(policy_route, json=policy)
    assert enabled.status_code == 200, enabled.text
    assert enabled.json()["settings"]["allow_unavailable_source_observations"] is True

    prepare_errors = []
    prepare = service._prepare

    async def diagnostic_prepare(*args, **kwargs):
        try:
            return await prepare(*args, **kwargs)
        except Exception as error:
            prepare_errors.append(
                type(error).__module__ + "." + type(error).__qualname__ + "\n" + traceback.format_exc()
            )
            raise

    monkeypatch.setattr(service, "_prepare", diagnostic_prepare)
    http.headers["Authorization"] = operator_token
    candidate = http.get(base + "/candidate")
    assert candidate.status_code == 200, "\n".join([candidate.text, *prepare_errors])
    candidate = candidate.json()
    assert candidate["admission"] == "READY", candidate["holds"]
    assert candidate["proof"]["version"] == 2
    observation = candidate["proof"]["unavailable_observations"][0]
    assert observation["outcome"] == {
        "status": "observation_unavailable",
        "reason": "original_read_result_not_retained",
    }
    assert candidate["current_profile"]["profile_id"] == "CONTROLLED-API-FIXTURE-NOT-QUALIFIED"
    body = {
        "version": 2,
        "candidate_digest": candidate["digest"],
        "expected_run_digest": candidate["run_digest"],
        "expected_journal_digest": candidate["journal_digest"],
        "expected_policy_digest": candidate["lease_policy"]["digest"],
        "valid_for_seconds": 60,
        "acknowledge_no_new_grant": True,
        "acknowledge_original_consumption": True,
        "acknowledge_renewed_same_attempt_lease": True,
        "acknowledge_unavailable_source_observations": True,
    }
    request_id = uuid4().hex
    route = base + "/requests-v2/" + request_id
    read_route = base + "/requests/" + request_id
    for invalid in (
        {**body, "extra": "not-admitted"},
        {k: v for k, v in body.items() if k != "acknowledge_unavailable_source_observations"},
        {**body, "acknowledge_unavailable_source_observations": 1},
        {**body, "acknowledge_unavailable_source_observations": False},
        {**body, "acknowledge_no_new_grant": 1},
        {**body, "acknowledge_original_consumption": "true"},
        {**body, "acknowledge_renewed_same_attempt_lease": False},
    ):
        rejected = http.post(route, json=invalid)
        assert rejected.status_code == 422, rejected.text
    assert http.post(read_route, json=body).status_code == 422
    queued = http.post(route, json=body)
    assert queued.status_code == 201, queued.text
    ticket = queued.json()
    assert ticket["status"] == "queued" and ticket["generation"] is None
    assert http.get(read_route).json() == ticket
    assert http.post(route, json=body).json() == ticket
    assert http.post(base + "/requests-v2/" + uuid4().hex, json=body).status_code == 409
    assert len(posts) == original_posts and not engine.active
    with pytest.raises(RuntimeError, match="API role"):
        http.portal.call(service.execute, ticket["id"])
    assert http.get(read_route).json() == ticket
    entries = http.portal.call(journal.entries, run_id)
    assert sum(e["kind"] == "invalid_read_settlement" for e in entries) == 1
    assert sum(e["kind"] == "invalid_read_claim" for e in entries) == 1
    assert sum(e["kind"] == "agent_consume" for e in entries) == 1

    http.headers["Authorization"] = "Bearer " + admin_token
    assert http.put(membership, json={"role": None}).status_code == 200
    http.headers["Authorization"] = operator_token
    assert http.get(read_route).status_code == 403
    assert http.get(base + "/candidate").status_code == 403
    http.headers["Authorization"] = "Bearer " + admin_token
    assert http.put(membership, json={"role": "developer"}).status_code == 200

    async def revoke_operator():
        async with store._unit():
            repository = ForgeIdentityUserRepository()
            user = await repository.find_by_id(identifier(operator))
            assert user is not None and user.enabled
            user.enabled = False
            await repository.save(user)

    http.portal.call(revoke_operator)
    http.headers["Authorization"] = operator_token
    assert http.get(read_route).status_code == 401
    assert http.post(route, json=body).status_code == 401
    assert len(posts) == original_posts and not engine.active and not provider_calls
