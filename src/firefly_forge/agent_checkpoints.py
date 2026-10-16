# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Atomic native checkpoints and consume-before-exposure for retained HTTP results."""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Awaitable, Callable
from typing import Any

from openai.types.chat import ChatCompletion
from pyfly.container import service
from pyfly.data.transaction import current_unit_of_work

from firefly_forge.agent_checkpoint_models import CheckpointPayload, ConsumePayload
from firefly_forge.agentic_checkpoint_boundary import BoundaryCheckpoint
from firefly_forge.bridge_completion_models import CompletionResponsePayload
from firefly_forge.bridge_completion_scope import BridgeCompletionScope
from firefly_forge.bridge_completions import RESPONSE, BridgeCompletions, _cut, _outside_unit, _request_id
from firefly_forge.journal import digest
from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied
from firefly_forge.store import identifier, prepared_credential_rejection

Data = dict[str, Any]
CHECKPOINT = "agent_checkpoint"
CONSUME = "agent_consume"
ENVELOPE_LIMIT = 256 * 1024
CHECKPOINT_LIMIT = 4 * 1024 * 1024
CONSUME_RESERVE = 4096


def _size(value: Data) -> int:
    return len(json.dumps(value, ensure_ascii=False).encode("utf-8"))


@service
class AgentCheckpoints:
    def __init__(self, completions: BridgeCompletions) -> None:
        self.completions = completions
        self.store, self.journal = completions.store, completions.journal

    async def _budget(self, run_id: str, *, extra: int = 0, reserve: bool = False, consume: bool = False) -> None:
        budget = await self.journal.records.checkpoint_budget(identifier(run_id))
        if (
            budget["count"] > 100
            or budget["consumes"] > budget["count"]
            or budget["max_bytes"] > ENVELOPE_LIMIT
            or (reserve and budget["count"] >= 100)
            or budget["bytes"]
            + max(budget["count"] - budget["consumes"] - int(consume), 0) * CONSUME_RESERVE
            + extra
            + (CONSUME_RESERVE if reserve else 0)
            > CHECKPOINT_LIMIT
        ):
            raise ValueError("Agent checkpoint retention is full or exceeds its bounded read")
        await self.completions._budget(
            run_id, payload_bytes=extra + (CONSUME_RESERVE if reserve else 0), checkpoint_consume=consume
        )

    async def _record(self, run_id: str, request_id: str, kind: str) -> Data | None:
        _request_id(request_id)
        await self._budget(run_id)
        row = await self.journal.records.checkpoint_record(identifier(run_id), request_id, kind)
        if row is None:
            return None
        data = self.completions._validate_record(
            copy.deepcopy(row.data),
            run_id,
            request_id,
            kind,
            {
                "digest": row.digest,
                "task_id": row.task_id,
                "sequence": row.sequence,
                "run_id": row.run_id.hex,
                "kind": row.kind,
                "idempotency_key": row.idempotency_key,
            },
        )
        payload = (CheckpointPayload if kind == CHECKPOINT else ConsumePayload).model_validate(data["payload"])
        if payload.request_id != request_id:
            raise ValueError("Checkpoint record identity differs from its envelope")
        return data

    async def _unconsumed(self, run_id: str, request_id: str) -> None:
        if await self._record(run_id, request_id, CONSUME) is not None:
            raise ValueError("Original completion was already consumed; graph reentry is unavailable")

    def scope(
        self,
        run: Data,
        task: Data,
        *,
        capture: Callable[[bytes], BoundaryCheckpoint],
        review: bool = False,
        assert_ownership: Callable[[], Awaitable[None]] | None = None,
    ) -> BridgeCompletionScope:
        frozen, frozen_task = copy.deepcopy(run), copy.deepcopy(task)
        reader = CURRENT_PRINCIPAL.get()
        field = "review_config_revision" if review else "config_revision"
        base = self.completions.scope(frozen, frozen_task, review=review, assert_ownership=assert_ownership)

        async def current() -> None:
            await self.completions._current(frozen, field, reader, assert_ownership)
            if reader is not None:
                value = await self.completions._reader(frozen["id"], reader, execute=True)
                if _cut(value, field) != _cut(frozen, field):
                    raise AccessDenied("Checkpoint current execution scope changed")
            await self.completions._current(frozen, field, reader, assert_ownership)

        async def checkpoint_admit(raw: Data, body: bytes) -> Data:
            _outside_unit()
            if type(body) is not bytes:
                raise ValueError("Checkpoint request requires immutable wire bytes")
            captured = capture(body)
            checkpoint = BoundaryCheckpoint.decode(captured.payload, captured.digest)
            if checkpoint.wire_sha256 != hashlib.sha256(body).hexdigest() or checkpoint.record.wire_bytes != len(body):
                raise ValueError("Checkpoint differs from the original wire request")
            snapshot = checkpoint.payload.decode("utf-8")
            await self.store.admit_credentials(snapshot)
            await current()
            self.store.reject_credentials(snapshot)

            async def retain(intent: Data, wire: bytes) -> None:
                if current_unit_of_work() is None:
                    raise ValueError("Checkpoint retention requires the intent transaction")
                await current()
                request_id = intent["idempotency_key"]
                await self._unconsumed(frozen["id"], request_id)
                payload = CheckpointPayload(
                    version=1,
                    request_id=request_id,
                    intent_digest=intent["digest"],
                    checkpoint_digest=checkpoint.digest,
                    snapshot=snapshot,
                ).model_dump(mode="json")
                prior = await self._record(frozen["id"], request_id, CHECKPOINT)
                if prior is not None:
                    if prior["payload"] != payload:
                        raise ValueError("Completion identity already has a different checkpoint")
                else:
                    extra = _size(payload) + 4096
                    if extra > ENVELOPE_LIMIT:
                        raise ValueError("Checkpoint envelope exceeds 256 KiB")
                    await self._budget(frozen["id"], reserve=True, extra=extra)
                    record = await self.journal._append(
                        frozen["id"], intent["task_id"], request_id, CHECKPOINT, payload
                    )
                    if _size(record) > ENVELOPE_LIMIT:
                        raise ValueError("Checkpoint envelope exceeds 256 KiB")
                await current()
                self.store.reject_credentials(snapshot)

            scoped = self.completions.scope(
                frozen, frozen_task, review=review, assert_ownership=assert_ownership, retain_intent=retain
            )
            record = await scoped.admit(raw, body)
            await current()
            self.store.reject_credentials(snapshot)
            self.store.reject_credentials(body.decode("utf-8"))
            return record

        async def admit(raw: Data, body: bytes) -> Data:
            with prepared_credential_rejection(self.store):
                return await checkpoint_admit(raw, body)

        async def response(intent: Data, body: bytes) -> None:
            _outside_unit()
            if type(body) is not bytes or not 1 <= len(body) <= 262144:
                raise ValueError("Original response requires bounded immutable bytes")
            # Retaining HTTP evidence and consuming it are distinct commits. A failed consume never exposes it.
            assert base.response is not None
            with prepared_credential_rejection(self.store):
                await base.response(intent, body)
                await self._consume(frozen, intent, body, current, reader)

        return BridgeCompletionScope(base.prepare, admit, base.guard, response)

    async def exact(self, run_id: str, request_id: str) -> Data:
        """Read immutable checkpoint receipts without returning a completion or issuing a restore ticket."""
        with prepared_credential_rejection(self.store):
            return await self._exact(run_id, request_id)

    async def _exact(self, run_id: str, request_id: str) -> Data:
        _outside_unit()
        run_id = identifier(run_id).hex
        _request_id(request_id)
        reader = CURRENT_PRINCIPAL.get()
        async with self.store._unit():
            run = await self.completions._reader(run_id, reader)
            scope = digest({"scope": self.journal._worker_scope(run)})
            intent = await self.completions._record(run_id, request_id)
            if any(intent["payload"][key] != run.get(key) for key in ("project_id", "repository_id")):
                raise AccessDenied("Checkpoint belongs to another project or repository scope")
            checkpoint = await self._record(run_id, request_id, CHECKPOINT)
            if checkpoint is None or checkpoint["payload"]["intent_digest"] != intent["digest"]:
                raise KeyError("Original checkpoint not found")
            consumed = await self._record(run_id, request_id, CONSUME)
            if consumed is not None and (
                consumed["task_id"] != intent["task_id"]
                or consumed["payload"]["checkpoint_record_digest"] != checkpoint["digest"]
                or consumed["payload"]["intent_digest"] != intent["digest"]
            ):
                raise ValueError("Checkpoint consume receipt has different immutable bindings")
            result = {"checkpoint": checkpoint, "consume": consumed}
        self._validate_checkpoint(checkpoint, intent)
        return await self.completions._admitted_read(run_id, result, reader, scope)

    @staticmethod
    def _validate_checkpoint(checkpoint: Data, intent: Data) -> None:
        # Native codec compatibility consults installed distribution metadata outside the database fence.
        _outside_unit()
        captured = BoundaryCheckpoint.decode(
            checkpoint["payload"]["snapshot"].encode("utf-8"), checkpoint["payload"]["checkpoint_digest"]
        )
        descriptor = intent["payload"]["descriptor"]
        if (
            checkpoint["task_id"] != intent["task_id"]
            or captured.wire_sha256 != descriptor["wire_sha256"]
            or captured.record.wire_bytes != descriptor["wire_bytes"]
        ):
            raise ValueError("Retained checkpoint wire differs from its intent")

    async def _bundle(self, run: Data, intent: Data, body: bytes) -> tuple[Data, Data, Data]:
        run_id, request_id = run["id"], intent["idempotency_key"]
        retained = await self.completions._record(run_id, request_id)
        checkpoint = await self._record(run_id, request_id, CHECKPOINT)
        response = await self.completions._record(run_id, request_id, RESPONSE)
        if retained != intent or checkpoint is None or checkpoint["payload"]["intent_digest"] != retained["digest"]:
            raise ValueError("Original checkpoint and native intent differ")
        payload = CompletionResponsePayload.model_validate(response["payload"])
        if (
            response["task_id"] != intent["task_id"]
            or checkpoint["task_id"] != intent["task_id"]
            or payload.intent_digest != retained["digest"]
            or payload.response.encode("utf-8") != body
            or payload.response_sha256 != hashlib.sha256(body).hexdigest()
            or payload.response_bytes != len(body)
        ):
            raise ValueError("Response does not match the original retained HTTP result")
        completion = ChatCompletion.model_validate_json(body, strict=True)
        if (
            completion.model != retained["payload"]["descriptor"]["model"]
            or not completion.choices
            or any(choice.finish_reason not in {"stop", "tool_calls"} for choice in completion.choices)
        ):
            raise ValueError("Only an original successful text completion can be consumed")
        return retained, checkpoint, response

    async def _consume(
        self, run: Data, intent: Data, body: bytes, current: Callable[[], Awaitable[None]], reader: Any
    ) -> None:
        await current()
        async with self.store._unit():
            bundle = await self._bundle(run, intent, body)
        self._validate_checkpoint(bundle[1], bundle[0])
        await self.store.admit_credentials(bundle)
        await current()
        async with self.store._unit():
            await self.completions._writer(run, reader)
            await self.journal._run(run["id"])
            await current()
            if await self._bundle(run, intent, body) != bundle:
                raise ValueError("Checkpoint consumption evidence changed during admission")
            await self._unconsumed(run["id"], intent["idempotency_key"])
            actual = await self.journal._effect_task(run["id"], intent["payload"]["task_key"])
            if actual.status != "running" or actual.attempt != intent["payload"]["task_attempt"]:
                raise ValueError("Checkpoint consumption needs the original running task attempt")
            _, checkpoint, response = bundle
            payload = ConsumePayload(
                version=1,
                request_id=intent["idempotency_key"],
                intent_digest=intent["digest"],
                checkpoint_record_digest=checkpoint["digest"],
                response_record_digest=response["digest"],
                response_sha256=response["payload"]["response_sha256"],
                response_bytes=response["payload"]["response_bytes"],
            ).model_dump(mode="json")
            await self._budget(run["id"], extra=_size(payload) + 4096, consume=True)
            consumed = await self.journal._append(
                run["id"], intent["task_id"], intent["idempotency_key"], CONSUME, payload
            )
            if _size(consumed) > CONSUME_RESERVE:
                raise ValueError("Checkpoint consumption exceeds its reserved envelope")
            await current()
            self.store.reject_credentials(bundle)
        await current()
        self.store.reject_credentials(bundle)
