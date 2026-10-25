# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Native rollback semantics with explicit host ACL substitutes, not physical-lock proof."""

import copy
import importlib
import importlib.util
from uuid import uuid4

import pytest
from test_known_worker_completion_proof_host import known_worker as known_worker

from firefly_forge.project_access import AccessDenied, ProjectAccess
from firefly_forge.store import Store


@pytest.fixture
async def settlement(known_worker, monkeypatch):
    from types import MethodType

    env = known_worker
    env.allowed = True

    async def allow(*args, **kwargs):
        if not env.allowed:
            raise AccessDenied("Controlled current project permission revoked")

    monkeypatch.setattr(ProjectAccess, "require_project", allow)
    monkeypatch.setattr(ProjectAccess, "require_repository", allow)
    env.store.reject_credentials = MethodType(Store.reject_credentials, env.store)

    async def admit(value):
        env.store.reject_credentials(value)

    env.store.admit_credentials = admit
    token = env.principal_context.set(env.principal)
    try:
        yield env
    finally:
        env.principal_context.reset(token)


def service(env):
    assert importlib.util.find_spec("firefly_forge.known_worker_completions"), (
        "Native known-worker settlement is missing"
    )
    return importlib.import_module("firefly_forge.known_worker_completions").KnownWorkerCompletions(
        env.store, env.journal
    )


def consent(candidate):
    from firefly_forge.known_worker_completion_models import WorkerCompletionInputV1

    return WorkerCompletionInputV1(
        version=1,
        candidate_digest=candidate["digest"],
        policy_digest=candidate["policy_digest"],
        expected_run_digest=candidate["run_digest"],
        journal_digest=candidate["journal_digest"],
        proof_digest=candidate["proof"]["digest"],
        feedback_record_digest=candidate["proof"]["feedback_record_digest"],
        acknowledge_known_worker_output=True,
        acknowledge_no_execution_grant=True,
    )


async def test_typed_receipt_and_observed_hold_projection_commit_once_without_replay(settlement):
    env = settlement
    api = service(env)
    before = copy.deepcopy(env.store.run.data)
    candidate = await api.candidate(env.known_run["id"])
    request = consent(candidate)
    request_id = uuid4().hex
    record = await api.request(env.known_run["id"], request_id, request)
    assert record["kind"] == "worker_completion_receipt"
    assert record["payload"]["proof"] == candidate["proof"]
    assert record["payload"]["observed_hold"] == before["bridge_completion_hold"]
    assert env.store.run.data["bridge_completion_hold"] == {}
    assert {
        k: v
        for k, v in env.store.run.data.items()
        if k not in {"bridge_completion_hold", "bridge_completion_settlement"}
    } == {k: v for k, v in before.items() if k != "bridge_completion_hold"}
    assert await api.request(env.known_run["id"], request_id, request) == record
    assert len([r for r in env.journal.records.rows if r.kind == "worker_completion_receipt"]) == 1
    assert len(env.posts) == 2


async def test_operator_change_during_candidate_admission_never_returns_a_candidate(settlement):
    env = settlement
    api = service(env)
    original = env.store.admit_credentials

    async def changed(value):
        await original(value)
        if isinstance(value, dict) and "actor_digest" in value and "proof" in value:
            env.principal_context.set(None)

    env.store.admit_credentials = changed
    with pytest.raises(AccessDenied):
        await api.candidate(env.known_run["id"])
    assert env.store.run.data["bridge_completion_hold"]


async def test_receipt_insert_and_projection_rollback_together_on_save_failure(settlement, monkeypatch):
    env = settlement
    api = service(env)
    candidate = await api.candidate(env.known_run["id"])
    before = copy.deepcopy((env.store.run.data, [row.data for row in env.journal.records.rows]))
    original = env.store.run_records.save

    async def failed(row):
        await original(row)
        raise RuntimeError("Controlled projection storage failure")

    monkeypatch.setattr(env.store.run_records, "save", failed)
    with pytest.raises(RuntimeError, match="storage failure"):
        await api.request(env.known_run["id"], uuid4().hex, consent(candidate))
    assert (env.store.run.data, [row.data for row in env.journal.records.rows]) == before
    assert len(env.posts) == 2


async def test_revoked_operator_and_stale_candidate_cannot_change_the_observed_hold(settlement):
    env = settlement
    api = service(env)
    candidate = await api.candidate(env.known_run["id"])
    env.allowed = False
    with pytest.raises(AccessDenied):
        await api.request(env.known_run["id"], uuid4().hex, consent(candidate))
    env.allowed = True
    env.store.run.data["verification_refusal"] = {"kind": "changed independently"}
    with pytest.raises(ValueError, match="candidate changed"):
        await api.request(env.known_run["id"], uuid4().hex, consent(candidate))
    assert env.store.run.data["bridge_completion_hold"]
    assert not any(row.kind == "worker_completion_receipt" for row in env.journal.records.rows)


async def test_request_identity_cannot_replace_an_already_confirmed_input(settlement):
    env = settlement
    api = service(env)
    candidate = await api.candidate(env.known_run["id"])
    request = consent(candidate)
    request_id = uuid4().hex
    receipt = await api.request(env.known_run["id"], request_id, request)
    other = request.model_copy(update={"proof_digest": "f" * 64})
    with pytest.raises(AccessDenied, match="another input or operator"):
        await api.request(env.known_run["id"], request_id, other)
    assert await api.get(env.known_run["id"], request_id) == receipt
    assert len(env.posts) == 2


async def test_same_actor_permission_revocation_during_candidate_scan_refuses(settlement):
    env = settlement
    api = service(env)
    original = env.store.admit_credentials

    async def revoke(value):
        await original(value)
        if isinstance(value, dict) and "actor_digest" in value and "proof" in value:
            env.allowed = False

    env.store.admit_credentials = revoke
    with pytest.raises(AccessDenied):
        await api.candidate(env.known_run["id"])
    assert env.store.run.data["bridge_completion_hold"]


@pytest.mark.parametrize("route", ["get", "idempotent_post"])
async def test_new_credential_in_retained_metadata_blocks_every_receipt_return(settlement, route):
    env = settlement
    api = service(env)
    candidate = await api.candidate(env.known_run["id"])
    request = consent(candidate)
    request_id = uuid4().hex
    receipt = await api.request(env.known_run["id"], request_id, request)
    secret = receipt["payload"]["proof"]["feedback_sha256"]
    env.store.credential_registry = lambda: {"RETAINED_OUTPUT_TOKEN": secret}
    with pytest.raises(ValueError, match="configured credentials"):
        if route == "get":
            await api.get(env.known_run["id"], request_id)
        else:
            await api.request(env.known_run["id"], request_id, request)
    assert len([row for row in env.journal.records.rows if row.kind == "worker_completion_receipt"]) == 1
    assert len(env.posts) == 2


async def test_terminal_run_refuses_before_the_live_execution_guard(settlement, monkeypatch):
    env = settlement
    api = service(env)
    env.store.run.data["status"] = "failed"
    env.store.run.status = "failed"

    async def unexpected(*args, **kwargs):
        raise AssertionError("An ended run must not reach the live completion guard")

    monkeypatch.setattr(api.completions, "_execution", unexpected)
    with pytest.raises(ValueError, match="ended or cancelled"):
        await api.candidate(env.known_run["id"])
    assert env.store.run.data["bridge_completion_hold"]
