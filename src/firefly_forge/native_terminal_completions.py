# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Prepare known output outside the writer; accept it with its real native row."""

from __future__ import annotations

import copy
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from firefly_forge.bridge_completions import BridgeCompletions, _outside_unit
from firefly_forge.journal import Journal, digest
from firefly_forge.known_worker_completion_proof import (
    PreparedWorkerCompletion,
    _eligible,
    _prepared_digest,
    _scope,
    prepare_worker_completion,
)
from firefly_forge.known_worker_completions import _entries, _task
from firefly_forge.native_terminal_completion_models import NativeTerminalReceiptV1, validate_native_receipt
from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied, ProjectAccess
from firefly_forge.store import identifier, prepared_credential_rejection

Data = dict[str, Any]
SETTLEMENT = "terminal_completion_receipt"


class NativeAcceptance:
    def __init__(
        self,
        journal: Journal,
        run: Data,
        task: Data,
        prepared: PreparedWorkerCompletion,
        kind: str,
        key: str,
        payload: Data,
        checkpoint: Data | None,
        ownership: object,
        restored: Any = None,
    ) -> None:
        self.journal, self.store = journal, journal.store
        self.run, self.task, self.prepared = run, task, prepared
        self.kind, self.key, self.payload, self.checkpoint = kind, key, payload, checkpoint
        self.actor = CURRENT_PRINCIPAL.get()
        self.ownership = ownership
        self.restored = restored
        self.completions = BridgeCompletions(self.store, journal)

    async def before(self) -> None:
        await self.store.assert_ownership()
        if self.store.execution_ownership_token is not self.ownership or CURRENT_PRINCIPAL.get() != self.actor:
            raise AccessDenied("Native terminal acceptance lost its original execution lease or context")
        current = await self.store.get_run(self.run["id"])
        operator = (
            await self.restored.restoration.service.operator(self.restored.ticket["request"]["actor"])
            if self.restored is not None
            else None
        )
        await self.completions._writer(current, operator)
        await self.journal._run(current["id"])
        current = await self.store.get_run(current["id"])
        await self.completions._execution(current, self.actor)
        if self.restored is not None:
            await self.restored.current()
        task = _task(await self.journal._task(current["id"], "implement"))
        entries = _entries(await self.journal.records.find_for_run(identifier(current["id"])), current["id"])
        if (
            current != self.run
            or task != self.task
            or len(entries) != self.prepared.prefix_count
            or digest({"entries": entries}) != self.prepared.prefix_digest
            or _prepared_digest(self.prepared) != self.prepared.seal
        ):
            raise ValueError("Native terminal acceptance changed after preparation")
        self.store.reject_credentials(self.prepared.report)

    async def commit(self, acceptance: Data) -> None:
        current = await self.store.get_run(self.run["id"])
        task = _task(await self.journal._task(current["id"], "implement"))
        entries = _entries(await self.journal.records.find_for_run(identifier(current["id"])), current["id"])
        prepared = self.prepared
        _eligible(current, task, prepared.feedback or None, accepted=True)
        suffix = entries[prepared.prefix_count :]
        if (
            _prepared_digest(prepared) != prepared.seal
            or _scope(current, task) != prepared.scope_digest
            or digest({"entries": entries[: prepared.prefix_count]}) != prepared.prefix_digest
            or (current.get("usage") or {}).get("runtime_report") != prepared.report
            or acceptance.get("kind") != self.kind
            or acceptance.get("idempotency_key") != self.key
            or acceptance.get("payload") != self.payload
            or len([entry for entry in entries if entry["digest"] == acceptance["digest"]]) != 1
            or any(
                entry != acceptance
                and not (
                    self.kind == "worker_feedback"
                    and entry["kind"] == "worker_evidence"
                    and entry["payload"].get("feedback_id") == prepared.feedback.get("id")
                )
                for entry in suffix
            )
            or len(suffix) > (2 if self.kind == "worker_feedback" else 1)
        ):
            raise ValueError("Native terminal result differs from its exact acceptance cut")
        if self.kind == "worker_feedback":
            if (
                not prepared.feedback
                or current.get("worker_feedback") != prepared.feedback
                or self.payload != prepared.feedback
            ):
                raise ValueError("Native terminal feedback differs from its real sanitized acceptance")
        elif (
            prepared.feedback
            or task["status"] != "done"
            or task.get("idempotency_key") != self.key
            or not self.checkpoint
            or self.payload.get("source_tree") != self.checkpoint.get("source_tree")
            or any(current.get("checkpoint", {}).get(key) != value for key, value in self.checkpoint.items())
        ):
            raise ValueError("Normal terminal completion requires its actual successful source receipt")
        proof = {
            "version": 1,
            "run_id": current["id"],
            "task_id": "implement",
            "task_attempt": task["attempt"],
            "task_key": task["idempotency_key"],
            "runtime_contract": prepared.runtime_contract,
            "completion_count": len(prepared.completions),
            "completions": list(prepared.completions),
            "terminal_report_sha256": digest(prepared.report or {}),
            "final_response_sha256": prepared.output_sha256,
            "observed_hold_digest": digest(current["bridge_completion_hold"]),
            "acceptance_kind": self.kind,
            "acceptance_record_digest": acceptance["digest"],
            "accepted_payload_sha256": digest(acceptance["payload"]),
            "source_checkpoint": self.checkpoint,
            "verification": "native_terminal_output_known_only",
        }
        proof["digest"] = digest(proof)
        payload = NativeTerminalReceiptV1.model_validate(
            {
                "version": 1,
                "run_id": current["id"],
                "producer": "native_implementation_receipt" if self.kind == "receipt" else "native_worker_feedback",
                "actor_digest": digest(current["execution_actor"]),
                "proof": proof,
                "observed_hold": copy.deepcopy(current["bridge_completion_hold"]),
            }
        ).model_dump(mode="json")
        self.store.reject_credentials(payload)
        record = await self.journal._append(
            current["id"], "implement", "terminal-completion:" + acceptance["digest"][:40], SETTLEMENT, payload
        )
        row = await self.journal._run(current["id"])
        row.data = {
            **row.data,
            "bridge_completion_hold": {},
            "bridge_completion_settlement": {
                "version": 1,
                "producer": payload["producer"],
                "record_digest": record["digest"],
                "proof_digest": proof["digest"],
                "acceptance_record_digest": acceptance["digest"],
            },
        }
        await self.store.run_records.save(row)
        await self.completions._execution(await self.store.get_run(current["id"]), self.actor)
        if self.restored is not None:
            await self.restored.current()
        await self.store.assert_ownership()
        if self.store.execution_ownership_token is not self.ownership or CURRENT_PRINCIPAL.get() != self.actor:
            raise AccessDenied("Native terminal acceptance lease or context changed before commit")
        self.store.reject_credentials(record)


@asynccontextmanager
async def native_terminal_acceptance(
    journal: Journal,
    run_id: str,
    kind: str,
    key: str,
    payload: Data,
    checkpoint: Data | None = None,
    *,
    restoration: Any = None,
) -> AsyncIterator[NativeAcceptance | None]:
    store = journal.store
    run = await store.get_run(run_id)
    if not run.get("bridge_completion_hold"):
        if (run.get("bridge_completion_settlement") or {}).get("producer"):
            await _admit_duplicate(journal, run, kind, key, payload, checkpoint)
        yield None
        return
    if not (run.get("usage") or {}).get("runtime_report"):
        yield None
        return
    _outside_unit()
    if kind not in {"receipt", "worker_feedback"}:
        raise ValueError("Native terminal acceptance requires its real acceptance writer")
    with prepared_credential_rejection(store):
        await store.assert_ownership()
        ownership = store.execution_ownership_token
        actor = CURRENT_PRINCIPAL.get()
        await BridgeCompletions(store, journal)._execution(run, actor)
        from firefly_forge.native_terminal_restore_proof import prepare_restore_acceptance

        restored = await prepare_restore_acceptance(restoration) if restoration is not None else None
        await journal.entries(run_id)
        async with store._unit():
            current = await store.get_run(run_id)
            task = _task(await journal._task(run_id, "implement"))
            entries = _entries(await journal.records.find_for_run(identifier(run_id)), run_id)
        if current != run:
            raise ValueError("Native terminal run changed during credential admission")
        if kind == "receipt" and (task["idempotency_key"] != key or not checkpoint):
            raise ValueError("Normal terminal completion requires its actual task and source checkpoint")
        prepared = prepare_worker_completion(
            run,
            task,
            entries,
            feedback=payload if kind == "worker_feedback" else None,
            terminal_report=run["usage"]["runtime_report"],
            restored=restored,
        )
        await store.admit_credentials({"report": prepared.report, "payload": payload, "checkpoint": checkpoint})
        if CURRENT_PRINCIPAL.get() != actor:
            raise AccessDenied("Native terminal execution context changed during preparation")
        yield NativeAcceptance(journal, run, task, prepared, kind, key, payload, checkpoint, ownership, restored)


async def _admit_duplicate(
    journal: Journal, run: Data, kind: str, key: str, payload: Data, checkpoint: Data | None
) -> None:
    _outside_unit()
    store = journal.store
    actor = CURRENT_PRINCIPAL.get()
    with prepared_credential_rejection(store):
        await journal.entries(run["id"])
        async with store._unit():
            entries = _entries(await journal.records.find_for_run(identifier(run["id"])), run["id"])
        accepted = [entry for entry in entries if entry["kind"] == kind and entry["idempotency_key"] == key]
        receipts = [
            entry
            for entry in entries
            if entry["kind"] == SETTLEMENT
            and accepted
            and entry["payload"]["proof"]["acceptance_record_digest"] == accepted[0]["digest"]
        ]
        if not receipts:
            return
        if len(receipts) != 1 or len(accepted) != 1:
            raise ValueError("Native terminal acceptance has duplicate immutable identities")
        receipt = validate_native_receipt(receipts[0], entries)
        if (
            payload != accepted[0]["payload"]
            or checkpoint != receipt.proof.source_checkpoint
            or digest((run.get("usage") or {}).get("runtime_report") or {}) != receipt.proof.terminal_report_sha256
        ):
            raise ValueError("Native terminal acceptance cannot replace its original result")
        await store.admit_credentials({"receipt": receipts[0], "payload": payload})
        await store.assert_ownership()
        async with store._unit():
            completions = BridgeCompletions(store, journal)
            current = await store.get_run(run["id"])
            await completions._writer(current)
            await journal._run(run["id"])
            await ProjectAccess(store).authorize_execution(current)
            latest = _entries(await journal.records.find_for_run(identifier(run["id"])), run["id"])
            if current != run or latest != entries or CURRENT_PRINCIPAL.get() != actor:
                raise AccessDenied("Native terminal duplicate changed during current admission")
            store.reject_credentials({"receipt": receipts[0], "payload": payload})
