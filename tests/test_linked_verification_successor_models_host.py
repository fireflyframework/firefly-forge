# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""An explicit all-fresh successor preserves prior known effects and has one frontier."""

import pytest
from linked_verification_fixtures import candidate, receipt
from pydantic import ValidationError

from firefly_forge import linked_verification_models as models
from firefly_forge.execution_requests import digest
from firefly_forge.journal import digest as native_digest


def held(request_id="a" * 32):
    value = receipt(status="needs_reconciliation")
    value["request_id"] = value["request"]["request_id"] = request_id
    value["request_digest"] = digest(value["request"])
    value["progress"].update(started=2, completed=2, unknown=False)
    return models.LinkedVerificationReceiptV1.model_validate(value)


def successor(parent, *, request_id="f" * 32, status="queued"):
    scope = dict(
        predecessor_request_id=parent.request_id,
        predecessor_event_digest=parent.event_digest,
        parent_attempt=3,
        output_policy="vitest_no_cache_v1",
    )
    value = candidate()
    value["successor"] = scope
    value["execution_plan"] = [
        dict(
            ordinal=i + 1,
            logical_command_digest="1" * 64,
            physical_argv=["qualified", str(i), *(["--", "--no-cache"] if i == 0 else [])],
            output_policy="vitest_no_cache_v1" if i == 0 else "unchanged",
        )
        for i in range(value["effect_budget"])
    ]
    value["digest"] = native_digest({k: v for k, v in value.items() if k != "digest"})
    value = models.LinkedVerificationCandidateV1.model_validate(value).model_dump(mode="json")
    body = dict(
        version=1,
        request_id=request_id,
        candidate_digest=value["digest"],
        successor=scope,
        acknowledge_all_fresh_checks=True,
        acknowledge_no_coding_or_repair=True,
        acknowledge_original_history_and_budget_preserved=True,
        acknowledge_old_results_not_reused=True,
    )
    complete = status == "completed"
    data = dict(
        version=1,
        kind="linked_verify",
        run_id=value["run_id"],
        request_id=request_id,
        request_digest=digest(body),
        candidate=value,
        request=body,
        status=status,
        event_digest="2" * 64,
        no_coding_grant=True,
        original_budget_preserved=True,
        detail="",
        progress=dict(
            started=3 if complete else 0,
            completed=3 if complete else 0,
            next_ordinal=None,
            unknown=False,
            failed=False,
            ready_to_settle=False,
        ),
    )
    if complete:
        data["settlement"] = dict(
            parent_verify_receipt_digest="3" * 64,
            operation_receipt_digest="2" * 64,
            source_tree=value["source_tree"],
            source_digest=value["source_digest"],
            commands_digest=value["commands_digest"],
            checks_count=3,
        )
    return models.LinkedVerificationReceiptV1.model_validate(data)


def frontier(values):
    return models.linked_verification_frontier(
        values, run_id="b" * 32, implementation_receipt_digest="c" * 64, terminal_receipt_digest="c" * 64
    )


def test_known_prefix_requires_explicit_all_fresh_successor_and_unique_current_leaf():
    parent = held()
    child = successor(parent, status="completed")
    assert frontier([child, parent]) == child
    assert parent.progress.started == parent.progress.completed == 2
    assert child.progress.started == child.progress.completed == child.candidate.effect_budget == 3
    assert child.candidate.original_effect_budget == 22 and child.candidate.original_effect_count == 18


@pytest.mark.parametrize("change", ["event", "run", "implementation", "terminal", "unknown", "failed", "branch"])
def test_successor_graph_refuses_foreign_unknown_failed_or_branching_history(change):
    parent = held()
    child = successor(parent)
    if change in {"event", "run", "implementation", "terminal"}:
        data = child.model_dump(mode="json")
        if change == "event":
            data["candidate"]["successor"]["predecessor_event_digest"] = "9" * 64
            data["request"]["successor"]["predecessor_event_digest"] = "9" * 64
        else:
            key = {
                "run": "run_id",
                "implementation": "implementation_receipt_digest",
                "terminal": "terminal_receipt_digest",
            }[change]
            data["candidate"][key] = "9" * (32 if change == "run" else 64)
            if change == "run":
                data["run_id"] = "9" * 32
        data["candidate"]["digest"] = native_digest({k: v for k, v in data["candidate"].items() if k != "digest"})
        data["request"]["candidate_digest"] = data["candidate"]["digest"]
        data["request_digest"] = digest(data["request"])
        child = models.LinkedVerificationReceiptV1.model_validate(data)
    elif change in {"unknown", "failed"}:
        data = parent.model_dump(mode="json")
        data["progress"].update(
            completed=1 if change == "unknown" else 2, unknown=change == "unknown", failed=change == "failed"
        )
        if change == "failed":
            data["status"] = "failed"
        parent = models.LinkedVerificationReceiptV1.model_validate(data)
    values = [parent, child]
    if change == "branch":
        values.append(successor(parent, request_id="8" * 32))
    with pytest.raises(ValueError):
        frontier(values)


def test_successor_consent_cannot_omit_no_reuse_or_change_reviewed_predecessor():
    child = successor(held())
    for change in ["missing_ack", "changed_predecessor"]:
        data = child.model_dump(mode="json")
        if change == "missing_ack":
            data["request"].pop("acknowledge_old_results_not_reused")
        else:
            data["request"]["successor"]["predecessor_request_id"] = "9" * 32
        data["request_digest"] = digest(data["request"])
        with pytest.raises(ValidationError):
            models.LinkedVerificationReceiptV1.model_validate(data)


def test_unrelated_roots_do_not_become_current_by_timestamp_or_list_order():
    with pytest.raises(ValueError):
        frontier([held(), held("8" * 32)])


@pytest.mark.parametrize("change", ["commands", "budget"])
def test_successor_cannot_change_the_frozen_full_logical_command_scope(change):
    parent = held()
    data = successor(parent).model_dump(mode="json")
    if change == "commands":
        data["candidate"]["commands_digest"] = "9" * 64
    else:
        data["candidate"]["effect_budget"] += 1
        data["candidate"]["execution_plan"].append(
            dict(
                ordinal=4, logical_command_digest="1" * 64, physical_argv=["qualified", "4"], output_policy="unchanged"
            )
        )
    data["candidate"]["digest"] = native_digest({k: v for k, v in data["candidate"].items() if k != "digest"})
    data["request"]["candidate_digest"] = data["candidate"]["digest"]
    data["request_digest"] = digest(data["request"])
    child = models.LinkedVerificationReceiptV1.model_validate(data)
    with pytest.raises(ValueError):
        frontier([parent, child])
