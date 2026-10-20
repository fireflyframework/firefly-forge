# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Recovery feedback uses native attempt identities, not polling timing."""

import copy
import hashlib
from types import SimpleNamespace

import pytest
from test_scm_delegated_feedback import RECEIPT, received
from test_scm_delegated_feedback import delegated_feedback as delegated_feedback

from firefly_forge.collaboration_feedback import feedback_card, feedback_due, milestone_key


def failed_run():
    return dict(
        id="2" * 32,
        status="failed",
        active_task="implement",
        attempt=4,
        final_verification=[{"attempt": 1, "exit_code": 0}],
    )


def with_recovery(*, request_id="a" * 32, attempt=4, status="consumed"):
    return {
        **failed_run(),
        "provider_recovery": dict(request_id=request_id, task_key="b" * 32, attempt=attempt, status=status),
    }


def card(run, safe=lambda value: value):
    return feedback_card(dict(id="c" * 32, command={"action": "status"}), run, {}, "", safe)


@pytest.mark.parametrize("changes", [{"request_id": "d" * 32}, {"attempt": 3}])
def test_distinct_failed_recoveries_have_distinct_feedback_keys(changes):
    first = with_recovery()
    second = with_recovery(**changes)
    assert milestone_key(first) != milestone_key(second)
    receipt = dict(feedback_status="sent", feedback_key=milestone_key(first), feedback_history=[{}])
    assert feedback_due(receipt, second)
    assert not feedback_due(receipt, copy.deepcopy(first))


def test_recovery_card_names_attempt_and_distinguishes_old_checks():
    body = card(with_recovery())
    assert "Recovery attempt: **4**" in body
    assert "Recovery request: `" + "a" * 32 + "`" in body
    assert "Recorded check attempts: 1" in body
    assert "do not verify this recovery attempt" in body
    assert "1/1 checks passed" in body


@pytest.mark.parametrize("checks", [[{"exit_code": 0}], [{"attempt": True, "exit_code": 0}]])
def test_missing_or_invalid_check_attempt_is_not_current_recovery_evidence(checks):
    run = with_recovery()
    run["final_verification"] = checks
    body = card(run)
    assert "attempt provenance is incomplete" in body
    assert "do not verify this recovery attempt" in body


def test_checks_from_current_recovery_are_identified_without_stale_warning():
    run = with_recovery()
    run["final_verification"] = [{"attempt": 4, "exit_code": 0}]
    body = card(run)
    assert "Recorded check attempts: 4" in body
    assert "recorded for this recovery attempt" in body
    assert "do not verify" not in body


def test_legacy_run_fingerprint_is_unchanged():
    assert milestone_key(failed_run()) == "b5e5d6f470c2b506709dfd5ebd21a929cad8e4fe24349988605af40f893cd512"
    assert "Recovery attempt" not in card(failed_run())


@pytest.mark.parametrize(
    "state",
    [
        None,
        {},
        "invalid",
        {"request_id": "@everyone", "attempt": 4, "status": "consumed"},
        {"request_id": "a" * 32, "attempt": True, "status": "consumed"},
        {"request_id": "a" * 32, "attempt": 4, "status": []},
        {"request_id": "a" * 32, "attempt": 4, "status": "unknown"},
    ],
)
def test_invalid_recovery_does_not_change_legacy_key_or_add_public_claim(state):
    run = {**failed_run(), "provider_recovery": state}
    assert milestone_key(run) == milestone_key(failed_run())
    assert "Recovery attempt" not in card(run)


def test_mixed_checks_do_not_claim_the_recovery_attempt_was_verified():
    run = with_recovery()
    run["final_verification"] = [{"attempt": 1, "exit_code": 0}, {"attempt": 4, "exit_code": 0}]
    body = card(run)
    assert "Recorded check attempts: 1, 4" in body
    assert "do not verify this recovery attempt" in body


def test_recovery_feedback_preserves_the_existing_post_limit():
    receipt = dict(feedback_status="sent", feedback_key=milestone_key(with_recovery(attempt=3)))
    assert not feedback_due({**receipt, "feedback_history": [{}] * 32}, with_recovery())


def test_valid_recovery_wakes_a_legacy_receipt_without_reposting_unchanged_legacy_runs():
    receipt = dict(feedback_status="sent", feedback_stage="failed", feedback_request_id=None)
    assert not feedback_due(receipt, failed_run())
    assert feedback_due(receipt, with_recovery())


async def test_second_failed_recovery_posts_once_when_polling_misses_running_state(delegated_feedback):
    service, row, run, sent, _ = delegated_feedback
    received(row, "status")
    run.update(with_recovery(attempt=3), worker_request={})
    await service.process(RECEIPT)
    previous = copy.deepcopy(row.data["feedback_intent"])
    run.update(with_recovery(request_id="d" * 32))
    await service.sync_feedback()
    await service.sync_feedback()
    assert len(sent) == 2
    assert "Recovery attempt: **4**" in sent[-1]["body"]
    intent = row.data["feedback_intent"]
    assert intent["key"] != previous["key"]
    assert intent["body_sha256"] == hashlib.sha256(sent[-1]["body"].encode()).hexdigest()
    assert row.data["feedback_history"][-1]["key"] == intent["key"]
    assert row.data["feedback_history"][-1]["remote"]["id"] == 91


async def test_new_recovery_cannot_bypass_an_unknown_feedback_write(delegated_feedback):
    service, row, run, sent, state = delegated_feedback
    received(row, "status")
    run.update(with_recovery(attempt=3), worker_request={})
    state["fail_delivery"] = True
    await service.process(RECEIPT)
    retained = copy.deepcopy(row.data["feedback_intent"])
    run.update(with_recovery(request_id="d" * 32))
    await service.sync_feedback()
    await service.process(RECEIPT)
    assert len(sent) == 1
    assert row.data["feedback_status"] == "needs_reconciliation"
    assert row.data["feedback_intent"] == retained


async def test_terminal_recovery_selection_requires_same_run_repository_and_new_identity():
    from sqlalchemy.dialects import postgresql

    from firefly_forge.collaboration_persistence import ForgeWebhookInboxRepository

    statements = []

    async def scalars(statement):
        statements.append(str(statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})))
        return []

    repository = ForgeWebhookInboxRepository(session=SimpleNamespace(scalars=scalars))
    assert await repository.find_pending() == []
    assert len(statements) == 1
    sql = statements[0]
    assert "EXISTS (SELECT" in sql
    assert "forge_runs.repository_id = forge_webhook_inbox.repository_id" in sql
    assert "forge_runs.data ->> 'id'" in sql and "forge_webhook_inbox.data ->> 'run_id'" in sql
    assert "IS DISTINCT FROM" in sql
    assert "'provider_recovery'" in sql and "'feedback_intent'" in sql
    assert "'request_id'" in sql and "'attempt'" in sql
    assert "jsonb_typeof" in sql and "'reserved', 'consumed', 'completed'" in sql


async def test_feedback_intent_retains_the_native_recovery_identity(delegated_feedback):
    service, row, run, _, _ = delegated_feedback
    received(row, "status")
    run.update(with_recovery(), worker_request={})
    await service.process(RECEIPT)
    assert row.data["feedback_intent"]["provider_recovery"] == {"request_id": "a" * 32, "attempt": 4}
