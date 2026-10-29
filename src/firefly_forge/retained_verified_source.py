# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Native authority to finish unchanged source after an accepted verification blocker."""

from __future__ import annotations

import copy
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from firefly_forge.bridge_completions import _outside_unit
from firefly_forge.checkpoint_mode_handoff_custody import validate_completed_handoff
from firefly_forge.final_outcome_continuations import _same, final_record
from firefly_forge.final_outcome_models import RetainedVerifiedSourceV1
from firefly_forge.invalid_read_dispatch_guard import assert_generic_dispatch_allowed
from firefly_forge.invalid_read_recovery_events import read_recovery_events
from firefly_forge.journal import digest
from firefly_forge.native_terminal_completion_models import validate_native_receipt
from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied, ProjectAccess
from firefly_forge.run_evidence import LIMITS
from firefly_forge.sandbox_command_slots import _canonical
from firefly_forge.store import identifier
from firefly_forge.verification_models import check_verification_authority
from firefly_forge.verification_source_preparation import (
    PreparedVerificationSource,
    prepare_verification_source,
    verification_catalog_root,
)

Data = dict[str, Any]


@dataclass(frozen=True)
class PreparedRetainedSource:
    source: PreparedVerificationSource
    proof: RetainedVerifiedSourceV1
    _current: Callable[[], Awaitable[None]]

    async def revalidate(self) -> None:
        await self.source.revalidate(assert_current=self._current)

    def fence(self) -> None:
        self.source.fence()


async def prepare_retained_source(store: Any, journal: Any, run: Data, workspace: Path) -> PreparedRetainedSource:
    _outside_unit()
    await store.assert_ownership()
    actor = CURRENT_PRINCIPAL.get()
    if actor is None:
        raise AccessDenied("Retained verified source requires its current authenticated worker")
    ownership = store.execution_ownership_token
    expected = copy.deepcopy(run)
    requests = getattr(store, "execution_requests", None)
    marker = run.get("invalid_read_continuity") or {}
    if requests is None or requests.store is not store or not marker.get("request_id"):
        raise ValueError("Unchanged source requires its accepted native final-result action")
    await assert_generic_dispatch_allowed(store, journal, run)
    async with store._unit():
        ticket = await requests.get(marker["request_id"])
        events = await read_recovery_events(requests, ticket["id"], maximum=LIMITS["journal"])
        native_run, native_task, entries = await requests.invalid_read_continuities.recoveries._native(run["id"])
        if not _same(native_run, run):
            raise ValueError("Retained source run changed before its native cut")
        rows = await journal.sandboxes.for_run(identifier(run["id"]))
        task = await journal._task(run["id"], "implement")
    record = final_record(ticket, entries, events)
    state = run.get("verification_handoff") or {}
    if (
        record is None
        or ticket["status"] != "completed"
        or record["payload"]["candidate"]["outcome"] != "blocked"
        or record["payload"]["candidate"]["followup_kind"] != "verification"
        or state.get("status") != "completed"
        or type(state.get("failed_commands")) is not int
        or state["failed_commands"] != 0
        or type(run.get("attempt")) is not int
        or run["attempt"] != record["payload"]["candidate"]["attempt"] + 1
        or task.attempt != run["attempt"]
        or task.status != "running"
        or task.data.get("idempotency_key") is None
        or run.get("active_task") != "implement"
    ):
        raise ValueError("Unchanged source requires the exact next attempt after passed native verification")
    values = {row.id.hex: _canonical(row, entries, run["id"]) for row in rows}
    custody = validate_completed_handoff(run, entries, rows, values)
    handoff = next(e for e in entries if e["digest"] == custody["handoff_intent_digest"])
    intent = handoff["payload"]
    accepted = [
        e
        for e in entries
        if e["kind"] == "terminal_completion_receipt"
        and record["sequence"] < e["sequence"] < handoff["sequence"]
        and e["payload"]["proof"]["terminal_report_sha256"] == record["payload"]["candidate"]["report_digest"]
    ]
    if (
        len(accepted) != 1
        or intent["parent_attempt"] != record["payload"]["candidate"]["attempt"]
        or intent["parent_round"] + 1 != run["attempt"]
        or Path(intent["workspace"]) != workspace
    ):
        raise ValueError("Unchanged source lost its accepted blocker or original verification namespace")
    validate_native_receipt(accepted[0], entries)
    check_verification_authority(store, run, intent)

    async def current() -> None:
        await store.assert_ownership()
        if CURRENT_PRINCIPAL.get() != actor:
            raise AccessDenied("Retained source worker context changed")
        await ProjectAccess(store).authorize_actor(
            ProjectAccess.actor_snapshot(actor), expected.get("project_id"), "execute"
        )
        if store.execution_ownership_token is not ownership or digest(await store.get_run(run["id"])) != digest(
            expected
        ):
            raise ValueError("Retained source lost its current worker ownership or scope")
        await ProjectAccess(store).authorize_execution(expected)
        check_verification_authority(store, expected, intent)
        async with store._unit():
            if (
                not _same(await journal.entries(run["id"]), entries)
                or not _same(await requests.get(ticket["id"]), ticket)
                or not _same(await read_recovery_events(requests, ticket["id"], maximum=LIMITS["journal"]), events)
            ):
                raise ValueError("Retained source native custody changed during preparation")

    source = await prepare_verification_source(
        workspace,
        verification_catalog_root(store.path, run["id"]),
        budget=journal.source_catalog_budget,
        assert_current=current,
    )
    if source.source_digest != intent["source_digest"]:
        raise ValueError("Source changed after its passed native verification")
    value = dict(
        version=1,
        kind="retained_verified_final_source",
        run_id=run["id"],
        request_id=ticket["id"],
        attempt=run["attempt"],
        parent_attempt=intent["parent_attempt"],
        task_key=task.data["idempotency_key"],
        final_outcome_witness_digest=record["digest"],
        terminal_receipt_digest=accepted[0]["digest"],
        verification_custody_digest=custody["digest"],
        handoff_intent_digest=custody["handoff_intent_digest"],
        handoff_outcome_digest=custody["outcome_digest"],
        continuation_intent_digest=custody["continuation_intent_digest"],
        source_digest=intent["source_digest"],
        commands_digest=intent["commands_digest"],
        sandbox_digest=intent["sandbox_digest"],
        scope_digest=intent["scope_digest"],
    )
    value["digest"] = digest(value)
    proof = RetainedVerifiedSourceV1.model_validate(value)
    await store.admit_credentials(proof.model_dump(mode="json"))
    await source.revalidate(assert_current=current)
    return PreparedRetainedSource(source, proof, current)


def validate_retained_source_receipt(proof: RetainedVerifiedSourceV1, terminal: Data, entries: list[Data]) -> None:
    def selected(key: str, kind: str) -> Data:
        found = [e for e in entries if e["digest"] == getattr(proof, key) and e["kind"] == kind]
        if len(found) != 1:
            raise ValueError("Retained source receipt lost its unique native predecessor")
        return found[0]

    witness = selected("final_outcome_witness_digest", "final_outcome_continuation")
    previous = selected("terminal_receipt_digest", "terminal_completion_receipt")
    handoff = selected("handoff_intent_digest", "verification_handoff_intent")
    outcome = selected("handoff_outcome_digest", "verification_handoff_outcome")
    coding = selected("continuation_intent_digest", "verification_continuation_intent")
    candidate = witness["payload"]["candidate"]
    accepted = terminal["payload"]["proof"]
    intent = handoff["payload"]
    if (
        proof.run_id != terminal["run_id"]
        or proof.task_key != accepted["task_key"]
        or proof.attempt != accepted["task_attempt"]
        or proof.request_id != candidate["request_id"]
        or candidate["outcome"] != "blocked"
        or candidate["followup_kind"] != "verification"
        or proof.parent_attempt != candidate["attempt"]
        or previous["payload"]["proof"]["terminal_report_sha256"] != candidate["report_digest"]
        or any(
            getattr(proof, key) != intent[key]
            for key in ("source_digest", "commands_digest", "sandbox_digest", "scope_digest")
        )
        or intent["parent_attempt"] != proof.parent_attempt
        or intent["parent_round"] + 1 != proof.attempt
        or outcome["payload"].get("status") != "completed"
        or outcome["payload"].get("failed_commands") != 0
        or outcome["payload"].get("id") != intent["id"]
        or coding["payload"].get("id") != intent["id"]
        or coding["payload"].get("next_round") != proof.attempt
        or not witness["sequence"]
        < previous["sequence"]
        < handoff["sequence"]
        < outcome["sequence"]
        < coding["sequence"]
        < terminal["sequence"]
    ):
        raise ValueError("Retained source receipt changed its accepted blocker or verified continuation")
    validate_native_receipt(previous, entries)
