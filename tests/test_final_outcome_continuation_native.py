# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Controlled PostgreSQL final-outcome acceptance; no retained CRM or provider effects."""

import asyncio
import copy
from uuid import uuid4

import pytest
from checkpoint_continuation_native_fixtures import native_checkpoint_cut
from final_outcome_native_fixtures import enable_final, final_input, seed_consumed_final

from firefly_forge.agent_checkpoints import AgentCheckpoints
from firefly_forge.final_outcome_continuations import ADMITTED, HANDOFF
from firefly_forge.invalid_read_dispatch_guard import assert_generic_dispatch_allowed
from firefly_forge.invalid_read_lease import ContinuityLeasePolicyV1, write_policy
from firefly_forge.native_terminal_completion_models import validate_native_receipt
from firefly_forge.verification_handoffs import VerificationHandoffs, automatic_candidate

pytest_plugins = ["test_invalid_read_continuity_native"]


async def test_native_consumed_final_acceptance_releases_existing_verification_without_replay(
    native_continuity, monkeypatch
):
    c = native_continuity
    # Select this command before the first action: it is part of the frozen native scope.
    await c.store.update(c.run_id, commands=[dict(argv=["approved-final-check"], timeout=2)])
    failed = await seed_consumed_final(c, monkeypatch)
    before = await native_checkpoint_cut(c, failed["id"])
    old_run = copy.deepcopy(await c.store.get_run(c.run_id))
    enable_final(c)
    service = c.service.final_outcomes
    candidate = await service.candidate(c.run_id, failed["id"])
    assert candidate["admission"] == "READY", candidate["holds"]
    assert candidate["outcome"] == "blocked" and candidate["followup_kind"] == "verification"
    assert candidate["admission_window_purpose"] == "claim_only"
    await service.request(c.run_id, failed["id"], uuid4().hex, final_input(candidate))

    async def forbidden(*args, **kwargs):
        raise AssertionError("Known final acceptance cannot replay a model or consumption")

    monkeypatch.setattr(c.engine.provider_factory("bridge"), "execute", forbidden)
    monkeypatch.setattr(AgentCheckpoints, "_consume", forbidden)
    await c.service.execute(failed["id"])
    accepted = await native_checkpoint_cut(c, failed["id"])
    current = await c.store.get_run(c.run_id)
    assert accepted["ticket"]["status"] == "completed", accepted["ticket"].get("result")
    assert current["status"] == "paused" and current["worker_feedback"]["disposition"] == "blocked"
    assert current["worker_request"]["kind"] == "verification" and current["worker_request"]["status"] == "pending"
    assert current["attempt"] == current["implementation_rounds"] == 2
    assert current["usage"] == old_run["usage"] and current["repair_budget"] == old_run["repair_budget"]
    assert current["sandbox_snapshot"] == old_run["sandbox_snapshot"]
    assert accepted["count"] == 1 and accepted["commands"] == before["commands"]
    assert accepted["journal"][: len(before["journal"])] == before["journal"]
    assert [e for e in accepted["journal"] if e["kind"] == "agent_consume"] == [
        e for e in before["journal"] if e["kind"] == "agent_consume"
    ]
    terminal = [e for e in accepted["journal"] if e["kind"] == "terminal_completion_receipt"]
    assert len(terminal) == 1
    validate_native_receipt(terminal[0], accepted["journal"])
    closures = terminal[0]["payload"]["proof"]["completions"]
    assert any(e.get("custody") == "known_final_outcome_v1" for e in closures)
    await assert_generic_dispatch_allowed(c.store, c.journal, current)
    assert automatic_candidate(current), "The accepted blocked report must reach the real verification handoff"
    handoff = VerificationHandoffs(c.store, c.journal, c.engine)
    await handoff.execute(c.run_id)
    checked = await c.store.get_run(c.run_id)
    assert checked["status"] == "queued" and checked["verification_handoff"]["status"] == "completed", checked
    assert checked["worker_request"]["response_source"] == "factory_verification"
    commands = await c.journal.sandbox_executions(c.run_id)
    assert len(commands) == len(before["commands"]) + 1
    assert c.command_calls[-1] == ["approved-final-check"]
    assert checked["implementation_rounds"] == 2 and checked["usage"] == old_run["usage"]
    assert len(c.posts) == 4 and c.checkpoint_writes == [("existing.py", "VALUE = 2\n")]
    assert c.checkpoint_reads == ["existing.py"]
    await c.service.execute(failed["id"])
    assert await c.journal.sandbox_executions(c.run_id) == commands and len(c.posts) == 4


async def test_native_final_admission_atomic_rollback_and_concurrent_idempotency(native_continuity, monkeypatch):
    c = native_continuity
    failed = await seed_consumed_final(c, monkeypatch)
    enable_final(c)
    service = c.service.final_outcomes
    body, action = final_input(await service.candidate(c.run_id, failed["id"])), uuid4().hex
    before = await native_checkpoint_cut(c, failed["id"])
    event = c.requests._event

    async def rollback(row, kind, evidence):
        await event(row, kind, evidence)
        if kind == ADMITTED:
            raise RuntimeError("Fictional rollback after request event")

    with monkeypatch.context() as patch:
        patch.setattr(c.requests, "_event", rollback)
        with pytest.raises(RuntimeError, match="rollback"):
            await service.request(c.run_id, failed["id"], action, body)
    assert await native_checkpoint_cut(c, failed["id"]) == before
    prepare = service._prepare
    gate, count = asyncio.Event(), 0

    async def together(*args):
        nonlocal count
        value = await prepare(*args)
        count += 1
        if count == 3:
            gate.set()
        await asyncio.wait_for(gate.wait(), 20)
        return value

    with monkeypatch.context() as patch:
        patch.setattr(service, "_prepare", together)
        receipts = await asyncio.gather(*(service.request(c.run_id, failed["id"], action, body) for _ in range(3)))
    assert receipts == receipts[:1] * 3
    after = await native_checkpoint_cut(c, failed["id"])
    assert after["count"] == 1 and after["journal"][:-1] == before["journal"]
    assert after["journal"][-1]["kind"] == HANDOFF
    assert after["events"][:-1] == before["events"] and after["events"][-1]["kind"] == ADMITTED
    for spent in (failed["id"], uuid4().hex):
        with pytest.raises(ValueError):
            await service.request(c.run_id, failed["id"], spent, body)
    with pytest.raises(ValueError):
        await service.request(c.run_id, failed["id"], action, body.model_copy(update={"valid_for_seconds": 1}))
    assert await native_checkpoint_cut(c, failed["id"]) == after and len(c.posts) == 4


async def test_native_final_claim_rechecks_revoked_policy_without_new_work(native_continuity, monkeypatch):
    c = native_continuity
    failed = await seed_consumed_final(c, monkeypatch)
    enable_final(c)
    service = c.service.final_outcomes
    body = final_input(await service.candidate(c.run_id, failed["id"]))
    await service.request(c.run_id, failed["id"], uuid4().hex, body)
    before = await native_checkpoint_cut(c, failed["id"])
    write_policy(c.store.configuration, ContinuityLeasePolicyV1(version=1, enabled=True))
    await c.service.execute(failed["id"])
    after = await native_checkpoint_cut(c, failed["id"])
    assert after["ticket"]["status"] == "failed"
    assert after["journal"] == before["journal"] and after["commands"] == before["commands"]
    assert len(c.posts) == 4 and not any(e["kind"] == "terminal_completion_receipt" for e in after["journal"])
