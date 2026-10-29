# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Native QA may reference fresh-scope local checks without charging or executing them again."""

from __future__ import annotations

import copy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from firefly_forge.checkpoint_mode_handoff_custody import validate_completed_handoff
from firefly_forge.final_outcome_models import RetainedVerifiedSourceV1
from firefly_forge.invalid_read_recovery_events import read_recovery_events
from firefly_forge.journal import digest
from firefly_forge.native_terminal_completion_models import validate_native_receipt
from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied, ProjectAccess
from firefly_forge.run_evidence import LIMITS
from firefly_forge.sandbox_command_slots import _canonical
from firefly_forge.sandbox_models import validate_sandbox_snapshot
from firefly_forge.source_catalog import _json
from firefly_forge.store import identifier
from firefly_forge.verification_models import check_verification_authority
from firefly_forge.verification_scope_attestation import PreparedBuildScope, prepare_build_scope, scope_record
from firefly_forge.verification_source_preparation import (
    PreparedVerificationSource,
    prepare_verification_source,
    verification_catalog_root,
)

Data = dict[str, Any]


def retained_check(argv: list[str], index: int, attempt: int, record: Data, value: Data) -> Data:
    effect = value["effects"]["start"]
    result = effect.get("evidence", {})
    if effect.get("status") != "done" or type(result.get("exit_code")) is not int or result["exit_code"] != 0:
        raise ValueError("Failed or unknown checks cannot be reused")
    return dict(
        argv=argv,
        exit_code=0,
        output=result.get("output", ""),
        created_at=record["created_at"],
        attempt=attempt,
        configured_check_index=index,
        execution="reused",
        source_execution_id=record["payload"]["execution_id"],
        source_record_digest=record["digest"],
    )


@dataclass
class PreparedVerificationReuse:
    engine: Any
    run: Data
    intent: Data
    key: str
    source: PreparedVerificationSource
    scope: PreparedBuildScope
    payload: Data
    checks: dict[int, Data]
    ownership: object
    actor: Any
    ticket: Data
    events: list[Data]
    verify_attempt: int
    record: Data | None = None

    async def current(self, *, completed: bool = False) -> None:
        store, journal = self.engine.store, self.engine.journal
        await store.assert_ownership()
        if store.execution_ownership_token is not self.ownership or CURRENT_PRINCIPAL.get() != self.actor:
            raise AccessDenied("Verification reuse lost its native worker ownership")
        current = await store.get_run(self.run["id"])
        await ProjectAccess(store).authorize_execution(current)
        await store.assert_campaign_current(current)
        check_verification_authority(store, current, self.intent)
        if (
            type(current.get("attempt")) is not int
            or current["attempt"] != self.run["attempt"]
            or _json(current.get("usage")) != _json(self.run.get("usage"))
        ):
            raise ValueError("Verification reuse changed its retained report or spent usage")
        async with store._unit():
            task = await journal._task(current["id"], "verify")
        if (
            task.data.get("idempotency_key") != self.key
            or type(task.attempt) is not int
            or task.attempt != self.verify_attempt
            or task.status != ("done" if completed else "running")
        ):
            raise ValueError("Verification reuse requires its exact current native verify task")
        requests = store.execution_requests
        if _json(await requests.get(self.ticket["id"])) != _json(self.ticket) or _json(
            await read_recovery_events(requests, self.ticket["id"], maximum=LIMITS["journal"])
        ) != _json(self.events):
            raise ValueError("Verification reuse changed its original final-result admission")

    async def revalidate(self) -> None:
        await self.source.revalidate(assert_current=self.current)
        await self.scope.revalidate()
        await self.current()

    async def begin(self) -> None:
        await self.revalidate()
        async with self.engine.store._unit():
            await self.engine.journal._run(self.run["id"])
            await self.current()
            entries = await self.engine.journal.entries(self.run["id"])
            existing = [
                e for e in entries if e["kind"] == "verification_reuse_intent" and e["idempotency_key"] == self.key
            ]
            if existing:
                if len(existing) != 1 or _json(existing[0]["payload"]) != _json(self.payload):
                    raise ValueError("Verification reuse cannot replace a retained native intent")
                self.record = existing[0]
            else:
                self.record = await self.engine.journal._append(
                    self.run["id"], "verify", self.key, "verification_reuse_intent", self.payload
                )
            self.source.fence()

    def check(self, index: int) -> Data | None:
        value = self.checks.get(index)
        if value is None:
            return None
        if self.record is None:
            raise ValueError("Reused observations require their committed native intent")
        return {**copy.deepcopy(value), "verification_reuse_intent_digest": self.record["digest"]}

    async def finish(self, final: list[Data]) -> None:
        if self.record is None:
            raise ValueError("Verification reuse has no committed intent")
        await self.current(completed=True)
        self.source.fence()
        entries = await self.engine.journal.entries(self.run["id"])
        receipts = [
            e
            for e in entries
            if e["kind"] == "receipt" and e["task_id"] == "verify" and e["idempotency_key"] == self.key
        ]
        if len(receipts) != 1 or _json(receipts[0]["payload"]["checks"]) != _json(final):
            raise ValueError("Reused QA requires its real current verify task receipt")
        for index in self.checks:
            wanted = self.check(index)
            if sum(_json(check) == _json(wanted) for check in final) != 1:
                raise ValueError("Verify task omitted or changed a referenced reused check")
        payload = {
            **self.payload,
            "checks": [self.check(index) for index in self.checks],
            "intent_digest": self.record["digest"],
            "verify_task_receipt_digest": receipts[0]["digest"],
        }
        old = [e for e in entries if e["kind"] == "verification_reuse_receipt" and e["idempotency_key"] == self.key]
        if old and (len(old) != 1 or _json(old[0]["payload"]) != _json(payload)):
            raise ValueError("Verification reuse receipt cannot be replaced")
        await self.engine.journal._append(self.run["id"], "verify", self.key, "verification_reuse_receipt", payload)


async def prepare_verification_reuse(
    engine: Any, run: Data, workspace: Path, key: str, commands: list[Any]
) -> PreparedVerificationReuse | None:
    retained = run.get("usage", {}).get("retained_source")
    if retained is None or engine.journal is None:
        return None
    store, journal = engine.store, engine.journal
    proof = RetainedVerifiedSourceV1.model_validate(retained)
    if _json(proof.model_dump(mode="json")) != _json(retained):
        raise ValueError("Retained source raw JSON types differ from their native proof")
    await store.assert_ownership()
    entries = await journal.entries(run["id"])
    terminal = [
        e
        for e in entries
        if e["kind"] == "terminal_completion_receipt"
        and _json(e["payload"]["proof"].get("retained_source")) == _json(retained)
    ]
    if len(terminal) != 1:
        raise ValueError("Verification reuse requires its unique retained-source terminal receipt")
    validate_native_receipt(terminal[0], entries)
    async with store._unit():
        rows = await journal.sandboxes.for_run(identifier(run["id"]))
        task = await journal._task(run["id"], "verify")
        verify_attempt = task.attempt
        if type(verify_attempt) is not int or task.data.get("idempotency_key") != key:
            raise ValueError("Verification reuse requires the current native task intent")
    values = {r.id.hex: _canonical(r, entries, run["id"]) for r in rows}
    custody = validate_completed_handoff(run, entries, rows, values)
    handoff = next(e for e in entries if e["digest"] == custody["handoff_intent_digest"])
    intent = handoff["payload"]
    old_scope = scope_record(run, intent, entries)
    config = validate_sandbox_snapshot(run["sandbox_snapshot"])
    available = config.command_limit - len(rows)
    if old_scope is None:
        if len(commands) > available:
            raise ValueError("No fresh reusable build scope; remaining original command budget is insufficient")
        return None
    requests = store.execution_requests
    ticket = await requests.get(proof.request_id)
    events = await read_recovery_events(requests, ticket["id"], maximum=LIMITS["journal"])
    ownership, actor = store.execution_ownership_token, CURRENT_PRINCIPAL.get()

    async def current() -> None:
        await store.assert_ownership()
        if store.execution_ownership_token is not ownership or CURRENT_PRINCIPAL.get() != actor:
            raise AccessDenied("Verification input preparation lost worker ownership")
        saved = await store.get_run(run["id"])
        await ProjectAccess(store).authorize_execution(saved)
        check_verification_authority(store, saved, intent)
        if _json(saved.get("usage")) != _json(run.get("usage")) or saved.get("attempt") != run.get("attempt"):
            raise ValueError("Verification input preparation changed its retained final source")

    source = await prepare_verification_source(
        workspace,
        verification_catalog_root(store.path, run["id"]),
        budget=journal.source_catalog_budget,
        assert_current=current,
    )
    if source.source_digest != intent["source_digest"]:
        raise ValueError("Source changed after its passed native handoff")
    scope = await prepare_build_scope(store, journal, engine, run, intent, handoff, current)
    if scope is None or _json(scope.witness.model_dump(mode="json")) != _json(old_scope.model_dump(mode="json")):
        raise ValueError("Build scope changed; prior verification results cannot be reused")
    selected = {}
    for check in old_scope.checks:
        index = check.command_index
        if index > len(commands) or _json(commands[index - 1].model_dump()) != _json(run["commands"][index - 1]):
            continue
        record = next(
            e
            for e in entries
            if e["kind"] == "verification_handoff_command"
            and e["payload"]["id"] == intent["id"]
            and e["payload"]["command_index"] == index
        )
        selected[index] = retained_check(
            check.argv, index, intent["parent_attempt"], record, values[record["payload"]["execution_id"]]
        )
    if not selected:
        return None
    if len(commands) - len(selected) > available:
        raise ValueError("Fresh verification checks exceed the remaining original command budget")
    payload = dict(
        version=1,
        run_id=run["id"],
        task_key=key,
        source_digest=intent["source_digest"],
        retained_source_receipt_digest=terminal[0]["digest"],
        handoff_intent_digest=handoff["digest"],
        scope_digest=digest(old_scope.model_dump(mode="json")),
        checks=list(selected.values()),
        new_execution_count=0,
    )
    return PreparedVerificationReuse(
        engine,
        copy.deepcopy(run),
        intent,
        key,
        source,
        scope,
        payload,
        selected,
        ownership,
        actor,
        ticket,
        events,
        verify_attempt,
    )
