# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Explicit final allowance over the real recovery service and journal (host storage only)."""

import copy
from uuid import uuid4

import pytest
from pydantic import ValidationError
from test_provider_recovery_service import configure_executable
from test_provider_recovery_service import recovery as recovery

from firefly_forge.provider_recovery_models import ProviderProcessOutcome


async def fourth_failure(e, *, known=True):
    for key in ("3" * 32, "4" * 32):
        await e.journal._append(e.memory.id.hex, "implement", key, "intent", {})
    e.memory.task.attempt = 4
    e.memory.task.data = {"idempotency_key": "4" * 32}
    e.memory.run.data["attempt"] = 4
    await e.journal._append(
        e.memory.id.hex,
        "implement",
        "4" * 32 + ":provider",
        "provider_dispatch_intent",
        {"version": 1, "task_key": "4" * 32, "attempt": 4},
    )
    await e.service.record_process(
        e.memory.id.hex,
        "4" * 32,
        ProviderProcessOutcome(exit_code=-25, leader_waited=True, streams_drained=True, group_absent=known),
    )


def request(candidate, **changes):
    from firefly_forge.provider_recovery_allowances import ProviderRecoveryAllowanceInput

    return ProviderRecoveryAllowanceInput.model_validate(
        dict(
            request_id="a" * 32,
            candidate_digest=candidate["digest"],
            native_process_digest=candidate["native_process_digest"],
            agent="codex",
            reason="Reviewed a finite file-limit signal mitigation in controlled local tests.",
            mitigation_evidence_digest="e" * 64,
            adopt_current_executable=True,
        )
        | changes
    )


def allowances(e):
    from firefly_forge.provider_recovery_allowances import ProviderRecoveryAllowances

    configure_executable(e, e.workspace.parent.parent)
    return ProviderRecoveryAllowances(e.service)


async def test_final_allowance_reserves_once_preserves_prefix_and_legacy_budget(recovery):
    e = recovery
    await fourth_failure(e)
    before = copy.deepcopy(e.memory.run.data)
    prefix = await e.service._entries(e.memory.id.hex)
    with pytest.raises(ValueError, match="four-dispatch"):
        await e.service.candidate(e.memory.id.hex, "codex")
    service = allowances(e)
    candidate = await service.candidate(e.memory.id.hex, "codex", adopt_current_executable=True)
    assert candidate["next_attempt"] == 5 and candidate["dispatches_used"] == 4
    body = request(candidate)
    receipt = await service.reserve(e.memory.id.hex, body)
    assert receipt == await service.reserve(e.memory.id.hex, body)
    assert receipt == await service.get(e.memory.id.hex, body.request_id)
    entries = await e.service._entries(e.memory.id.hex)
    assert entries[: len(prefix)] == prefix
    assert sum(v["kind"] == "provider_recovery_allowance" for v in entries) == 1
    assert sum(v["kind"] == "provider_recovery_admission" for v in entries) == 1
    assert e.memory.task.attempt == 5 and e.memory.task.status == "queued"
    for key in before:
        if key not in {"status", "updated_at", "provider_recovery", "resume_existing"}:
            assert e.memory.run.data[key] == before[key]
    for changed in (dict(request_id=uuid4().hex), dict(reason="Different mitigation")):
        with pytest.raises(ValueError):
            await service.reserve(e.memory.id.hex, request(candidate, **changed))
    with pytest.raises(ValueError):
        await service.candidate(e.memory.id.hex, "codex", adopt_current_executable=True)
    await e.service.begin(e.memory.id.hex)
    key = e.memory.task.data["idempotency_key"]
    await e.service.dispatch(e.memory.id.hex, key)
    with pytest.raises(ValueError):
        await e.service.dispatch(e.memory.id.hex, key)
    e.memory.run.status = e.memory.run.data["status"] = "developing"
    await e.service.assert_launch(e.memory.id.hex, key)
    await e.service.assert_result(e.memory.id.hex, key)
    await e.service.record_process(
        e.memory.id.hex,
        key,
        ProviderProcessOutcome(exit_code=1, leader_waited=True, streams_drained=True, group_absent=True),
    )
    e.memory.run.status = e.memory.run.data["status"] = "failed"
    with pytest.raises(ValueError):
        await service.candidate(e.memory.id.hex, "codex", adopt_current_executable=True)
    assert receipt == await service.reserve(e.memory.id.hex, body)
    assert sum(v["kind"] == "provider_recovery_allowance" for v in await e.service._entries(e.memory.id.hex)) == 1


@pytest.mark.parametrize("mutation", ["unknown", "missing_native", "third_attempt", "source", "tail"])
async def test_allowance_denies_unknown_legacy_stale_and_nonfinal_failures(recovery, mutation):
    e = recovery
    await fourth_failure(e, known=mutation != "unknown")
    if mutation == "missing_native":
        e.memory.rows.pop()
    if mutation == "third_attempt":
        e.memory.task.attempt = 3
    service = allowances(e)
    if mutation in {"source", "tail"}:
        candidate = await service.candidate(e.memory.id.hex, "codex", adopt_current_executable=True)
        if mutation == "source":
            (e.workspace / "partial.py").write_text("CHANGED = True\n")
        else:
            await e.journal._append(e.memory.id.hex, "implement", "new", "note", {})
        with pytest.raises(ValueError):
            await service.reserve(e.memory.id.hex, request(candidate))
    else:
        with pytest.raises(ValueError):
            await service.candidate(e.memory.id.hex, "codex", adopt_current_executable=True)
    assert not any(v.kind == "provider_recovery_allowance" for v in e.memory.rows)


@pytest.mark.parametrize("boundary", ["begin", "dispatch", "launch", "result"])
async def test_fifth_attempt_requires_its_exact_sealed_native_allowance_at_every_boundary(recovery, boundary):
    e = recovery
    await fourth_failure(e)
    service = allowances(e)
    await service.reserve(
        e.memory.id.hex, request(await service.candidate(e.memory.id.hex, "codex", adopt_current_executable=True))
    )
    key = e.memory.task.data["idempotency_key"]
    if boundary in {"launch", "result"}:
        await e.service.dispatch(e.memory.id.hex, key)
        e.memory.run.status = e.memory.run.data["status"] = "developing"
    e.memory.run.data["provider_recovery"]["allowance_digest"] = "0" * 64
    with pytest.raises(ValueError, match="allowance"):
        if boundary == "begin":
            await e.service.begin(e.memory.id.hex)
        elif boundary == "dispatch":
            await e.service.dispatch(e.memory.id.hex, key)
        elif boundary == "launch":
            await e.service.assert_launch(e.memory.id.hex, key)
        else:
            await e.service.assert_result(e.memory.id.hex, key)


async def test_allowance_input_never_accepts_legacy_proof_or_budget_overrides(recovery):
    await fourth_failure(recovery)
    candidate = await allowances(recovery).candidate(recovery.memory.id.hex, "codex", adopt_current_executable=True)
    for change in (
        dict(reason=" "),
        dict(mitigation_evidence_digest="bad"),
        dict(legacy_attestation={}),
        dict(next_attempt=6),
        dict(repair_budget=1),
        dict(agent="bridge"),
    ):
        with pytest.raises(ValidationError):
            request(candidate, **change)


def feedback_payload(link):
    return dict(
        id="final-feedback",
        role_id="implementer",
        task_id="implement",
        attempt=5,
        disposition="completed",
        summary="Implementation reported; verification is a separate stage.",
        evidence_paths=[],
        request=None,
        **link,
    )


async def test_native_fifth_feedback_requires_real_consumed_allowance_and_legacy_stays_strict(recovery):
    from firefly_forge.agent_roles import WorkerFeedback

    e = recovery
    await fourth_failure(e)
    service = allowances(e)
    body = request(await service.candidate(e.memory.id.hex, "codex", adopt_current_executable=True))
    await service.reserve(e.memory.id.hex, body)
    state = e.memory.run.data["provider_recovery"]
    value = feedback_payload(
        dict(
            protocol="native-provider-recovery-allowance-v1",
            allowance_id=body.request_id,
            allowance_digest=state["allowance_digest"],
        )
    )
    with pytest.raises(ValidationError):
        WorkerFeedback.model_validate(value)
    with pytest.raises(ValueError):
        await e.journal.worker_feedback(e.memory.id.hex, value)
    await e.service.dispatch(e.memory.id.hex, state["task_key"])
    e.memory.run.status = e.memory.run.data["status"] = "developing"
    for change in (dict(allowance_digest="0" * 64), dict(allowance_id="f" * 32), dict(attempt=6)):
        with pytest.raises(ValueError):
            await e.journal.worker_feedback(e.memory.id.hex, value | change)
    recorded = await e.journal.worker_feedback(e.memory.id.hex, value)
    assert recorded["payload"] == value
    assert await e.journal.worker_feedback(e.memory.id.hex, value) == recorded
    assert sum(v.kind == "worker_feedback" for v in e.memory.rows) == 1


async def test_legacy_receipt_route_refuses_new_protocol_without_widening(recovery):
    e = recovery
    await fourth_failure(e)
    service = allowances(e)
    body = request(await service.candidate(e.memory.id.hex, "codex", adopt_current_executable=True))
    await service.reserve(e.memory.id.hex, body)
    with pytest.raises(ValueError, match="/provider-recoveries/allowances/"):
        await e.service.get(e.memory.id.hex, body.request_id)


async def test_final_allowance_requires_explicit_current_cli_and_rejects_binary_replacement(recovery):
    e = recovery
    await fourth_failure(e)
    service = allowances(e)
    for choice in (False, 1, None):
        with pytest.raises(ValueError):
            await service.candidate(e.memory.id.hex, "codex", adopt_current_executable=choice)
    candidate = await service.candidate(e.memory.id.hex, "codex", adopt_current_executable=True)
    for change in (dict(adopt_current_executable=False), dict(adopt_current_executable=1)):
        with pytest.raises(ValidationError):
            request(candidate, **change)
    body = request(candidate)
    with pytest.raises(ValidationError):
        type(body).model_validate(body.model_dump(exclude={"adopt_current_executable"}))
    await service.reserve(e.memory.id.hex, body)
    key = e.memory.task.data["idempotency_key"]
    await e.service.dispatch(e.memory.id.hex, key)
    e.memory.run.status = e.memory.run.data["status"] = "developing"
    (e.workspace.parent.parent / "official-cli").write_text("#!/bin/sh\nexit 7\n")
    with pytest.raises(ValueError, match="executable changed"):
        await e.service.assert_launch(e.memory.id.hex, key)


async def test_final_allowance_current_revocation_blocks_dispatch_and_receipt_readback(recovery, monkeypatch):
    from firefly_forge.project_access import AccessDenied, ProjectAccess

    e = recovery
    await fourth_failure(e)
    service = allowances(e)
    body = request(await service.candidate(e.memory.id.hex, "codex", adopt_current_executable=True))
    await service.reserve(e.memory.id.hex, body)
    prefix = await e.service._entries(e.memory.id.hex)

    async def denied(*args, **kwargs):
        return False

    monkeypatch.setattr(ProjectAccess, "authorize_actor", denied)
    with pytest.raises(AccessDenied):
        await e.service.dispatch(e.memory.id.hex, e.memory.task.data["idempotency_key"])
    with pytest.raises(AccessDenied):
        await service.get(e.memory.id.hex, body.request_id)
    assert await e.service._entries(e.memory.id.hex) == prefix


async def test_final_feedback_replays_exact_receipt_after_completion_but_refuses_new_late_message(recovery):
    e = recovery
    await fourth_failure(e)
    service = allowances(e)
    body = request(await service.candidate(e.memory.id.hex, "codex", adopt_current_executable=True))
    await service.reserve(e.memory.id.hex, body)
    state = e.memory.run.data["provider_recovery"]
    await e.service.dispatch(e.memory.id.hex, state["task_key"])
    e.memory.run.status = e.memory.run.data["status"] = "developing"
    value = feedback_payload(
        dict(
            protocol="native-provider-recovery-allowance-v1",
            allowance_id=body.request_id,
            allowance_digest=state["allowance_digest"],
        )
    )
    first = await e.journal.worker_feedback(e.memory.id.hex, value)
    e.memory.task.status = "done"
    e.memory.run.status = e.memory.run.data["status"] = "validating"
    assert await e.journal.worker_feedback(e.memory.id.hex, value) == first
    with pytest.raises(ValueError, match="active implementation"):
        await e.journal.worker_feedback(e.memory.id.hex, value | {"id": "late-final-feedback"})
    assert sum(v.kind == "worker_feedback" for v in e.memory.rows) == 1


async def test_final_allowance_native_repository_reads_stay_inside_the_store_transaction(recovery):
    from contextlib import asynccontextmanager

    e = recovery
    await fourth_failure(e)
    service = allowances(e)
    await service.reserve(
        e.memory.id.hex, request(await service.candidate(e.memory.id.hex, "codex", adopt_current_executable=True))
    )
    original_unit, original_task = e.memory._unit, e.journal._task
    depth = 0

    @asynccontextmanager
    async def unit():
        nonlocal depth
        depth += 1
        try:
            async with original_unit() as session:
                yield session
        finally:
            depth -= 1

    async def native_task(*args):
        if not depth:
            raise AssertionError("Native repository access requires a transaction")
        return await original_task(*args)

    e.memory._unit, e.journal._task = unit, native_task
    dispatch = await e.service.begin(e.memory.id.hex)
    assert dispatch["attempt"] == 5 and dispatch["disposition"] == "execute"
    assert depth == 0
