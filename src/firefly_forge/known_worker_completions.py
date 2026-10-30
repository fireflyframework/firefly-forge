# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Explicit native settlement of known worker output; no model or graph dispatch."""

from __future__ import annotations

import copy
import hashlib
from pathlib import Path
from typing import Any

from jsonschema.exceptions import ValidationError as SchemaError  # type: ignore[import-untyped]
from pyfly.container import service

from firefly_forge.bridge_completions import BridgeCompletions, _outside_unit
from firefly_forge.journal import Journal, digest
from firefly_forge.known_worker_completion_models import (
    ConsumedTerminalInputV2,
    WorkerCompletionCandidateV1,
    WorkerCompletionInputV1,
)
from firefly_forge.known_worker_completion_proof import (
    SETTLEMENT,
    prepare_worker_completion,
    validate_worker_completion,
)
from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied, ProjectAccess
from firefly_forge.store import Store, identifier, prepared_credential_rejection

Data = dict[str, Any]


def policy_digest() -> str:
    _outside_unit()
    root = Path(__file__).parent
    return digest(
        {
            name: hashlib.sha256((root / name).read_bytes()).hexdigest()
            for name in (
                "known_worker_completions.py",
                "known_worker_completion_models.py",
                "known_worker_completion_proof.py",
                "consumed_terminal_completions.py",
            )
        }
    )


def _actor() -> Any:
    actor = CURRENT_PRINCIPAL.get()
    if actor is None or actor.authentication_method not in {"oidc_session", "oidc_bearer", "local_bootstrap"}:
        raise AccessDenied("A current native operator must explicitly review worker completion")
    return actor


def _entries(rows: Any, run_id: str) -> list[Data]:
    result = []
    for row in rows:
        value = copy.deepcopy(row.data)
        if (
            any(
                value.get(key) != item
                for key, item in {
                    "run_id": row.run_id.hex,
                    "sequence": row.sequence,
                    "task_id": row.task_id,
                    "idempotency_key": row.idempotency_key,
                    "kind": row.kind,
                    "digest": row.digest,
                }.items()
            )
            or row.run_id.hex != run_id
        ):
            raise ValueError("Native completion row differs from its canonical columns")
        result.append(value)
    if not Journal.verify_chain(result):
        raise ValueError("Native completion journal is not intact")
    return result


def _task(row: Any) -> Data:
    return {**copy.deepcopy(row.data), "id": row.task_id, "status": row.status, "attempt": row.attempt}


@service
class KnownWorkerCompletions:
    def __init__(self, store: Store, journal: Journal, engine: Any = None) -> None:
        self.store, self.journal = store, journal
        self.engine = engine
        self.completions = BridgeCompletions(store, journal)

    async def _access(self, run_id: str, actor: Any) -> Data:
        run = await self.completions._reader(run_id, actor, execute=True)
        if (
            run.get("status") in {"failed", "cancelled", "interrupted", "succeeded"}
            or (run.get("cancel_request") or {}).get("status") == "requested"
        ):
            raise ValueError("Known worker settlement refuses an ended or cancelled execution")
        await self.completions._execution(run, actor)
        return run

    async def _read(self, run_id: str) -> tuple[Data, Data, list[Data]]:
        actor = _actor()
        run = await self._access(run_id, actor)
        await self.journal.entries(run_id)
        async with self.store._unit():
            current = await self._access(run_id, actor)
            if current != run:
                raise ValueError("Worker completion run changed during custody admission")
            task = await self.journal._task(run_id, "implement")
            rows = await self.journal.records.find_for_run(identifier(run_id))
            return current, _task(task), _entries(rows, run_id)

    async def _prepare(self, run_id: str) -> tuple[Data, Any]:
        actor = _actor()
        run, task, entries = await self._read(run_id)
        try:
            prepared = prepare_worker_completion(run, task, entries)
            proof = validate_worker_completion(prepared, run, task, entries)
        except (ValueError, KeyError, TypeError, SchemaError):
            raise ValueError("Known worker completion is ineligible; inspect its retained native evidence") from None
        value = {
            "version": 1,
            "run_id": run_id,
            "actor_digest": digest(ProjectAccess.actor_snapshot(_actor())),
            "policy_digest": policy_digest(),
            "run_digest": digest(run),
            "journal_digest": digest({"entries": entries}),
            "proof": proof,
        }
        value["digest"] = digest(value)
        candidate = WorkerCompletionCandidateV1.model_validate(value).model_dump(mode="json")
        await self.store.admit_credentials(candidate)
        async with self.store._unit():
            latest = await self._access(run_id, actor)
            rows = await self.journal.records.find_for_run(identifier(run_id))
            if (
                digest(latest) != candidate["run_digest"]
                or digest({"entries": _entries(rows, run_id)}) != candidate["journal_digest"]
            ):
                raise ValueError("Known worker candidate changed during credential admission")
            self.store.reject_credentials(candidate)
        if CURRENT_PRINCIPAL.get() != actor:
            raise AccessDenied("Known worker completion operator changed during preparation")
        return candidate, prepared

    async def candidate(self, run_id: str) -> Data:
        _outside_unit()
        with prepared_credential_rejection(self.store):
            current = await self.store.get_run(identifier(run_id).hex)
            if current.get("status") == "failed":
                from firefly_forge.consumed_terminal_completions import prepare

                return (await prepare(self, identifier(run_id).hex)).candidate
            return (await self._prepare(identifier(run_id).hex))[0]

    @staticmethod
    def _existing(
        entries: list[Data], request_id: str, request: WorkerCompletionInputV1, actor_digest: str
    ) -> Data | None:
        from firefly_forge.known_worker_completion_models import WorkerCompletionReceiptV1

        matches = [
            e for e in entries if e["kind"] == SETTLEMENT and e["idempotency_key"] == "worker-completion:" + request_id
        ]
        if not matches:
            return None
        if len(matches) != 1:
            raise ValueError("Duplicate native completion receipt identity")
        value = WorkerCompletionReceiptV1.model_validate(matches[0]["payload"])
        if (
            value.run_id != matches[0]["run_id"]
            or value.request_id != request_id
            or value.request != request
            or value.actor_digest != actor_digest
        ):
            raise AccessDenied("Worker completion identity belongs to another input or operator")
        return copy.deepcopy(matches[0])

    async def request(
        self, run_id: str, request_id: str, request: WorkerCompletionInputV1 | ConsumedTerminalInputV2
    ) -> Data:
        _outside_unit()
        if isinstance(request, ConsumedTerminalInputV2):
            from firefly_forge.consumed_terminal_completions import request as consumed_request

            return await consumed_request(self, run_id, request_id, request)
        if identifier(request_id).hex != request_id:
            raise ValueError("Use an exact lowercase native completion request identity")
        run_id = identifier(run_id).hex
        request = WorkerCompletionInputV1.model_validate(request.model_dump(mode="json"))
        actor = _actor()
        actor_digest = digest(ProjectAccess.actor_snapshot(actor))
        with prepared_credential_rejection(self.store):
            await self.completions._reader(run_id, actor, execute=True)
            async with self.store._unit():
                rows = await self.journal.records.find_for_run(identifier(run_id))
                existing = self._existing(_entries(rows, run_id), request_id, request, actor_digest)
            if existing is not None:
                return await self._admitted_receipt(run_id, existing, actor, execute=True)
            try:
                candidate, prepared = await self._prepare(run_id)
            except ValueError:
                async with self.store._unit():
                    await self.completions._reader(run_id, actor, execute=True)
                    rows = await self.journal.records.find_for_run(identifier(run_id))
                    existing = self._existing(_entries(rows, run_id), request_id, request, actor_digest)
                if existing is None:
                    raise
                return await self._admitted_receipt(run_id, existing, actor, execute=True)
            expected = {
                "candidate_digest": candidate["digest"],
                "policy_digest": candidate["policy_digest"],
                "expected_run_digest": candidate["run_digest"],
                "journal_digest": candidate["journal_digest"],
                "proof_digest": candidate["proof"]["digest"],
                "feedback_record_digest": candidate["proof"]["feedback_record_digest"],
            }
            if (
                any(getattr(request, key) != value for key, value in expected.items())
                or candidate["actor_digest"] != actor_digest
            ):
                raise ValueError("The reviewed known-worker completion candidate changed")
            await self.store.assert_ownership()
            async with self.store._unit():
                frozen = await self._access(run_id, actor)
                await self.completions._writer(frozen, actor)
                row = await self.journal._run(run_id)
                current = await self._access(run_id, actor)
                rows = await self.journal.records.find_for_run(identifier(run_id))
                entries = _entries(rows, run_id)
                existing = self._existing(entries, request_id, request, actor_digest)
                if existing is not None:
                    self.store.reject_credentials(existing)
                    result = existing
                else:
                    if digest(current) != candidate["run_digest"] or CURRENT_PRINCIPAL.get() != actor:
                        raise AccessDenied("Known worker run or operator changed before settlement")
                    task = await self.journal._task(run_id, "implement")
                    proof = validate_worker_completion(prepared, current, _task(task), entries)
                    if proof != candidate["proof"]:
                        raise ValueError("Known worker proof changed before settlement")
                    result = await self._commit(row, current, request_id, request, candidate, proof, actor)
            return await self._admitted_receipt(run_id, result, actor, execute=True)

    async def _commit(
        self,
        row: Any,
        current: Data,
        request_id: str,
        request: WorkerCompletionInputV1,
        candidate: Data,
        proof: Data,
        actor: Any,
    ) -> Data:
        from firefly_forge.known_worker_completion_models import WorkerCompletionReceiptV1

        payload = WorkerCompletionReceiptV1.model_validate(
            {
                "version": 1,
                "run_id": current["id"],
                "request_id": request_id,
                "actor_digest": candidate["actor_digest"],
                "candidate_digest": candidate["digest"],
                "policy_digest": candidate["policy_digest"],
                "run_digest": candidate["run_digest"],
                "journal_digest": candidate["journal_digest"],
                "request": request.model_dump(mode="json"),
                "proof": proof,
                "observed_hold": copy.deepcopy(current["bridge_completion_hold"]),
            }
        ).model_dump(mode="json")
        self.store.reject_credentials(payload)
        record = await self.journal._append(
            current["id"], "implement", "worker-completion:" + request_id, SETTLEMENT, payload
        )
        row.data = {
            **row.data,
            "bridge_completion_hold": {},
            "bridge_completion_settlement": {
                "version": 1,
                "request_id": request_id,
                "record_digest": record["digest"],
                "proof_digest": proof["digest"],
            },
        }
        await self.store.run_records.save(row)
        await self.store.assert_ownership()
        if CURRENT_PRINCIPAL.get() != actor:
            raise AccessDenied("Known worker completion operator changed before commit")
        self.store.reject_credentials(record)
        return record

    async def get(self, run_id: str, request_id: str) -> Data:
        actor = _actor()
        run_id = identifier(run_id).hex
        await self.completions._reader(run_id, actor)
        async with self.store._unit():
            entries = _entries(await self.journal.records.find_for_run(identifier(run_id)), run_id)
        from firefly_forge.consumed_terminal_completions import find_witness, readback

        if find_witness(entries, request_id) is not None:
            return await readback(self, run_id, request_id)
        matches = [
            e for e in entries if e["kind"] == SETTLEMENT and e["idempotency_key"] == "worker-completion:" + request_id
        ]
        if len(matches) != 1:
            raise KeyError("Known worker completion receipt not found")
        from firefly_forge.known_worker_completion_models import WorkerCompletionReceiptV1

        value = WorkerCompletionReceiptV1.model_validate(matches[0]["payload"])
        if value.run_id != run_id or value.request_id != request_id:
            raise ValueError("Known worker completion receipt scope differs")
        return await self._admitted_receipt(run_id, matches[0], actor, execute=False)

    async def _admitted_receipt(self, run_id: str, record: Data, actor: Any, *, execute: bool) -> Data:
        _outside_unit()
        with prepared_credential_rejection(self.store):
            await self.store.admit_credentials(record)
            async with self.store._unit():
                await self.completions._reader(run_id, actor, execute=execute)
                row = await self.journal.records.bridge_record(
                    identifier(run_id), record["idempotency_key"], SETTLEMENT
                )
                if row is None:
                    raise ValueError("The native worker completion receipt disappeared")
                current = self.completions._validate_record(
                    copy.deepcopy(row.data),
                    run_id,
                    record["idempotency_key"],
                    SETTLEMENT,
                    {
                        "run_id": row.run_id.hex,
                        "sequence": row.sequence,
                        "task_id": row.task_id,
                        "idempotency_key": row.idempotency_key,
                        "kind": row.kind,
                        "digest": row.digest,
                    },
                )
                if current != record or CURRENT_PRINCIPAL.get() != actor:
                    raise AccessDenied("Known worker receipt or reader changed during admission")
                self.store.reject_credentials(record)
            return copy.deepcopy(record)
