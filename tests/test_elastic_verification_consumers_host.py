# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Full configured suites reach real consumers without renewing execution capacity."""

import copy
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_bridge_inflight_cancel import RUN, MemoryStore

from firefly_forge.coding_check_grants import CodingCheckGrants
from firefly_forge.coding_check_models import CodingCheckGrantInput
from firefly_forge.communication import communication_facts
from firefly_forge.engine import Engine
from firefly_forge.project_access import CURRENT_PRINCIPAL, ProjectAccess
from firefly_forge.sandbox_models import SandboxInput, capture_sandbox


def checks():
    return [{"argv": ["approved-check", str(index)], "timeout": 30} for index in range(51)]


@pytest.mark.parametrize("operation", ["build", "maintain", "test"])
async def test_actual_engine_reaches_verification_frontier_with_full_suite(tmp_path, monkeypatch, operation):
    store = MemoryStore(tmp_path / "state")
    store.run.data.update(operation=operation, commands=checks(), implementation_rounds=1, verification=[])
    worker = SimpleNamespace(execute=AsyncMock(side_effect=AssertionError("No provider dispatch is permitted")))
    engine = Engine(store, tmp_path / "workspaces", lambda _: worker)
    engine._inspect_workspace = AsyncMock(return_value={"base_revision": "b" * 40})
    engine._receipt = AsyncMock()

    async def begin(identifier, stage, *args):
        return {"disposition": "execute" if stage == "verify" else "completed", "idempotency_key": stage}

    engine._begin_task = begin
    snapshot = SimpleNamespace(
        artifact=SimpleNamespace(file_count=1, model_dump=lambda **_: {}),
        preview="Controlled source observation",
        preview_bytes=29,
        preview_truncated=False,
    )
    engine._snapshot = AsyncMock(return_value=(["product.py"], snapshot, "c" * 40))
    frontier = []

    async def command(identifier, run, workspace, argv, timeout, stage, **kwargs):
        frontier.append((copy.deepcopy(run["commands"]), argv, stage, kwargs["ordinal"]))
        raise RuntimeError("Controlled stop before any verification command effect")

    engine._project_command = command
    monkeypatch.setattr(ProjectAccess, "authorize_execution", AsyncMock())
    monkeypatch.setattr("firefly_forge.engine.checked", AsyncMock(return_value=""))
    await engine.execute_run(RUN)
    assert frontier == [(checks(), checks()[0]["argv"], "verify", 1)], store.run.data.get("error")
    assert "Controlled stop before any verification command effect" in store.run.data["error"]
    assert store.run.data["commands"] == checks() and store.run.data["implementation_rounds"] == 1
    worker.execute.assert_not_awaited()


@pytest.fixture
def grants():
    run = {
        "id": "a" * 32,
        "agent": "codex",
        "backend": "pyfly",
        "commands": checks(),
        "sandbox_snapshot": capture_sandbox(SandboxInput(command_limit=100)),
    }
    actor = SimpleNamespace()
    row = SimpleNamespace(data=run)

    @asynccontextmanager
    async def locked(*args):
        yield row

    recoveries = SimpleNamespace(
        _candidate=AsyncMock(
            return_value={
                "next_attempt": 5,
                "operator_digest": "b" * 64,
                "cut": {"source_digest": "c" * 64, "scope_digest": "d" * 64},
                "journal_digest": "e" * 64,
            }
        ),
        engine=SimpleNamespace(sandbox=object()),
        journal=SimpleNamespace(
            sandbox_executions=AsyncMock(return_value=[{}] * 5),
            _append=AsyncMock(return_value={"digest": "f" * 64}),
        ),
        store=SimpleNamespace(
            admit_credentials=AsyncMock(),
            reject_credentials=lambda _: None,
            run_records=SimpleNamespace(save=AsyncMock()),
        ),
        _operator=AsyncMock(return_value=actor),
        _locked=locked,
        _entries=AsyncMock(return_value=[]),
    )
    return CodingCheckGrants(recoveries), run, actor


async def test_real_candidate_and_grant_keep_full_scope_and_clip_calls_to_existing_reserve(grants, monkeypatch):
    service, run, actor = grants
    monkeypatch.setattr(service, "actor_snapshot", lambda _: {})
    token = CURRENT_PRINCIPAL.set(actor)
    try:
        candidate = await service.candidate(run["id"], "1" * 32)
        assert candidate["command_indices"] == list(range(51))
        assert candidate["commands"][50]["argv"] == checks()[50]["argv"]
        assert candidate["independent_final_check_count"] == 51
        assert candidate["shared_commands_used"] == 5 and candidate["interactive_commands_remaining"] == 44
        body = CodingCheckGrantInput(
            request_id="2" * 32,
            recovery_request_id="1" * 32,
            candidate_digest=candidate["digest"],
            expected_revision=0,
            command_indices=list(range(51)),
            call_limit=137,
            valid_seconds=300,
            reason="Explicit review of the full configured suite",
        )
        receipt = await service.grant(run["id"], body)
        assert receipt["call_limit"] == 44 and receipt["request"]["call_limit"] == 137
        assert run["commands"] == checks() and run["sandbox_snapshot"]["configuration"]["command_limit"] == 100
        service.recoveries.journal._append.assert_awaited_once()
    finally:
        CURRENT_PRINCIPAL.reset(token)


async def test_large_candidate_still_refuses_insufficient_existing_final_capacity(grants):
    service, run, _ = grants
    service.recoveries.journal.sandbox_executions.return_value = [{}] * 50
    with pytest.raises(ValueError, match="Remaining shared command budget"):
        await service._candidate(run, "1" * 32)
    service.recoveries.journal._append.assert_not_awaited()


@pytest.mark.parametrize("reason", ["scope", "history"])
async def test_large_grant_preserves_existing_scope_and_lifetime_limits(grants, monkeypatch, reason):
    service, run, actor = grants
    monkeypatch.setattr(service, "actor_snapshot", lambda _: {})
    candidate = await service._candidate(run, "1" * 32)
    body = CodingCheckGrantInput(
        request_id="2" * 32,
        recovery_request_id="1" * 32,
        candidate_digest=candidate["digest"],
        expected_revision=0,
        command_indices=[51] if reason == "scope" else [50],
        call_limit=30,
        valid_seconds=300,
        reason="Explicit scoped review",
    )
    if reason == "history":
        service.recoveries._entries.return_value = [
            {"kind": "coding_check_grant", "idempotency_key": f"{index:032x}"} for index in range(20)
        ]
    before = copy.deepcopy(run)
    token = CURRENT_PRINCIPAL.set(actor)
    try:
        with pytest.raises(ValueError, match="retained configured scope|grant history is full"):
            await service.grant(run["id"], body)
    finally:
        CURRENT_PRINCIPAL.reset(token)
    assert run == before
    service.recoveries.journal._append.assert_not_awaited()
    service.recoveries.store.run_records.save.assert_not_awaited()


def handoff_run():
    return {
        "id": "a" * 32,
        "status": "paused",
        "worker_request": {"id": "verification-request"},
        "verification_handoff": {
            "version": 1,
            "id": "b" * 32,
            "request_id": "verification-request",
            "status": "completed",
            "commands_count": 51,
            "completed_commands": 51,
            "failed_commands": 2,
        },
    }


def test_factual_communication_keeps_full_governed_check_batch():
    assert communication_facts(handoff_run())["check_batch"] == {
        "source": "governed_handoff",
        "provenance": "retained",
        "total": 51,
        "recorded": 51,
        "failed": 2,
    }


@pytest.mark.parametrize("value", [True, "51", -1])
def test_full_check_facts_still_refuse_invalid_count_types_and_values(value):
    run = handoff_run()
    run["verification_handoff"]["commands_count"] = value
    assert "check_batch" not in communication_facts(run)


@pytest.mark.parametrize("field,value", [("completed_commands", 52), ("failed_commands", 52)])
def test_full_check_facts_refuse_inconsistent_completion_counts(field, value):
    run = handoff_run()
    run["verification_handoff"][field] = value
    assert "check_batch" not in communication_facts(run)
