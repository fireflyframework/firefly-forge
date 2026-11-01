# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Real ACK WAL and completed custody; accepted reconciliation is a typed read seam."""

import copy
from types import SimpleNamespace
from uuid import UUID

import pytest
from linked_verification_fixtures import candidate as candidate_fixture

from firefly_forge.consumed_terminal_completions import PreparedAcceptedReconciledHandoff
from firefly_forge.execution_requests import digest as event_digest
from firefly_forge.git_deliveries import GitDeliveries
from firefly_forge.journal import digest
from firefly_forge.linked_verification_models import LinkedVerificationInputV1
from firefly_forge.linked_verifications import LinkedVerifications
from firefly_forge.unknown_command_proof import ACKNOWLEDGEMENT, assert_continuation_current

pytest_plugins = ["test_completed_final_ack_host"]


def closed_failed_operation(c, run):
    operation = "9" * 32
    actor, identity = run["execution_actor"], {"issuer": c.principal.issuer, "subject": c.principal.subject}
    candidate = candidate_fixture(1)
    candidate.update(
        run_id=c.run_id,
        commands_digest=digest({"commands": run["commands"]}),
        run_digest=digest(run),
        actor_digest=digest({"actor": actor, "identity": identity}),
    )
    candidate["digest"] = digest({key: value for key, value in candidate.items() if key != "digest"})
    body = LinkedVerificationInputV1(
        version=1,
        request_id=operation,
        candidate_digest=candidate["digest"],
        acknowledge_all_fresh_checks=True,
        acknowledge_no_coding_or_repair=True,
        acknowledge_original_history_and_budget_preserved=True,
    ).model_dump(mode="json")
    data = dict(
        id=operation,
        run_id=c.run_id,
        kind="linked_verify",
        status="failed",
        request=body,
        request_digest=event_digest(body),
        candidate=candidate,
        actor=actor,
        identity=identity,
        parent_run=copy.deepcopy(run),
        commands=run["commands"],
        commands_digest=candidate["commands_digest"],
        effect_budget=1,
        inputs_digest=candidate["input_scope_digest"],
    )
    row = SimpleNamespace(id=UUID(operation), run_id=UUID(c.run_id), kind="linked_verify", status="failed", data=data)
    events = []

    def append(kind, evidence):
        value = dict(
            request_id=operation,
            sequence=len(events) + 1,
            previous_hash=events[-1]["digest"] if events else "0" * 64,
            kind=kind,
            evidence=evidence,
            created_at="2026-11-01T00:00:00Z",
        )
        value["digest"] = event_digest(value)
        events.append(value)
        return value

    append("linked_verification_admitted", {"request_digest": data["request_digest"]})
    append("linked_verification_claimed", {"request_digest": data["request_digest"]})
    intent = append(
        "linked_verification_command_intent",
        dict(ordinal=1, command_digest=event_digest(run["commands"][0]), inputs_digest=data["inputs_digest"]),
    )
    append(
        "linked_verification_command_result",
        dict(ordinal=1, intent_digest=intent["digest"], exit_code=1, output="Controlled known failed check"),
    )
    append("linked_verification_held", {"reason": "check_failed"})

    async def records(run_id, kind):
        assert run_id == UUID(c.run_id) and kind == "linked_verify"
        return [row]

    async def ledger(request_id, maximum):
        assert request_id == row.id and len(events) <= maximum
        return [SimpleNamespace(data=copy.deepcopy(event)) for event in events]

    c.store.execution_requests = SimpleNamespace(
        records=SimpleNamespace(for_run=records), journal=SimpleNamespace(linked_verification_events=ledger)
    )
    c.engine.linked_verifications = LinkedVerifications(c.store, c.journal, c.store.execution_requests, c.engine)
    c.linked_events = events


@pytest.fixture
async def accepted_git(final_ack, monkeypatch):
    c = final_ack
    historical = copy.deepcopy(c.prepared_final.record["payload"]["run"])
    prefix = copy.deepcopy((await c.journal.entries(c.run_id))[: c.prepared_final.record["sequence"] - 1])
    assert historical["verification_handoff"]["status"] == "completed"
    assert any(entry["kind"] == ACKNOWLEDGEMENT for entry in prefix)
    c.store.run.data.update(
        verification_handoff={**historical["verification_handoff"], "status": "held"},
        consumed_terminal_recovery={"state": "accepted", "request_id": "8" * 32},
    )
    c.store.tasks.rows[0].status = "done"
    live = await c.store.get_run(c.run_id)
    closed_failed_operation(c, live)
    native = copy.deepcopy(await c.journal.entries(c.run_id))
    c.git_current_checks = 0
    c.git_view = None
    c.git_mutation = None

    async def current():
        c.git_current_checks += 1
        await c.store.assert_ownership()
        if not c.allowed or await c.store.get_run(c.run_id) != live or await c.journal.entries(c.run_id) != native:
            raise ValueError("Controlled accepted reconciliation authority changed")

    async def prepared(store, journal, run, entries, terminal):
        assert store is c.store and journal is c.journal
        assert run == live and entries == native and terminal["kind"] == "terminal_completion_receipt"
        c.git_view = PreparedAcceptedReconciledHandoff(copy.deepcopy(historical), copy.deepcopy(prefix), current)
        if c.git_mutation is not None:
            c.git_mutation(c.git_view)
        await c.git_view.current()
        return c.git_view

    monkeypatch.setattr("firefly_forge.consumed_terminal_completions.prepare_accepted_reconciled_handoff", prepared)
    c.deliveries = GitDeliveries(c.store, c.engine, c.journal, workspace=lambda _: c.workspace)
    c.git_before = copy.deepcopy((live, native, [(row.status, row.data) for row in c.store.sandboxes.rows]))
    return c


async def test_live_held_projection_still_refuses_ordinary_ack_continuation(accepted_git):
    c = accepted_git
    with pytest.raises(ValueError, match="acknowledgement does not authorize"):
        await assert_continuation_current(c.journal, c.run_id)
    assert c.git_view is None


async def test_known_failed_accepted_held_ack_reaches_existing_checkpoint_policy_gate(accepted_git, monkeypatch):
    c = accepted_git
    reached = []

    class PolicyGateReached(Exception):
        pass

    async def policy(deliveries, current, **kwargs):
        assert current == c.git_before[0]
        reached.append(True)
        raise PolicyGateReached("Explicit WIP checkpoint still requires current policy and consent")

    monkeypatch.setattr("firefly_forge.git_failed_unit_adoption.effective_policy", policy)
    with pytest.raises(PolicyGateReached):
        await c.deliveries._guard(c.run_id, c.git_before[0], c.git_before[0]["execution_actor"])
    assert reached == [True] and c.git_current_checks >= 3
    assert (
        await c.store.get_run(c.run_id),
        await c.journal.entries(c.run_id),
        [(row.status, row.data) for row in c.store.sandboxes.rows],
    ) == c.git_before
    assert len(c.calls) == 1


@pytest.mark.parametrize("change", ["historical_grant", "acknowledged_row", "permission", "ownership", "view"])
async def test_accepted_git_read_authority_cannot_override_changed_custody(accepted_git, change):
    from firefly_forge.git_linked_verification import assert_git_continuation_current

    c = accepted_git
    if change == "historical_grant":
        c.git_mutation = lambda view: view.run.update(repair_budget=17)
    elif change == "acknowledged_row":
        c.unknown.data["effects"]["start"] = {"status": "done", "evidence": {"exit_code": 0}}
    elif change == "permission":
        c.allowed = False
    elif change == "ownership":
        c.store.owned = False
    else:
        c.git_mutation = lambda view: view.entries[-1]["payload"].update(changed=True)
    calls = len(c.calls)
    with pytest.raises((ValueError, RuntimeError)):
        await assert_git_continuation_current(c.deliveries, c.run_id)
    assert len(c.calls) == calls
