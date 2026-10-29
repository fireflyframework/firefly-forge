# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Generic dispatch cannot replace a consumed continuity ticket or its native recovery."""

from __future__ import annotations

import copy
import hashlib
import re
from types import SimpleNamespace
from typing import Any

from pyfly.data.relational.sqlalchemy import LockMode

from firefly_forge.execution_request_persistence import ForgeExecutionRequestRepository
from firefly_forge.invalid_read_recovery_events import read_recovery_events
from firefly_forge.invalid_read_settlements import CLAIM, KIND, SETTLEMENT, settled_requests
from firefly_forge.invalid_read_tickets import verify_ticket
from firefly_forge.journal import digest
from firefly_forge.known_worker_completions import _entries
from firefly_forge.store import Store, identifier

Data = dict[str, Any]
_MESSAGE = "Invalid-read continuity requires same-ticket recovery or a completed native receipt before generic dispatch"
_ID = re.compile(r"^[a-f0-9]{32}$")


def _require(value: Any) -> None:
    if not value:
        raise ValueError(_MESSAGE)


def _completed(ticket: Data, entries: list[Data], events: list[Data]) -> None:
    if any(event["kind"] == "final_outcome_continuation_admitted" for event in events):
        from firefly_forge.final_outcome_continuations import final_record
        from firefly_forge.native_terminal_completion_models import validate_native_receipt

        record = final_record(ticket, entries, events)
        if record is None:
            raise ValueError(_MESSAGE)
        _require(ticket["status"] == "completed")
        result = ticket["result"]
        _require(type(result) is dict and set(result) == {"run_status", "same_task_attempt", "outcome"})
        _require(result["run_status"] in {"paused", "succeeded", "validating", "failed"})
        _require(type(result["same_task_attempt"]) is int and result["same_task_attempt"] == ticket["binding"]["round"])
        _require(result["outcome"] == record["payload"]["candidate"]["outcome"])
        p = record["payload"]
        terminal = [
            e
            for e in entries
            if e["kind"] == "terminal_completion_receipt"
            and e["sequence"] > record["sequence"]
            and e["payload"]["proof"]["terminal_report_sha256"] == p["candidate"]["report_digest"]
            and any(
                c["consume_record_digest"] == p["candidate"]["consume_record_digest"]
                for c in e["payload"]["proof"]["completions"]
            )
        ]
        _require(len(terminal) == 1)
        validate_native_receipt(terminal[0], entries)
        return
    if any(event["kind"] == "checkpoint_continuation_admitted" for event in events):
        from firefly_forge.checkpoint_continuations import checkpoint_record

        _require(checkpoint_record(ticket, entries, events) is not None)
        _require(ticket["status"] == "completed")
        result = ticket["result"]
        _require(type(result) is dict and set(result) == {"run_status", "same_task_attempt"})
        _require(result["run_status"] in {"paused", "succeeded", "validating"})
        _require(type(result["same_task_attempt"]) is int and result["same_task_attempt"] == ticket["binding"]["round"])
        return
    if any(event["kind"] == "request_recovered_after_readonly_observation" for event in events):
        from firefly_forge.readonly_observation_recoveries import validate_readonly_observation_record

        _require(validate_readonly_observation_record(ticket, entries, events) is not None)
        _require(ticket["status"] == "completed")
        result = ticket["result"]
        _require(type(result) is dict and set(result) == {"run_status", "same_task_attempt"})
        _require(result["run_status"] in {"paused", "succeeded", "validating"})
        _require(type(result["same_task_attempt"]) is int and result["same_task_attempt"] == ticket["binding"]["round"])
        return
    if any(event["kind"] == "request_recovered_after_rejection" for event in events):
        from firefly_forge.provider_rejection_recoveries import validate_rejection_recovery_record

        record = validate_rejection_recovery_record(ticket, entries, events)
        _require(record is not None and ticket["status"] == "completed")
        result = ticket["result"]
        _require(type(result) is dict and set(result) == {"run_status", "same_task_attempt"})
        _require(result["run_status"] in {"paused", "succeeded", "validating"})
        _require(type(result["same_task_attempt"]) is int and result["same_task_attempt"] == ticket["binding"]["round"])
        return
    _require(
        events[0]["kind"] == "request_admitted"
        and events[0]["evidence"]
        == {
            "request_digest": ticket["request_digest"],
            "admission_seal": ticket["admission_seal"],
        }
    )
    recovered = any(e["kind"] == "request_recovered_before_claim" for e in events) or any(
        e["kind"] == "invalid_read_recovery"
        and (
            e["idempotency_key"] == "invalid-recover:" + ticket["id"]
            or isinstance(e["payload"], dict)
            and e["payload"].get("request_id") == ticket["id"]
        )
        for e in entries
    )
    if recovered:
        from firefly_forge.invalid_read_recoveries import validate_recovery_record

        _require(validate_recovery_record(ticket, entries, events) is not None)
        expected = [
            "request_admitted",
            "worker_failed",
            "request_recovered_before_claim",
            "worker_claim",
            "worker_completed",
        ]
    else:
        expected = ["request_admitted", "worker_claim", "worker_completed"]
    _require([event["kind"] for event in events] == expected)
    _require(isinstance(ticket["generation"], str) and _ID.fullmatch(ticket["generation"]))
    _require(type(ticket["owner_pid"]) is int and ticket["owner_pid"] > 0)
    _require(
        events[-2]["evidence"]
        == {
            "generation": ticket["generation"],
            "request_digest": ticket["request_digest"],
            "owner_pid": ticket["owner_pid"],
        }
    )
    result = ticket["result"]
    _require(type(result) is dict and set(result) == {"run_status", "same_task_attempt"})
    _require(result["run_status"] in {"paused", "succeeded", "validating"})
    _require(type(result["same_task_attempt"]) is int and result["same_task_attempt"] == ticket["binding"]["round"])
    _require(events[-1]["evidence"] == result)


async def assert_generic_dispatch_allowed(store: Any, journal: Any, run: Data) -> None:
    """Only archived, proven completion releases ordinary routes; no execution authority is granted."""
    marker = run.get("invalid_read_continuity")
    requests = getattr(store, "execution_requests", None)
    if requests is None:
        if marker is not None:
            raise ValueError(_MESSAGE)
        if not isinstance(store, Store):
            return
        requests = SimpleNamespace(store=store, records=ForgeExecutionRequestRepository())
    try:
        _require(requests is not None and requests.store is store)
        run_id = run["id"]
        _require(isinstance(run_id, str) and _ID.fullmatch(run_id))
        async with store._unit():
            _require(await store.get_run(run_id) == run)
            if marker is None:
                count = await requests.records.count_for_run(identifier(run_id), KIND)
                _require(type(count) is int and count == 0)
                return
            _require(
                type(marker) is dict
                and set(marker)
                == {
                    "request_id",
                    "settlement_digest",
                    "claim_digest",
                    "lease_generation",
                }
            )
            request_id = marker["request_id"]
            _require(isinstance(request_id, str) and _ID.fullmatch(request_id))
            row = await requests.records.find_by_id(identifier(request_id), lock=LockMode.PESSIMISTIC_READ)
            _require(row is not None and row.id.hex == request_id and row.run_id.hex == run_id and row.kind == KIND)
            ticket = copy.deepcopy(row.data)
            _require(ticket["id"] == request_id and ticket["run_id"] == run_id and ticket["kind"] == row.kind)
            _require(row.status == ticket["status"] == "completed")
            verify_ticket(ticket)
            _require(digest(ticket["request"]) == ticket["request_digest"])
            _require(
                marker
                == {
                    "request_id": ticket["id"],
                    "settlement_digest": ticket["migration_digest"],
                    "claim_digest": ticket["claim_digest"],
                    "lease_generation": ticket["lease_generation"],
                }
            )
            _require(journal is not None and journal.store is store)
            entries = _entries(await journal.records.find_for_run(identifier(run_id)), run_id)
            settlements = [entry for entry in entries if entry["kind"] == SETTLEMENT]
            _require(settlements and settlements[-1]["digest"] == ticket["migration_digest"])
            settlement = settlements[-1]
            settled_requests(settlement, entries)
            receipt = settlement["payload"]
            _require(ticket["request"] == {"run_id": run_id, "actor": receipt["actor"], "input": receipt["request"]})
            _require(row.request_key == receipt["proof"]["hold_request_id"])
            _require(ticket["task_key"] == receipt["proof"]["task_key"])
            _require(ticket["lease_generation"] == receipt["binding"]["invocation_id"])
            for key in (
                "binding",
                "checkpoint_digest",
                "command_custody_digest",
                "settings_digest",
                "protected_scope_digest",
                "lease",
                "lease_policy",
                "current_profile",
                "policy_epoch",
            ):
                _require(ticket[key] == receipt[key])
            _require(
                isinstance(ticket["checkpoint"], str)
                and hashlib.sha256(ticket["checkpoint"].encode()).hexdigest() == ticket["checkpoint_digest"]
            )
            claims = [
                entry for entry in entries if entry["kind"] == CLAIM and entry["digest"] == ticket["claim_digest"]
            ]
            _require(len(claims) == 1 and claims[0]["payload"]["settlement_digest"] == settlement["digest"])
            from firefly_forge.run_evidence import LIMITS

            maximum = (
                LIMITS["journal"]
                if any(entry["kind"] in {"checkpoint_continuation", "final_outcome_continuation"} for entry in entries)
                else 11
                if any(entry["kind"] == "readonly_observation_recovery" for entry in entries)
                else 8
                if any(entry["kind"] == "provider_rejection_recovery" for entry in entries)
                else 5
            )
            events = await read_recovery_events(requests, request_id, maximum=maximum)
            _completed(ticket, entries, events)
    except (KeyError, TypeError, AttributeError, ValueError, ImportError):
        raise ValueError(_MESSAGE) from None
