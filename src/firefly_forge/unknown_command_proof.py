# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Recognition of one explicit follow-up grant; no inference of original command outcome."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from firefly_forge.journal import digest
from firefly_forge.unknown_command_models import UnknownCommandReceiptV1

if TYPE_CHECKING:
    from firefly_forge.completed_final_epoch import PreparedCompletedFinal

Data = dict[str, Any]
ACKNOWLEDGEMENT = "unknown_command_acknowledgement"


@dataclass(frozen=True)
class _PreparedCustodyPolicy:
    run_id: str
    digest: str


_POLICY: ContextVar[_PreparedCustodyPolicy | None] = ContextVar("forge_command_custody_policy", default=None)
_COMPLETED_FINAL: ContextVar[PreparedCompletedFinal | None] = ContextVar(
    "forge_completed_final_command_custody", default=None
)


@contextmanager
def completed_final_command_scope(prepared: PreparedCompletedFinal | None) -> Iterator[None]:
    if prepared is None:
        yield
        return
    token = _COMPLETED_FINAL.set(prepared)
    try:
        yield
    finally:
        _COMPLETED_FINAL.reset(token)


@contextmanager
def continuation_policy_scope(run_id: str) -> Iterator[None]:
    from firefly_forge.unknown_command_acknowledgements import policy_digest

    token = _POLICY.set(_PreparedCustodyPolicy(run_id, policy_digest()))
    try:
        yield
    finally:
        _POLICY.reset(token)


def unknown_execution(row: Any, value: Data) -> str:
    effects = value["effects"]
    if (
        row.task_id != "implement"
        or row.status != "removed"
        or value["request"]["request"].get("configuration", {}).get("mode") != "host"
        or set(effects) not in ({"create", "start", "cleanup"}, {"create", "cleanup"})
        or effects["create"].get("status") != "done"
        or effects["cleanup"].get("status") != "done"
        or ("start" in effects and effects["start"] != {"status": "intent"})
        or any(state.get("evidence", {}).get("outcome") == "interrupted" for state in effects.values())
        or any(
            state.get("evidence", {}).get("workspace_import") == "requires_reconciliation" for state in effects.values()
        )
    ):
        raise ValueError("Only canonical cleaned native host custody with an unknown start or no start is eligible")
    return "start_unknown" if "start" in effects else "closed_before_start"


def _mode_acknowledgement_policy(run: Data, record: Data, current_policy: str) -> bool:
    from firefly_forge.checkpoint_mode_runtime import acknowledgement_policy_matches

    return acknowledgement_policy_matches(run, record, current_policy)


def _acknowledged_followup(
    row: Any,
    value: Data,
    entries: list[Data],
    run: Data,
    ordinal: int,
    current_policy_digest: str,
    *,
    completed: bool = False,
) -> Data | None:
    try:
        classification = unknown_execution(row, value)
    except ValueError:
        return None
    records = [
        entry
        for entry in entries
        if entry["kind"] == ACKNOWLEDGEMENT and entry["payload"].get("execution_id") == row.id.hex
    ]
    state = run.get("verification_handoff", {})
    if state.get("status") != ("completed" if completed else "running") or not state.get("id"):
        return None
    intents = [
        entry
        for entry in entries
        if entry["kind"] == "verification_handoff_intent" and entry["payload"].get("id") == state["id"]
    ]
    if len(intents) != 1:
        return None
    intent_entry, matches = intents[0], []
    intent = intent_entry["payload"]
    for record in records:
        try:
            receipt = UnknownCommandReceiptV1.model_validate(record["payload"])
        except ValueError:
            continue
        f = receipt.followup.model_dump()
        adoption = [
            entry
            for entry in entries
            if entry["kind"] == "verification_handoff_adoption"
            and entry["payload"].get("request", {}).get("request_id") == f["adoption_request_id"]
        ]
        starts = [
            entry
            for entry in entries
            if entry["kind"] == "sandbox_intent"
            and entry["payload"].get("execution_id") == row.id.hex
            and entry["payload"].get("effect") == "start"
        ]
        if len(adoption) != 1 or len(starts) != (1 if classification == "start_unknown" else 0):
            continue
        start_digest = starts[0]["digest"] if starts else None
        last_effect = max(
            e["sequence"]
            for e in entries
            if e["kind"] in {"sandbox_intent", "sandbox_receipt", "sandbox_reconcile"}
            and e["payload"].get("execution_id") == row.id.hex
        )
        bound = adoption[0]
        request = bound["payload"]["request"]
        if (
            receipt.run_id != run["id"]
            or (
                receipt.candidate.policy_digest != current_policy_digest
                and not _mode_acknowledgement_policy(run, record, current_policy_digest)
            )
            or receipt.candidate.execution_digest != digest(value)
            or receipt.candidate.classification != classification
            or receipt.candidate.start_intent_digest != start_digest
            or receipt.candidate.journal_digest
            != digest({"entries": [e for e in entries if e["sequence"] < record["sequence"]]})
            or receipt.candidate.ordinal != row.ordinal
            or record["idempotency_key"] != "sandbox-unknown-ack:" + receipt.request_id
            or record["task_id"] != "implement"
            or not last_effect < record["sequence"] < bound["sequence"] < intent_entry["sequence"]
            or any(
                request.get(key) != f[key]
                for key in (
                    "pending_request_digest",
                    "source_digest",
                    "commands_digest",
                    "policy_digest",
                    "provider_digest",
                    "agent",
                )
            )
            or bound["payload"].get("body_digest") != digest(request)
            or bound["payload"].get("actor", {}).get("user_id") != receipt.actor.user_id
            or bound["payload"].get("actor", {}).get("authentication_method") != receipt.actor.authentication_method
            or intent.get("adoption_request_id") != f["adoption_request_id"]
            or intent.get("request_digest") != f["pending_request_digest"]
            or any(
                intent.get(key) != f[key]
                for key in (
                    "source_digest",
                    "commands_digest",
                    "policy_digest",
                    "provider_digest",
                    "sandbox_digest",
                    "scope_digest",
                    "agent",
                    "parent_attempt",
                    "continuation",
                    "ordinals",
                )
            )
            or intent.get("parent_attempt") != row.parent_attempt
            or ordinal not in f["ordinals"]
            or min(f["ordinals"]) <= row.ordinal
            or any(
                state.get(key) != intent.get(key)
                for key in (
                    "id",
                    "request_id",
                    "source_digest",
                    "commands_digest",
                    "policy_digest",
                    "provider_digest",
                    "continuation",
                )
            )
        ):
            continue
        matches.append(receipt.actor.model_dump())
    return matches[0] if len(matches) == 1 else None


def continuation_scope(run: Data) -> str:
    """Only original grants: mutable source and execution progress are deliberately excluded."""
    return digest(
        {
            key: run.get(key)
            for key in (
                "id",
                "project_id",
                "repository_id",
                "component_id",
                "component_snapshot",
                "execution_actor",
                "sandbox_snapshot",
                "publish",
                "commands",
                "policy_snapshot",
                "tool_policy_snapshot",
                "role_snapshot",
                "skills",
                "design_snapshot",
                "branding_snapshot",
                "product_definition",
                "review_model_selection",
                "review_config_revision",
                "review_execution_config",
                "scm_snapshot",
                "knowledge_snapshot",
                "learning_snapshot",
                "mcp_snapshot",
                "search_snapshot",
                "operation",
                "git_delivery_policy",
                "backend",
                "frontend",
                "brief",
                "repair_budget",
                "assurance",
            )
        }
    )


def _one(entries: list[Data], kind: str, key: str) -> Data | None:
    matches = [e for e in entries if e["kind"] == kind and e["idempotency_key"] == key]
    return matches[0] if len(matches) == 1 else None


def _acknowledged_continuation(
    row: Any,
    values: dict[str, Data],
    rows: list[Any],
    entries: list[Data],
    run: Data,
    tasks: list[Any],
    task_id: str | None,
    policy: str,
) -> Data | None:
    """Recognize the real completed-handoff lineage, never settle the retained command."""
    from uuid import NAMESPACE_URL, uuid5

    state = run.get("verification_handoff", {})
    handoff = _one(entries, "verification_handoff_intent", state.get("id", ""))
    if handoff is None:
        return None
    intent = handoff["payload"]
    actor = _acknowledged_followup(row, values[row.id.hex], entries, run, intent["ordinals"][0], policy, completed=True)
    if actor is None:
        return None
    acknowledgements = [
        UnknownCommandReceiptV1.model_validate(e["payload"])
        for e in entries
        if e["kind"] == ACKNOWLEDGEMENT
        and e["payload"].get("execution_id") == row.id.hex
        and e["payload"].get("followup", {}).get("adoption_request_id") == intent.get("adoption_request_id")
    ]
    if len(acknowledgements) != 1:
        return None
    followup = acknowledgements[0].followup
    if continuation_scope(run) != followup.continuation_scope_digest or run.get("agent") != intent["agent"]:
        return None
    outcome = _one(entries, "verification_handoff_outcome", intent["id"] + ":outcome")
    response = _one(entries, "worker_response", "worker-response:" + intent["id"])
    coding = _one(entries, "verification_continuation_intent", intent["id"] + ":coding")
    resumed = [
        e
        for e in entries
        if e["kind"] in {"resume", "handoff"}
        and e["payload"].get("automatic") is True
        and e["payload"].get("verification_handoff_id") == intent["id"]
    ]
    if outcome is None or response is None or coding is None or len(resumed) != 1:
        return None
    if (
        outcome["payload"] != state
        or state.get("completed_commands") != len(intent["ordinals"])
        or response["payload"] != run.get("worker_response")
        or response["payload"].get("source") != "factory_verification"
        or response["payload"].get("verification_handoff_id") != intent["id"]
        or response["payload"].get("responding_actor")
        != {
            "version": 4,
            "authentication_method": "factory_verification",
            "handoff_id": intent["id"],
            **{k: intent[k] for k in ("source_digest", "commands_digest", "policy_digest", "provider_digest")},
        }
        or coding["payload"]
        != {
            "version": 1,
            "id": intent["id"],
            "request_id": intent["request_id"],
            "parent_round": intent["parent_round"],
            "next_round": intent["parent_round"] + 1,
            "provider_digest": intent["provider_digest"],
        }
        or resumed[0]["payload"].get("agent") != intent["agent"]
        or resumed[0]["payload"].get("config_revision") != run.get("config_revision")
        or not handoff["sequence"]
        < response["sequence"]
        < outcome["sequence"]
        < resumed[0]["sequence"]
        < coding["sequence"]
        or any(e["kind"] in {"resume", "handoff"} and e["sequence"] > resumed[0]["sequence"] for e in entries)
    ):
        return None
    for index, ordinal in enumerate(intent["ordinals"]):
        command = _one(entries, "verification_handoff_command", intent["id"] + ":" + str(index))
        matches = [
            r
            for r in rows
            if r.task_id == "implement" and r.parent_attempt == intent["parent_attempt"] and r.ordinal == ordinal
        ]
        if command is None or len(matches) != 1:
            return None
        execution = values[matches[0].id.hex]
        start = execution["effects"].get("start", {})
        if (
            start.get("status") != "done"
            or type(start.get("evidence", {}).get("exit_code")) is not int
            or execution["effects"].get("cleanup", {}).get("status") != "done"
            or command["payload"]
            != {
                "version": 1,
                "id": intent["id"],
                "request_id": intent["request_id"],
                "command_index": index + 1,
                "ordinal": ordinal,
                "execution_id": matches[0].id.hex,
                "exit_code": start["evidence"]["exit_code"],
                "outcome": "completed",
            }
            or not handoff["sequence"] < command["sequence"] < response["sequence"]
        ):
            return None
    implementation = next((t for t in tasks if t.task_id == "implement"), None)
    if implementation is None or type(run.get("attempt")) is not int:
        return None
    attempt = implementation.attempt
    repair_budget = run.get("repair_budget", 0)
    completed_grants = {
        e["payload"].get("id")
        for e in entries
        if e["kind"] == "verification_handoff_outcome" and e["payload"].get("status") == "completed"
    }
    resumed_grants = {
        e["payload"].get("verification_handoff_id")
        for e in entries
        if e["kind"] in {"resume", "handoff"} and e["payload"].get("automatic") is True
    }
    answered_grants = {
        e["payload"].get("responding_actor", {}).get("handoff_id")
        for e in entries
        if e["kind"] == "worker_response"
        and e["payload"].get("responding_actor", {}).get("version") == 4
        and e["payload"].get("responding_actor", {}).get("authentication_method") == "factory_verification"
    }
    if (
        type(repair_budget) is not int
        or repair_budget < 0
        or attempt > min(4, 1 + repair_budget + len(completed_grants & resumed_grants & answered_grants))
        or attempt < followup.continuation_task_attempt
        or run["attempt"] != attempt
    ):
        return None
    previous_receipt = None
    for current in range(followup.continuation_task_attempt, attempt + 1):
        key = uuid5(NAMESPACE_URL, f"forge:{run['id']}:implement:{current}").hex
        native = _one(entries, "intent", key)
        if native is None:
            return None
        if current == followup.continuation_task_attempt:
            if not resumed[0]["sequence"] < native["sequence"] < coding["sequence"]:
                return None
        else:
            repair = _one(entries, "repair", key)
            failed = (repair or {}).get("payload", {}).get("failed_checks", [])
            verify = [
                e
                for e in entries
                if e["kind"] == "receipt"
                and e["task_id"] == "verify"
                and e["payload"].get("implementation_round") == current - 1
                and e["payload"].get("failed_checks") == failed
            ]
            if (
                previous_receipt is None
                or repair is None
                or len(verify) != 1
                or repair["payload"].get("reason") != "verification_repair"
                or not any(type(c.get("exit_code")) is int and c["exit_code"] != 0 for c in failed)
                or not previous_receipt["sequence"] < verify[0]["sequence"] < repair["sequence"] < native["sequence"]
            ):
                return None
        receipt = _one(entries, "receipt", key)
        if current < attempt and (receipt is None or receipt["payload"].get("implementation_round") != current):
            return None
        previous_receipt = receipt
    if run.get("implementation_pending") is True:
        if (
            task_id != "implement"
            or implementation.status != "running"
            or run.get("implementation_rounds") != attempt - 1
        ):
            return None
    elif (
        run.get("implementation_pending") is not False
        or implementation.status != "done"
        or run.get("implementation_rounds") != attempt
        or previous_receipt is None
        or previous_receipt["payload"].get("implementation_round") != attempt
    ):
        return None
    elif task_id == "verify":
        verify_task = next((t for t in tasks if t.task_id == "verify"), None)
        if verify_task is None or verify_task.status != "running":
            return None
        verify_intent = _one(
            entries, "intent", uuid5(NAMESPACE_URL, f"forge:{run['id']}:verify:{verify_task.attempt}").hex
        )
        if verify_intent is None or verify_intent["sequence"] <= previous_receipt["sequence"]:
            return None
    elif task_id is not None:
        return None
    return actor


def _completed_final_ack(
    row: Any, values: dict[str, Data], rows: list[Any], entries: list[Data], run: Data, policy: str
) -> tuple[Data, Data] | None:
    from firefly_forge.checkpoint_mode_handoff_custody import validate_completed_handoff

    prepared = _COMPLETED_FINAL.get()
    if prepared is None:
        return None
    record, terminal = prepared.record, prepared.terminal
    payload = record["payload"]
    frozen = payload["run"]
    prefix = entries[: record["sequence"] - 1]
    proof = terminal["payload"]["proof"]
    if (
        record["run_id"] != run["id"]
        or continuation_scope(run) != continuation_scope(frozen)
        or digest({"entries": prefix}) != payload["candidate"]["journal_digest"]
        or len([e for e in entries if digest(e) == digest(record)]) != 1
        or len([e for e in entries if digest(e) == digest(terminal)]) != 1
        or terminal["sequence"] <= record["sequence"]
        or proof["task_attempt"] != payload["candidate"]["attempt"]
        or proof["task_key"] != payload["task"]["idempotency_key"]
        or type(run.get("attempt")) is not int
        or run["attempt"] < proof["task_attempt"]
        or any(
            type((run.get("usage") or {}).get(k)) is not int or (run.get("usage") or {})[k] < frozen["usage"][k]
            for k in ("requests", "input_tokens", "output_tokens")
        )
    ):
        return None
    try:
        custody = validate_completed_handoff(frozen, prefix, rows, values)
    except ValueError:
        return None
    handoff = next(e for e in prefix if e["digest"] == custody["handoff_intent_digest"])
    actor = _acknowledged_followup(
        row, values[row.id.hex], prefix, frozen, handoff["payload"]["ordinals"][0], policy, completed=True
    )
    return (actor, frozen) if actor is not None else None


def acknowledged_followup(
    row: Any,
    value: Data,
    entries: list[Data],
    run: Data,
    ordinal: int,
    current_policy_digest: str,
    *,
    completed: bool = False,
    rows: list[Any] | None = None,
    values: dict[str, Data] | None = None,
) -> Data | None:
    actor = _acknowledged_followup(row, value, entries, run, ordinal, current_policy_digest, completed=completed)
    # A new handoff still owns its own commands; the old acknowledgement only retains the abandoned outcome.
    if actor is not None or completed or run.get("verification_handoff", {}).get("status") != "running":
        return actor
    prepared = _COMPLETED_FINAL.get()
    if prepared is None or rows is None or values is None:
        return None
    state = run["verification_handoff"]
    intent = _one(entries, "verification_handoff_intent", state.get("id", ""))
    if (
        intent is None
        or intent["sequence"] <= prepared.terminal["sequence"]
        or intent["payload"].get("parent_attempt") != run.get("attempt")
        or ordinal not in intent["payload"].get("ordinals", [])
        or type(intent["payload"].get("parent_attempt")) is not int
        or intent["payload"]["parent_attempt"] < row.parent_attempt
        or (intent["payload"]["parent_attempt"] == row.parent_attempt and ordinal <= row.ordinal)
    ):
        return None
    # This branch is used inside native command allocation, whose writer checks the
    # current task/handoff, policy, provider and actor before creating the new ordinal.
    native = _completed_final_ack(row, values, rows, entries, run, current_policy_digest)
    return native[0] if native is not None else None


def _completed_final_phase(run: Data, tasks: list[Any], entries: list[Data], start: int, task_id: str | None) -> bool:
    from uuid import NAMESPACE_URL, uuid5

    from firefly_forge.store import identifier

    implementation = next((t for t in tasks if t.task_id == "implement"), None)
    if (
        implementation is None
        or type(implementation.attempt) is not int
        or implementation.attempt != run.get("attempt")
        or implementation.run_id != identifier(run["id"])
        or type(run.get("implementation_rounds")) is not int
        or implementation.data.get("idempotency_key")
        != uuid5(NAMESPACE_URL, f"forge:{run['id']}:implement:{implementation.attempt}").hex
    ):
        return False
    attempt = implementation.attempt
    repair = run.get("repair_budget", 0)
    completed = {
        e["payload"].get("id")
        for e in entries
        if e["kind"] == "verification_handoff_outcome" and e["payload"].get("status") == "completed"
    }
    resumed = {
        e["payload"].get("verification_handoff_id")
        for e in entries
        if e["kind"] in {"resume", "handoff"} and e["payload"].get("automatic") is True
    }
    answered = {
        e["payload"].get("responding_actor", {}).get("handoff_id")
        for e in entries
        if e["kind"] == "worker_response"
        and type(e["payload"].get("responding_actor", {}).get("version")) is int
        and e["payload"]["responding_actor"]["version"] == 4
        and e["payload"]["responding_actor"].get("authentication_method") == "factory_verification"
    }
    if (
        type(repair) is not int
        or repair < 0
        or not start <= attempt <= min(4, 1 + repair + len(completed & resumed & answered))
    ):
        return False
    previous = None
    for current in range(start, attempt + 1):
        key = uuid5(NAMESPACE_URL, f"forge:{run['id']}:implement:{current}").hex
        native = _one(entries, "intent", key)
        if native is None or native.get("run_id") != run["id"] or native.get("task_id") != "implement":
            return False
        if current > start:
            transition = _one(entries, "repair", key)
            failed = (transition or {}).get("payload", {}).get("failed_checks", [])
            checks = [
                e
                for e in entries
                if e["kind"] == "receipt"
                and e["task_id"] == "verify"
                and e["payload"].get("implementation_round") == current - 1
                and digest(e["payload"].get("failed_checks")) == digest(failed)
            ]
            if (
                previous is None
                or transition is None
                or len(checks) != 1
                or transition["payload"].get("reason") != "verification_repair"
                or not any(type(c.get("exit_code")) is int and c["exit_code"] != 0 for c in failed)
                or not previous["sequence"] < checks[0]["sequence"] < transition["sequence"] < native["sequence"]
            ):
                return False
        previous = _one(entries, "receipt", key)
        if current < attempt and (previous is None or previous["payload"].get("implementation_round") != current):
            return False
    if run.get("implementation_pending") is True:
        return (
            task_id == "implement"
            and implementation.status == "running"
            and run.get("implementation_rounds") == attempt - 1
        )
    if (
        run.get("implementation_pending") is not False
        or implementation.status != "done"
        or run.get("implementation_rounds") != attempt
        or previous is None
        or previous["payload"].get("implementation_round") != attempt
    ):
        return False
    if task_id == "verify":
        verify = next((t for t in tasks if t.task_id == "verify"), None)
        if verify is None or verify.status != "running":
            return False
        intent = _one(entries, "intent", uuid5(NAMESPACE_URL, f"forge:{run['id']}:verify:{verify.attempt}").hex)
        return (
            intent is not None
            and intent.get("run_id") == run["id"]
            and intent.get("task_id") == "verify"
            and intent["sequence"] > previous["sequence"]
        )
    return task_id is None


def acknowledged_continuation(
    row: Any,
    values: dict[str, Data],
    rows: list[Any],
    entries: list[Data],
    run: Data,
    tasks: list[Any],
    task_id: str | None,
    policy: str,
) -> Data | None:
    from firefly_forge.checkpoint_mode_handoff_custody import validate_completed_handoff

    actor = _acknowledged_continuation(row, values, rows, entries, run, tasks, task_id, policy)
    if actor is not None:
        return actor
    native = _completed_final_ack(row, values, rows, entries, run, policy)
    if native is None:
        return None
    actor, frozen = native
    prepared = _COMPLETED_FINAL.get()
    assert prepared is not None
    base = prepared.terminal["payload"]["proof"]["task_attempt"]
    if run["attempt"] == base and not run.get("verification_handoff"):
        implementation = next((t for t in tasks if t.task_id == "implement"), None)
        return (
            actor
            if (
                task_id is None
                and run.get("status") == "paused"
                and run.get("implementation_pending") is False
                and run.get("implementation_rounds") == base
                and implementation is not None
                and implementation.attempt == base
                and implementation.status == "paused"
                and implementation.data.get("idempotency_key") == prepared.record["payload"]["task"]["idempotency_key"]
            )
            else None
        )
    try:
        custody = validate_completed_handoff(run, entries, rows, values)
    except (ValueError, KeyError):
        return None
    handoff = next(e for e in entries if e["digest"] == custody["handoff_intent_digest"])
    intent = handoff["payload"]
    if handoff["sequence"] <= prepared.terminal["sequence"] or intent["parent_round"] < base:
        return None
    return actor if _completed_final_phase(run, tasks, entries, intent["parent_round"] + 1, task_id) else None


async def assert_continuation_current(journal: Any, run_id: str, task_id: str | None = None) -> list[str]:
    from pyfly.data.transaction import current_unit_of_work

    from firefly_forge.known_worker_completions import _entries
    from firefly_forge.project_access import ProjectAccess
    from firefly_forge.sandbox_command_slots import _canonical, _complete, _recovered
    from firefly_forge.store import identifier
    from firefly_forge.unknown_command_acknowledgements import policy_digest
    from firefly_forge.verification_models import check_verification_authority, verification_provider

    prepared_final = _COMPLETED_FINAL.get()
    if prepared_final is not None and current_unit_of_work() is None:
        await prepared_final.current()
    async with journal.store._unit():
        if not any(e.kind == ACKNOWLEDGEMENT for e in await journal.records.find_for_run(identifier(run_id))):
            return []
    prepared = _POLICY.get()
    policy = prepared.digest if prepared is not None and prepared.run_id == run_id else policy_digest()
    async with journal.store._unit():
        native = await journal.records.find_for_run(identifier(run_id))
        if not any(e.kind == ACKNOWLEDGEMENT for e in native):
            return []
        run = await journal.store.get_run(run_id)
        if run.get("verification_handoff", {}).get("status") == "running":
            return []
        entries = _entries(native, run_id)
        if run.get("checkpoint_mode_adoption"):
            from firefly_forge.checkpoint_mode_runtime import assert_mode_actors

            await assert_mode_actors(journal, run, entries)
        rows = await journal.sandboxes.for_run(identifier(run_id))
        values = {row.id.hex: _canonical(row, entries, run_id) for row in rows}
        tasks = await journal.tasks.find_for_run(identifier(run_id))
        risks = []
        acknowledged_ids = {e["payload"].get("execution_id") for e in entries if e["kind"] == ACKNOWLEDGEMENT}
        if not acknowledged_ids <= values.keys():
            raise ValueError("An acknowledged execution disappeared from native custody")
        for row in rows:
            if row.id.hex not in acknowledged_ids:
                if (
                    task_id is None
                    and not _complete(row, values[row.id.hex])
                    and not _recovered(row, values[row.id.hex], entries, tasks)
                ):
                    raise ValueError("An unacknowledged command outcome blocks native continuation")
                continue
            actor = acknowledged_continuation(row, values, rows, entries, run, tasks, task_id, policy)
            if actor is None:
                raise ValueError("Command custody acknowledgement does not authorize this native continuation")
            projection = run
            if not run.get("verification_handoff") and prepared_final is not None:
                projection = prepared_final.record["payload"]["run"]
            intent = next(
                e["payload"]
                for e in entries
                if e["kind"] == "verification_handoff_intent"
                and e["payload"].get("id") == projection["verification_handoff"]["id"]
            )
            check_verification_authority(journal.store, projection, intent)
            if (
                not run.get("checkpoint_mode_adoption")
                and verification_provider(journal.store, projection) != intent["provider_digest"]
            ):
                raise ValueError("The acknowledged continuation provider grant changed")
            await ProjectAccess(journal.store).authorize_actor(actor, run.get("project_id"), "execute")
            risks.append(row.id.hex)
        await journal.store.assert_ownership()
    if prepared_final is not None and current_unit_of_work() is None:
        await prepared_final.current()
    return risks


async def assert_followup_actors(
    journal: Any, run: Data, native_entries: list[Any], intent: Data, policy: str | None
) -> None:
    from firefly_forge.known_worker_completions import _entries
    from firefly_forge.project_access import ProjectAccess
    from firefly_forge.sandbox_command_slots import _canonical
    from firefly_forge.store import identifier

    bound = [
        row
        for row in native_entries
        if row.kind == ACKNOWLEDGEMENT
        and row.data["payload"].get("followup", {}).get("adoption_request_id") == intent.get("adoption_request_id")
    ]
    if not bound:
        return
    if policy is None:
        raise ValueError("Unknown-command dispatch requires its prepared acknowledgement policy")
    entries = _entries(native_entries, run["id"])
    rows = {row.id.hex: row for row in await journal.sandboxes.for_run(identifier(run["id"]))}
    values = {key: _canonical(row, entries, run["id"]) for key, row in rows.items()}
    for record in bound:
        row = rows.get(record.data["payload"].get("execution_id"))
        if row is None:
            raise ValueError("The acknowledged command disappeared before verification dispatch")
        value = _canonical(row, entries, run["id"])
        actor = acknowledged_followup(
            row, value, entries, run, intent["ordinals"][0], policy, rows=list(rows.values()), values=values
        )
        if actor is None:
            raise ValueError("Unknown-command acknowledgement changed before verification dispatch")
        await ProjectAccess(journal.store).authorize_actor(actor, run.get("project_id"), "execute")
    await journal.store.assert_ownership()
