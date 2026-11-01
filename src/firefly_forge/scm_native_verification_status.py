# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Read native QA through current authenticated ticket scope, without borrowing execution consent."""

from __future__ import annotations

import copy
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

from firefly_forge.final_outcome_models import RetainedVerifiedSourceV1
from firefly_forge.journal import digest
from firefly_forge.known_worker_completions import _entries, _task
from firefly_forge.linked_verification_models import KIND, LinkedVerificationReceiptV1, linked_verification_frontier
from firefly_forge.native_terminal_completion_models import validate_native_receipt
from firefly_forge.project_access import AccessDenied
from firefly_forge.scm_conversation_models import ConversationReadScopeV1, TicketContextReferenceV1
from firefly_forge.scm_conversations import SCMConversations
from firefly_forge.scm_native_verification_status_models import (
    SCMNativeVerificationOperationV1,
    SCMNativeVerificationStatusV1,
)
from firefly_forge.store import identifier
from firefly_forge.verification_models import verification_scope

Data = dict[str, Any]
PAGE_SIZE = 64


class SCMNativeVerificationStatus:
    def __init__(self, collaboration: Any, verifications: Any) -> None:
        self.collaboration, self.verifications = collaboration, verifications
        self.store = collaboration.store
        self.conversations = SCMConversations(collaboration)

    async def _authorize(self, delivery: Data, scope: Data, reference: Data) -> Data:
        current = await self.conversations.scopes(delivery, remote=False)
        if len(current) != 1 or ConversationReadScopeV1.model_validate(current[0]).model_dump(mode="json") != scope:
            raise AccessDenied("The current registered ticket reader or exact project scope changed")
        await self.conversations.resolve_reference(reference, delivery, current)
        run = await self.store.get_run(reference["run_id"])
        if (
            scope["verified_receipt_id"] != delivery["id"]
            or run["id"] != reference["run_id"]
            or run.get("project_id") != scope["project_id"]
            or reference["project_id"] != scope["project_id"]
            or run["repository_id"] != scope["repository_id"]
            or delivery["repository_id"] != scope["repository_id"]
            or run.get("component_id") != reference["component_id"]
            or run.get("component_id") not in scope["component_ids"]
        ):
            raise AccessDenied("Native verification status belongs to another authenticated ticket or run")
        return copy.deepcopy(run)

    async def _operations(self, run_id: str) -> AsyncIterator[tuple[Any, list[Data]]]:
        records = self.verifications.requests.records
        async with self.store._unit():
            expected = await records.count_for_run(identifier(run_id), KIND)
        cursor, observed = None, 0
        while True:
            async with self.store._unit():
                rows = await records.for_run_page(identifier(run_id), KIND, after=cursor, limit=PAGE_SIZE)
                snapshots = []
                for row in rows:
                    if row.run_id.hex != run_id or row.kind != KIND or cursor is not None and row.id <= cursor:
                        raise ValueError("Native verification history lost its exact monotonic run cursor")
                    cursor = row.id
                    snapshot = SimpleNamespace(
                        id=row.id,
                        run_id=row.run_id,
                        kind=row.kind,
                        status=row.status,
                        request_key=row.request_key,
                        data=copy.deepcopy(row.data),
                    )
                    snapshots.append((snapshot, await self.verifications._events(snapshot)))
            for value in snapshots:
                observed += 1
                yield value
            if len(rows) < PAGE_SIZE:
                break
        async with self.store._unit():
            actual = await records.count_for_run(identifier(run_id), KIND)
        if observed != expected or actual != expected:
            raise ValueError("Native verification history changed while its complete ledger was read")

    @staticmethod
    def _parent_key(run: Data, candidate: Any, entries: list[Data]) -> tuple[str, str, int]:
        proof = RetainedVerifiedSourceV1.model_validate(run["usage"]["retained_source"])
        if (
            proof.run_id != run["id"]
            or proof.attempt != run.get("attempt")
            or proof.scope_digest != verification_scope(run)
            or proof.commands_digest != digest({"commands": run["commands"]})
            or proof.sandbox_digest != run["sandbox_snapshot"]["digest"]
        ):
            raise ValueError("Native verification status changed its recorded accepted source scope")
        receipts = [entry for entry in entries if entry["digest"] == candidate.implementation_receipt_digest]
        terminals = [entry for entry in entries if entry["digest"] == candidate.terminal_receipt_digest]
        if (
            len(receipts) != 1
            or len(terminals) != 1
            or receipts[0]["kind"] != "receipt"
            or receipts[0]["task_id"] != "implement"
            or receipts[0]["idempotency_key"] != proof.task_key
            or receipts[0]["payload"].get("source_tree") != candidate.source_tree
        ):
            raise ValueError("Native verification lost its exact accepted implementation receipt")
        terminal = validate_native_receipt(terminals[0], entries)
        if (
            terminal.proof.task_key != proof.task_key
            or terminal.proof.task_attempt != proof.attempt
            or terminal.proof.retained_source != proof
        ):
            raise ValueError("Native verification terminal evidence belongs to another source or attempt")
        return candidate.implementation_receipt_digest, candidate.terminal_receipt_digest, proof.attempt

    async def _observe(self, run: Data, scope: Data) -> SCMNativeVerificationStatusV1 | None:
        if not run.get("usage", {}).get("retained_source") or run.get("implementation_pending") is not False:
            return None
        async with self.store._unit():
            entries = _entries(await self.verifications.journal.records.find_for_run(identifier(run["id"])), run["id"])
            task = _task(await self.verifications.journal._task(run["id"], "implement"))
        proof = RetainedVerifiedSourceV1.model_validate(run["usage"]["retained_source"])
        if task["status"] != "done" or task["attempt"] != run["attempt"] or task["idempotency_key"] != proof.task_key:
            raise ValueError("Native verification status requires its exact current accepted implementation")
        receipts = [
            entry
            for entry in entries
            if entry["kind"] == "receipt"
            and entry["task_id"] == "implement"
            and entry["idempotency_key"] == proof.task_key
        ]
        terminals = [
            entry
            for entry in entries
            if entry["kind"] == "terminal_completion_receipt"
            and (entry["payload"].get("proof") or {}).get("retained_source") == proof.model_dump(mode="json")
        ]
        if len(receipts) != 1 or len(terminals) != 1:
            raise ValueError("The current accepted implementation or terminal receipt is ambiguous")
        current_key = (receipts[0]["digest"], terminals[0]["digest"], run["attempt"])
        groups: dict[tuple[str, str, int], list[LinkedVerificationReceiptV1]] = {}
        current_operations = []
        for_scope = ("project_id", "repository_id", "component_id")
        async for row, events in self._operations(run["id"]):
            parent = row.data["parent_run"]
            if parent.get("id") != run["id"] or any(parent.get(key) != run.get(key) for key in for_scope):
                raise AccessDenied("A native verification record belongs to another project, repository or component")
            receipt = LinkedVerificationReceiptV1.model_validate(await self.verifications._receipt(row, events))
            key = self._parent_key(parent, receipt.candidate, entries)
            groups.setdefault(key, []).append(receipt)
            if key == current_key:
                current_operations.append(
                    SCMNativeVerificationOperationV1.model_validate(
                        dict(
                            receipt=receipt,
                            final_event_kind=events[-1]["kind"],
                            hold_reason=events[-1]["evidence"]["reason"]
                            if events[-1]["kind"] == "linked_verification_held"
                            else None,
                            results=tuple(
                                dict(
                                    ordinal=e["evidence"]["ordinal"],
                                    exit_code=e["evidence"]["exit_code"],
                                    command_digest=digest(row.data["commands"][e["evidence"]["ordinal"] - 1]),
                                    event_digest=e["digest"],
                                )
                                for e in events
                                if e["kind"] == "linked_verification_command_result"
                            ),
                        )
                    )
                )
        for key, values in groups.items():
            linked_verification_frontier(
                values,
                run_id=run["id"],
                implementation_receipt_digest=key[0],
                terminal_receipt_digest=key[1],
                parent_attempt=key[2],
            )
        current = linked_verification_frontier(
            groups.get(current_key, []),
            run_id=run["id"],
            implementation_receipt_digest=current_key[0],
            terminal_receipt_digest=current_key[1],
            parent_attempt=current_key[2],
        )
        others = [receipt for key, values in groups.items() if key != current_key for receipt in values]
        value = dict(
            version=1,
            source="validated_native_request_events",
            run_id=run["id"],
            project_id=run["project_id"],
            repository_id=run["repository_id"],
            component_id=run["component_id"],
            attempt=run["attempt"],
            implementation_receipt_digest=current_key[0],
            terminal_receipt_digest=current_key[1],
            source_tree=receipts[0]["payload"]["source_tree"],
            read_scope_digest=digest(scope),
            parent_run_digest=digest(run),
            current_request_id=current.request_id if current else None,
            operations=tuple(operation.model_dump() for operation in current_operations),
            other_implementation_operations=len(others),
            other_implementation_digest=digest({"receipts": [receipt.model_dump(mode="json") for receipt in others]}),
            execution_grant=False,
        )
        if current is not None:
            operation = next(op for op in current_operations if op.receipt.request_id == current.request_id)
            if (
                current.status == "needs_reconciliation"
                and operation.final_event_kind == "linked_verification_held"
                and current.progress.started == current.progress.completed
                and not current.progress.unknown
                and not current.progress.failed
            ):
                value["held_predecessor"] = dict(
                    request_id=current.request_id,
                    event_digest=current.event_digest,
                    parent_attempt=run["attempt"],
                    review_only=True,
                )
        return SCMNativeVerificationStatusV1.model_validate({**value, "digest": digest(value)})

    async def read(self, delivery: Data, scope: Data, reference: Data) -> SCMNativeVerificationStatusV1 | None:
        delivery = copy.deepcopy(delivery)
        scope = ConversationReadScopeV1.model_validate(scope).model_copy(deep=True).model_dump(mode="json")
        reference = TicketContextReferenceV1.model_validate(reference).model_copy(deep=True).model_dump(mode="json")
        run = await self._authorize(delivery, scope, reference)
        result = await self._observe(run, scope)
        current_run = await self._authorize(delivery, scope, reference)
        fresh = await self._observe(current_run, scope)
        final_run = await self._authorize(delivery, scope, reference)
        if (result.digest if result else None) != (fresh.digest if fresh else None):
            raise ValueError("The native verification frontier changed before read disclosure")
        if final_run != current_run:
            raise AccessDenied("The accepted parent changed before native verification disclosure")
        return result

    async def for_delivery(self, delivery: Data) -> SCMNativeVerificationStatusV1 | None:
        delivery = copy.deepcopy(delivery)
        if not delivery.get("run_id"):
            return None
        run = await self.store.get_run(delivery["run_id"])
        if not run.get("usage", {}).get("retained_source") or run.get("implementation_pending") is not False:
            return None
        scopes = await self.conversations.scopes(delivery, remote=False)
        references, _ = await self.conversations.references(delivery, scopes, durable=True)
        matching = [reference for reference in references if reference["run_id"] == delivery["run_id"]]
        if len(scopes) != 1 or len(matching) != 1:
            raise AccessDenied("Native verification status requires one exact authenticated ticket/run reference")
        return await self.read(delivery, scopes[0], matching[0])

    async def assert_current(
        self, view: SCMNativeVerificationStatusV1, delivery: Data, scope: Data, reference: Data
    ) -> None:
        expected = SCMNativeVerificationStatusV1.model_validate_json(view.model_dump_json()).digest
        actual = await self.read(delivery, scope, reference)
        if (
            actual is None
            or actual.digest != expected
            or SCMNativeVerificationStatusV1.model_validate_json(view.model_dump_json()).digest != expected
        ):
            raise AccessDenied("The exact native verification read projection changed before publication")
