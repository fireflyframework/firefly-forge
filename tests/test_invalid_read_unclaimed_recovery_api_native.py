# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""PREPARED, UNRUN: real PyFly/SQL/OIDC route custody; no live qualification or model.

The existing identity fixture validates signed OIDC tokens through native JWKS.
The historical worker seed uses only its public MockTransport. Its runtime and
profile pins are controlled fixture seams, never actual registered qualification.
Only the sole database operator may run this case in its isolated _test database.
"""

import copy
from uuid import uuid4

import pytest
import test_identity_write_clients
import test_invalid_read_continuity_native as historical
from pydantic_ai import models

from firefly_forge import invalid_read_continuities as native
from firefly_forge.agentic_checkpoint_invocation import runtime_contract
from firefly_forge.engine import Engine
from firefly_forge.execution_requests import ExecutionRequests
from firefly_forge.invalid_read_continuity_controllers import (
    ContinuityLeasePolicyController,
    InvalidReadContinuityController,
)
from firefly_forge.invalid_read_lease import ContinuityLeasePolicies
from firefly_forge.journal import Journal
from firefly_forge.runtime_roles import RuntimeRoles
from firefly_forge.store import Store

native_identity_writes = test_identity_write_clients.native_identity_writes


@models.override_allow_model_requests(True)
def test_native_unclaimed_recovery_api_is_original_admin_scoped_and_cannot_dispatch(
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

    from test_invalid_read_recovery_host import recovery_input

    from firefly_forge.invalid_read_continuity_models import InvalidReadAdmissionInputV1

    base = f"runs/{run_id}/invalid-read-continuity"
    http.headers["Authorization"] = "Bearer " + issue("forge-api")
    policy = http.put("invalid-read-continuity/policy", json={"version": 1, "enabled": True, "max_window_seconds": 180})
    assert policy.status_code == 200, policy.text
    candidate = http.get(base + "/candidate")
    assert candidate.status_code == 200, candidate.text
    c = candidate.json()
    request_id = uuid4().hex
    body = InvalidReadAdmissionInputV1(
        version=1,
        candidate_digest=c["digest"],
        expected_run_digest=c["run_digest"],
        expected_journal_digest=c["journal_digest"],
        expected_policy_digest=c["lease_policy"]["digest"],
        valid_for_seconds=180,
        acknowledge_no_new_grant=True,
        acknowledge_original_consumption=True,
        acknowledge_renewed_same_attempt_lease=True,
    )
    admitted = http.post(base + "/requests/" + request_id, json=body.model_dump())
    assert admitted.status_code == 201, admitted.text
    http.portal.call(service._finish, request_id, "failed", {"detail": "Controlled preclaim failure"})
    path = base + "/requests/" + request_id
    before = http.get(path + "/recovery")
    assert before.status_code == 200 and before.json()["status"] == "not_recovered"
    inspected = http.get(path + "/recovery-candidate")
    assert inspected.status_code == 200, inspected.text
    assert inspected.json()["admission"] == "READY"
    recovery_id = uuid4().hex
    request = recovery_input(inspected.json()).model_dump()
    bad = {**request, "acknowledge_never_claimed": 1}
    assert http.post(path + "/recoveries/" + recovery_id, json=bad).status_code == 422
    recovered = http.post(path + "/recoveries/" + recovery_id, json=request)
    assert recovered.status_code == 201, recovered.text
    assert recovered.json()["request_id"] == request_id and recovered.json()["status"] == "queued"
    assert http.get(path + "/recovery").json() == recovered.json()
    assert http.post(path + "/recoveries/" + recovery_id, json=request).json() == recovered.json()
    assert len(posts) == 1 and not engine.active

    async def cannot_dispatch():
        with pytest.raises(RuntimeError, match="API role"):
            await service.execute(request_id)

    http.portal.call(cannot_dispatch)
    assert len(posts) == 1
    http.headers.pop("Authorization")
    assert http.get(path + "/recovery").status_code == 401
