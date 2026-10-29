# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Real manager methods retain one durable claim before controlled child execution."""

import asyncio
import copy

import pytest
from test_factory_delegation_authority_host import seed
from test_factory_delegation_contract_host import parent

from firefly_forge.delegation_contract import child_run, team_capture
from firefly_forge.delegation_models import DelegationStartInput
from firefly_forge.factory_delegations import approval_digest
from firefly_forge.journal import digest


async def prepared(tmp_path, monkeypatch):
    manager, session, native, task = await seed(tmp_path, monkeypatch)
    values = parent()
    values.pop("framework_knowledge_pin")
    values.update(attempt=1, active_task="implement")
    values["factory_team"] = team_capture(values)
    native.run.update(values)
    source = {"digest": "a" * 64, "path": str(tmp_path / "origin"), "files": {}, "revision": "b" * 40, "tree": "c" * 40}
    request = DelegationStartInput(request_id="d" * 32, task_id="analysis", brief="Analyze source")
    child = child_run(native.run, request, source)
    child["factory_context"]["parent_task_key"] = session.task_key
    child["factory_context"]["digest"] = digest({k: v for k, v in child["factory_context"].items() if k != "digest"})
    async with native.store._unit():
        event = await manager.ledger.append_locked(
            session.run_id,
            "child_admitted",
            "admit-child",
            {
                "parent_run_id": session.run_id,
                "parent_task_key": session.task_key,
                "child_run_id": child["id"],
                "request": request.model_dump(mode="json"),
                "approval_digest": approval_digest(child),
                "source_digest": source["digest"],
            },
        )
    original = native.store.get_run

    async def get_run(identity):
        return copy.deepcopy(child) if identity == child["id"] else await original(identity)

    native.store.get_run = get_run
    native.store._ownership_pid = 123
    native.store.execution_ownership_token = object()

    async def admit_credentials(value):
        pass

    native.store.admit_credentials = admit_credentials
    native.store.reject_credentials = lambda value: None

    async def verify(value):
        assert value == source

    monkeypatch.setattr("firefly_forge.factory_delegations.verify_child_source", verify, raising=False)
    return manager, session, native, child, event


async def test_manager_status_survives_reinstantiation_without_dispatch(tmp_path, monkeypatch):
    manager, session, native, child, event = await prepared(tmp_path, monkeypatch)
    assert hasattr(manager, "observe"), "Missing native child status reducer in manager"
    result = await manager.observe(session, [child["id"]])
    assert result["children"][0]["state"] == "queued"
    assert result["children"][0]["admission_digest"] == event["digest"]
    assert result["after_sequence"] == 1 and len(native.rows) == 2


async def test_concurrent_manager_claims_allow_one_callback_and_preserve_spent_claim_after_restart(
    tmp_path, monkeypatch
):
    manager, session, native, child, _ = await prepared(tmp_path, monkeypatch)
    assert hasattr(manager, "claim_child"), "Missing native child dispatch CAS"
    claims = await asyncio.gather(
        *(manager.claim_child(session, child["id"]) for _ in range(2)), return_exceptions=True
    )
    assert sum(isinstance(value, ValueError) for value in claims) == 1
    winner = next(value for value in claims if not isinstance(value, BaseException))
    assert winner["state"] == "running"
    assert len([row for row in native.rows if row["kind"] == "factory_team_event"]) == 2
    with pytest.raises(ValueError):
        await manager.claim_child(session, child["id"])


@pytest.mark.parametrize("drift", ["approval", "parent_attempt", "cancel"])
async def test_current_authority_drift_refuses_before_native_child_claim(tmp_path, monkeypatch, drift):
    manager, session, native, child, _ = await prepared(tmp_path, monkeypatch)
    if drift == "approval":
        child["role_snapshot"]["roles"][0]["allowed_tools"].append("external_mcp")
    elif drift == "parent_attempt":
        native.run["attempt"] = 2
    else:
        native.run["cancel_request"] = {"status": "requested"}
    with pytest.raises(ValueError):
        await manager.claim_child(session, child["id"])
    assert len(native.rows) == 2


async def test_known_child_result_settles_once_and_cannot_be_replaced(tmp_path, monkeypatch):
    manager, session, native, child, _ = await prepared(tmp_path, monkeypatch)
    claimed = await manager.claim_child(session, child["id"])
    result = {"summary": "Read-only child analysis", "changes": [], "delivery_verified": False}
    settled = await manager.settle_child(session, child["id"], claimed["claim_digest"], "completed", result)
    assert settled["state"] == "completed" and settled["summary"] == result["summary"]
    assert await manager.settle_child(session, child["id"], claimed["claim_digest"], "completed", result) == settled
    with pytest.raises(ValueError):
        await manager.settle_child(
            session, child["id"], claimed["claim_digest"], "completed", {**result, "summary": "Changed"}
        )
    assert len(native.rows) == 4


async def test_old_manager_owner_cannot_settle_after_owner_replacement(tmp_path, monkeypatch):
    manager, session, native, child, _ = await prepared(tmp_path, monkeypatch)
    claimed = await manager.claim_child(session, child["id"])
    native.store.execution_ownership_token = object()
    with pytest.raises(ValueError):
        await manager.settle_child(
            session,
            child["id"],
            claimed["claim_digest"],
            "completed",
            {"summary": "Late", "changes": [], "delivery_verified": False},
        )
    assert (await manager.observe(session, [child["id"]]))["children"][0]["state"] == "running"


async def test_child_frozen_authority_drift_after_claim_cannot_publish_success(tmp_path, monkeypatch):
    manager, session, native, child, _ = await prepared(tmp_path, monkeypatch)
    claimed = await manager.claim_child(session, child["id"])
    child["role_snapshot"]["roles"][0]["allowed_tools"].append("external_mcp")
    with pytest.raises(ValueError):
        await manager.settle_child(
            session,
            child["id"],
            claimed["claim_digest"],
            "completed",
            {"summary": "Late", "changes": [], "delivery_verified": False},
        )
    assert len(native.rows) == 3


async def test_result_credential_admission_failure_retains_spent_claim_without_terminal(tmp_path, monkeypatch):
    manager, session, native, child, _ = await prepared(tmp_path, monkeypatch)
    claimed = await manager.claim_child(session, child["id"])

    async def refused(value):
        raise ValueError("Controlled credential refusal")

    monkeypatch.setattr(native.store, "admit_credentials", refused, raising=False)
    with pytest.raises(ValueError, match="credential refusal"):
        await manager.settle_child(
            session,
            child["id"],
            claimed["claim_digest"],
            "completed",
            {"summary": "Rejected summary", "changes": [], "delivery_verified": False},
        )
    assert len(native.rows) == 3
