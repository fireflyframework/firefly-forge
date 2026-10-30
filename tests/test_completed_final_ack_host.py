# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Generated command custody; the completed-final validator is an isolated dependency seam."""

import copy
from contextvars import ContextVar
from types import SimpleNamespace
from uuid import uuid4

import pytest
from test_sandbox_command_ordinals_host import command_case  # noqa: F401
from test_unknown_command_acknowledgement_host import adopt_and_begin, consent, service, unknown_case  # noqa: F401

from firefly_forge.completed_final_epoch import PreparedCompletedFinal
from firefly_forge.journal import digest
from firefly_forge.process import ProcessResult
from firefly_forge.sandbox_command_slots import _canonical


@pytest.fixture
async def final_ack(unknown_case, monkeypatch):  # noqa: F811
    c = unknown_case
    api = service(c, monkeypatch)
    candidate = await api.candidate(c.run_id, c.unknown.id.hex, agent="agentic", adoption_request_id=uuid4().hex)
    await api.request(c.run_id, c.unknown.id.hex, uuid4().hex, consent(candidate))
    intent = await adopt_and_begin(c, candidate, monkeypatch)

    async def red(*_args):
        return ProcessResult(["fixture-check"], 1, "Controlled prior regression")

    c.backend.run = red
    await c.fresh().execute(c.run_id, c.workspace, c.workspace, ["fixture-check"], 2, "implement", ordinal=3)
    await c.journal.verification_command(c.run_id, intent, 0, check_current=lambda _run: None)
    await c.journal.verification_finish(c.run_id, intent, check_current=lambda _run: None)
    await c.journal._append(
        c.run_id,
        "handoff",
        uuid4().hex,
        "handoff",
        {
            "automatic": True,
            "verification_handoff_id": intent["id"],
            "agent": "agentic",
            "config_revision": c.store.run.data["config_revision"],
        },
    )
    task = c.store.tasks.rows[0]
    task.status, task.attempt = "queued", 2
    c.store.run.data.update(attempt=2, implementation_pending=True)

    async def plan(*_args):
        return {}

    monkeypatch.setattr(c.journal, "ensure_plan", plan)
    await c.journal.intent(c.run_id, "implement", 2, {"config_revision": c.store.run.data["config_revision"]})
    await c.journal.verification_provider_admission(c.run_id)
    c.store.run.data.update(
        status="failed",
        implementation_pending=False,
        implementation_rounds=2,
        usage={"requests": 2, "input_tokens": 3, "output_tokens": 4},
    )
    task.status = "needs_reconciliation"
    frozen = await c.store.get_run(c.run_id)
    prefix = await c.journal.entries(c.run_id)
    witness = await c.journal._append(
        c.run_id,
        "implement",
        uuid4().hex,
        "final_outcome_continuation",
        {
            "run": frozen,
            "task": {
                "id": "implement",
                "attempt": 2,
                "status": task.status,
                "idempotency_key": task.data["idempotency_key"],
            },
            "candidate": {"attempt": 2, "journal_digest": digest({"entries": prefix})},
        },
    )
    feedback = await c.journal._append(
        c.run_id,
        "implement",
        uuid4().hex,
        "worker_feedback",
        {"id": "new-review", "attempt": 2, "request": {"id": "new-review"}},
    )
    terminal = await c.journal._append(
        c.run_id,
        "implement",
        uuid4().hex,
        "terminal_completion_receipt",
        {
            "producer": "native_worker_feedback",
            "proof": {
                "task_attempt": 2,
                "task_key": task.data["idempotency_key"],
                "acceptance_record_digest": feedback["digest"],
            },
        },
    )
    c.store.run.status = "paused"
    c.store.run.data.update(
        status="paused",
        verification_handoff={},
        worker_request={
            "id": "new-review",
            "kind": "verification",
            "status": "pending",
            "task_id": "implement",
            "attempt": 2,
        },
        worker_feedback=feedback["payload"],
    )
    task.status = "paused"
    c.current_checks = 0

    async def current():
        c.current_checks += 1
        if not c.allowed:
            raise ValueError("Completed final authority revoked")

    from firefly_forge.checkpoint_mode_handoff_custody import _validate

    check_entries = await c.journal.entries(c.run_id)
    check_values = {r.id.hex: _canonical(r, check_entries, c.run_id) for r in c.store.sandboxes.rows}
    _validate(frozen, prefix, c.store.sandboxes.rows, check_values)
    c.prepared_final = PreparedCompletedFinal(witness, terminal, {"status": "completed"}, None, current)
    import firefly_forge.unknown_command_proof as proof

    variable = ContextVar("fictional_validated_completed_final", default=c.prepared_final)
    monkeypatch.setattr(proof, "_COMPLETED_FINAL", variable, raising=False)
    c.backend.run = c.successful_backend
    return c


async def test_accepted_final_ack_allows_current_readonly_git_boundary_without_fake_task_receipt(final_ack):
    from firefly_forge.unknown_command_proof import assert_continuation_current

    c = final_ack
    before = copy.deepcopy((c.store.run.data, c.unknown.data, [r.data for r in c.store.records.rows]))
    assert await assert_continuation_current(c.journal, c.run_id) == [c.unknown.id.hex]
    assert (c.store.run.data, c.unknown.data, [r.data for r in c.store.records.rows]) == before
    assert c.store.tasks.rows[0].status == "paused" and c.current_checks >= 2


@pytest.mark.parametrize("change", ["grant", "usage_type", "witness", "unknown_row", "permission", "new_coding"])
async def test_completed_final_ack_refuses_changed_authority_or_custody(final_ack, change):
    from firefly_forge.unknown_command_proof import assert_continuation_current

    c = final_ack
    task_id = None
    if change == "grant":
        c.store.run.data["repair_budget"] = 17
    elif change == "usage_type":
        c.store.run.data["usage"]["requests"] = 2.0
    elif change == "witness":
        c.prepared_final.record["payload"]["candidate"]["journal_digest"] = "f" * 64
    elif change == "unknown_row":
        c.unknown.data["effects"]["start"] = {"status": "done", "evidence": {"exit_code": 0}}
    elif change == "permission":
        c.allowed = False
    else:
        task_id = "implement"
    before = len(c.calls)
    with pytest.raises((ValueError, RuntimeError)):
        await assert_continuation_current(c.journal, c.run_id, task_id)
    assert len(c.calls) == before


async def test_nested_none_scope_retains_context_and_resets_on_exception(final_ack):
    from firefly_forge.unknown_command_proof import _COMPLETED_FINAL, completed_final_command_scope

    previous = _COMPLETED_FINAL.get()
    other = copy.copy(final_ack.prepared_final)
    with (
        pytest.raises(RuntimeError, match="controlled"),
        completed_final_command_scope(other),
        completed_final_command_scope(None),
    ):
        assert _COMPLETED_FINAL.get() is other
        raise RuntimeError("controlled")
    assert _COMPLETED_FINAL.get() is previous


async def test_later_native_handoff_next_round_and_qa_preserve_old_unknown(final_ack):
    from firefly_forge.tool_broker import FactoryToolPolicyInput
    from firefly_forge.unknown_command_proof import assert_continuation_current
    from firefly_forge.verification_handoffs import verification_intent
    from firefly_forge.verification_models import VerificationAdoption, check_verification_cut

    c = final_ack
    old = copy.deepcopy((c.unknown.data, c.unknown.status))
    c.store.configuration._write(
        c.store.configuration.directory / "tool-policy.json",
        {
            "revision": uuid4().hex,
            "settings": FactoryToolPolicyInput(
                automatic_verification={"enabled": True, "max_continuations": 6}
            ).model_dump(),
        },
    )
    candidate = await c.handoffs.candidates(c.run_id, "agentic")
    body = VerificationAdoption.model_validate(
        {
            "request_id": uuid4().hex,
            **{
                k: candidate[k]
                for k in (
                    "pending_request_digest",
                    "source_digest",
                    "commands_digest",
                    "policy_digest",
                    "provider_digest",
                    "agent",
                )
            },
        }
    )
    await c.handoffs.adopt(c.run_id, body)
    intent = verification_intent(
        c.store,
        await c.store.get_run(c.run_id),
        c.workspace,
        await c.journal.entries(c.run_id),
        await c.journal.sandbox_executions(c.run_id),
    )
    await c.journal.verification_begin(
        c.run_id, intent, check_current=lambda run: check_verification_cut(c.store, run, intent)
    )
    result = await c.fresh().execute(
        c.run_id, c.workspace, c.workspace, ["fixture-check"], 2, "implement", ordinal=intent["ordinals"][0]
    )
    assert result.exit_code == 0
    await c.journal.verification_command(
        c.run_id, intent, 0, check_current=lambda run: check_verification_cut(c.store, run, intent)
    )
    await c.journal.verification_finish(
        c.run_id, intent, check_current=lambda run: check_verification_cut(c.store, run, intent)
    )
    await c.journal._append(
        c.run_id,
        "handoff",
        uuid4().hex,
        "handoff",
        {
            "automatic": True,
            "verification_handoff_id": intent["id"],
            "agent": "agentic",
            "config_revision": c.store.run.data["config_revision"],
        },
    )
    task = c.store.tasks.rows[0]
    task.status, task.attempt = "queued", 3
    c.store.run.data.update(status="developing", attempt=3, implementation_pending=True)
    await c.journal.intent(c.run_id, "implement", 3, {"config_revision": c.store.run.data["config_revision"]})
    await c.journal.verification_provider_admission(c.run_id)
    result = await c.fresh().execute(c.run_id, c.workspace, c.workspace, ["new-authorized-round"], 2, "implement")
    assert result.exit_code == 0
    await c.journal.receipt(c.run_id, task.data["idempotency_key"], {"implementation_round": 3})
    c.store.run.data.update(implementation_rounds=3, implementation_pending=False)
    verify = SimpleNamespace(id=uuid4(), run_id=c.store.run.id, task_id="verify", attempt=1, status="queued", data={})
    c.store.tasks.rows.append(verify)
    await c.journal.intent(c.run_id, "verify", 1)
    result = await c.fresh().execute(c.run_id, c.workspace, c.workspace, ["new-native-qa"], 2, "verify")
    assert result.exit_code == 0
    assert await assert_continuation_current(c.journal, c.run_id) == [c.unknown.id.hex]
    assert (c.unknown.data, c.unknown.status) == old
    assert len(c.store.sandboxes.rows) == 6 and c.store.run.data["attempt"] == 3
    assert c.store.run.data["usage"] == {"requests": 2, "input_tokens": 3, "output_tokens": 4}


def phase_vector():
    from uuid import NAMESPACE_URL, UUID, uuid5

    run_id = uuid4().hex
    key = uuid5(NAMESPACE_URL, f"forge:{run_id}:implement:2").hex
    task = SimpleNamespace(
        task_id="implement", run_id=UUID(run_id), attempt=2, status="running", data={"idempotency_key": key}
    )
    run = {"id": run_id, "attempt": 2, "implementation_rounds": 1, "implementation_pending": True, "repair_budget": 0}
    entries = [
        {"kind": "verification_handoff_outcome", "payload": {"id": "known", "status": "completed"}},
        {"kind": "handoff", "payload": {"verification_handoff_id": "known", "automatic": True}},
        {
            "kind": "worker_response",
            "payload": {
                "responding_actor": {
                    "handoff_id": "known",
                    "version": 4,
                    "authentication_method": "factory_verification",
                }
            },
        },
        {"kind": "intent", "idempotency_key": key, "run_id": run_id, "task_id": "implement", "sequence": 10},
    ]
    return run, task, entries


@pytest.mark.parametrize("change", ["task_key", "task_run", "intent_run", "intent_task"])
def test_later_phase_requires_exact_native_task_key_and_columns(change):
    from firefly_forge.unknown_command_proof import _completed_final_phase

    run, task, entries = phase_vector()
    assert _completed_final_phase(run, [task], entries, 2, "implement")
    if change == "task_key":
        task.data["idempotency_key"] = uuid4().hex
    elif change == "task_run":
        task.run_id = uuid4()
    elif change == "intent_run":
        entries[-1]["run_id"] = uuid4().hex
    else:
        entries[-1]["task_id"] = "verify"
    assert not _completed_final_phase(run, [task], entries, 2, "implement")


@pytest.mark.parametrize("change", ["authentication_method", "version_type"])
def test_only_factory_answered_grants_count_toward_later_attempts(change):
    from uuid import NAMESPACE_URL, uuid5

    from firefly_forge.unknown_command_proof import _completed_final_phase

    run, task, entries = phase_vector()
    run.update(attempt=3, implementation_rounds=2)
    task.attempt = 3
    key = uuid5(NAMESPACE_URL, f"forge:{run['id']}:implement:3").hex
    task.data["idempotency_key"] = key
    entries[-1].update(idempotency_key=key)
    entries.extend(
        [
            {"kind": "verification_handoff_outcome", "payload": {"id": "other", "status": "completed"}},
            {"kind": "handoff", "payload": {"verification_handoff_id": "other", "automatic": True}},
            {
                "kind": "worker_response",
                "payload": {
                    "responding_actor": {
                        "handoff_id": "other",
                        "version": 4,
                        "authentication_method": "factory_verification",
                    }
                },
            },
        ]
    )
    assert _completed_final_phase(run, [task], entries, 3, "implement")
    actor = entries[-1]["payload"]["responding_actor"]
    actor["authentication_method" if change == "authentication_method" else "version"] = (
        "foreign" if change == "authentication_method" else 4.0
    )
    assert not _completed_final_phase(run, [task], entries, 3, "implement")


async def test_writer_uses_native_projection_without_running_offline_preparation(final_ack, monkeypatch):
    from dataclasses import replace

    import pyfly.data.transaction as transaction

    from firefly_forge.bridge_completions import _outside_unit
    from firefly_forge.unknown_command_proof import _COMPLETED_FINAL, assert_continuation_current

    c = final_ack
    active = False
    checks = 0
    monkeypatch.setattr(transaction, "current_unit_of_work", lambda: object() if active else None)

    async def current():
        nonlocal checks
        _outside_unit()
        checks += 1

    prepared = replace(c.prepared_final, _current=current)
    token = _COMPLETED_FINAL.set(prepared)
    try:
        await prepared.current()
        active = True
        assert await assert_continuation_current(c.journal, c.run_id) == [c.unknown.id.hex]
        active = False
        await prepared.current()
    finally:
        _COMPLETED_FINAL.reset(token)
    assert checks == 2
