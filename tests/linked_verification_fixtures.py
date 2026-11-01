# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Pure wire fixtures; these records assert no runtime, SQL or command execution."""

from firefly_forge.execution_requests import digest
from firefly_forge.journal import digest as native_digest
from firefly_forge.linked_verification_models import (
    LinkedVerificationCandidateV1,
    LinkedVerificationInputV1,
    LinkedVerificationReceiptV1,
)


def candidate(effect_budget=3):
    value = {
        "version": 1,
        "run_id": "b" * 32,
        **{
            field: "c" * 64
            for field in (
                "actor_digest",
                "policy_digest",
                "runtime_contract",
                "profile_digest",
                "runner_digest",
                "input_scope_digest",
                "source_digest",
                "implementation_receipt_digest",
                "terminal_receipt_digest",
                "commands_digest",
                "owner_digest",
                "run_digest",
                "journal_digest",
            )
        },
        "source_tree": "d" * 40,
        "effect_budget": effect_budget,
        "original_effect_budget": 22,
        "original_effect_count": 18,
    }
    value["digest"] = native_digest(value)
    return LinkedVerificationCandidateV1.model_validate(value).model_dump(mode="json")


def consent(value):
    return LinkedVerificationInputV1(
        version=1,
        request_id="a" * 32,
        candidate_digest=value["digest"],
        acknowledge_all_fresh_checks=True,
        acknowledge_no_coding_or_repair=True,
        acknowledge_original_history_and_budget_preserved=True,
    ).model_dump(mode="json")


def receipt(value=None, *, status="queued"):
    value = candidate() if value is None else value
    body = consent(value)
    complete = status == "completed"
    data = {
        "version": 1,
        "kind": "linked_verify",
        "run_id": value["run_id"],
        "request_id": body["request_id"],
        "request_digest": digest(body),
        "candidate": value,
        "request": body,
        "status": status,
        "progress": {
            "started": value["effect_budget"] if complete else 0,
            "completed": value["effect_budget"] if complete else 0,
            "next_ordinal": None,
            "unknown": False,
            "failed": False,
            "ready_to_settle": False,
        },
        "event_digest": "e" * 64,
        "no_coding_grant": True,
        "original_budget_preserved": True,
        "detail": "",
    }
    if complete:
        data["settlement"] = {
            "parent_verify_receipt_digest": "f" * 64,
            "operation_receipt_digest": "e" * 64,
            "source_tree": value["source_tree"],
            "source_digest": value["source_digest"],
            "commands_digest": value["commands_digest"],
            "checks_count": value["effect_budget"],
        }
    elif status == "executing":
        data["progress"]["next_ordinal"] = 1
    elif status == "needs_reconciliation":
        data["progress"].update(started=1, unknown=True)
    elif status == "failed":
        data["progress"].update(started=1, completed=1, failed=True)
    return LinkedVerificationReceiptV1.model_validate(data).model_dump(mode="json")
