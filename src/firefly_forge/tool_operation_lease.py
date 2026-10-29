# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Renew an existing tool lease only under its original current native authority."""

from __future__ import annotations

import copy
import math
from datetime import datetime
from typing import Any, cast
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import func, select

from firefly_forge.journal import digest
from firefly_forge.project_access import ProjectAccess
from firefly_forge.store import identifier
from firefly_forge.tool_operation_models import OwnedToolOperationV2
from firefly_forge.tool_operations import ToolWorkTimedOut

Data = dict[str, Any]
PHASES = {"prepare": "preparing", "implement": "developing", "verify": "validating", "review": "reviewing"}
CUT_FIELDS = (
    "repository_id",
    "project_id",
    "component_snapshot",
    "execution_actor",
    "agent",
    "backend",
    "frontend",
    "operation",
    "publish",
    "brief",
    "config_revision",
    "model_selection",
    "role_snapshot",
    "policy_snapshot",
    "tool_policy_snapshot",
    "sandbox_snapshot",
    "work_duration",
    "mcp_snapshot",
    "search_snapshot",
    "knowledge_snapshot",
    "learning_snapshot",
    "framework_knowledge_request",
    "framework_knowledge_pin",
    "factory_team",
    "factory_context",
)


def operation_cut(run: Data) -> str:
    return digest({name: run.get(name) for name in CUT_FIELDS})


async def current_task_intent(store: Any, journal: Any, run: Any, task_id: str) -> tuple[Any, Any]:
    if (
        task_id not in PHASES
        or run.status != PHASES[task_id]
        or run.data.get("status") != run.status
        or run.data.get("active_task") != task_id
        or run.data.get("cancel_request", {}).get("status") == "requested"
    ):
        raise ValueError("Owned tool execution requires its exact current active stage")
    task = await journal._task(run.id.hex, task_id)
    if (
        task.status != "running"
        or type(task.attempt) is not int
        or task.attempt < 1
        or type(run.data.get("attempt")) is not int
        or run.data["attempt"] != task.attempt
    ):
        raise ValueError("Owned tool execution requires its original running task attempt")
    key = uuid5(NAMESPACE_URL, f"forge:{run.id.hex}:{task_id}:{task.attempt}").hex
    if task.data.get("idempotency_key") != key:
        raise ValueError("Owned tool execution task key changed")
    intent = await journal.records.task_record(run.id, key, "intent")
    receipt = await journal.records.task_record(run.id, key, "receipt")
    if intent is None or receipt is not None:
        raise ValueError("Owned tool execution requires its unfinished original native intent")
    scalar = {
        "run_id": intent.run_id.hex,
        "sequence": intent.sequence,
        "task_id": intent.task_id,
        "idempotency_key": intent.idempotency_key,
        "kind": intent.kind,
        "previous_hash": intent.previous_hash,
        "digest": intent.digest,
    }
    if (
        digest(scalar) != digest({name: intent.data.get(name) for name in scalar})
        or digest({name: value for name, value in intent.data.items() if name != "digest"}) != intent.digest
        or digest(intent.data["payload"]) != digest(task.data.get("intent", {}))
    ):
        raise ValueError("Owned tool execution native intent custody changed")
    await ProjectAccess(store).authorize_execution(run.data)
    return task, intent


class OwnedToolLease:
    def __init__(
        self,
        store: Any,
        journal: Any,
        records: Any,
        run_id: str,
        correlation_id: str,
        record: OwnedToolOperationV2,
        ownership: object,
        request_digest: str,
        progress: Data,
    ) -> None:
        self.store, self.journal, self.records = store, journal, records
        self.run_id, self.correlation_id = run_id, correlation_id
        self.record, self.ownership, self.request_digest = record, ownership, request_digest
        self.progress = copy.deepcopy(progress)

    def matches(self, row: Any) -> None:
        if (
            row.run_id.hex != self.run_id
            or row.task_id != self.record.task_id
            or row.correlation_id != self.correlation_id
            or row.data.get("admission", {}).get("run_id") != self.run_id
            or row.data.get("admission", {}).get("task_id") != row.task_id
            or row.data.get("admission", {}).get("tool") != row.tool
            or row.data.get("admission", {}).get("correlation_id") != row.correlation_id
            or row.data.get("request_digest") != self.request_digest
            or digest(row.data.get("admission", {})) != self.request_digest
            or digest(row.data["admission"].get("operation")) != digest(self.record.model_dump(mode="json"))
            or digest(row.data.get("ownership")) != digest(self.progress)
            or digest({"expires": row.data.get("lease_expires_at")})
            != digest({"expires": self.progress["lease_expires_at"]})
        ):
            raise ValueError("Tool ownership differs from its original admission and current progress")

    async def validate_locked(self, run: Any, row: Any, database_time: float, *, status: str = "pending") -> None:
        await self.store.assert_ownership()
        if (
            self.store.execution_ownership_token is not self.ownership
            or self.store.schema != self.record.factory_fence.factory_schema
            or self.store._ownership_pid != self.record.factory_fence.pid
            or run.id.hex != self.run_id
            or run.data.get("id") != self.run_id
            or operation_cut(run.data) != self.record.run_cut
            or row.status != status
        ):
            raise ValueError("Original tool owner or frozen execution scope is unavailable")
        self.matches(row)
        expires = self.progress["lease_expires_at"]
        if type(expires) not in {int, float} or not math.isfinite(expires) or expires <= database_time:
            raise ValueError("Tool ownership expired; an old pending operation cannot be adopted")
        if self.record.work_deadline_ms is not None and database_time * 1000 >= self.record.work_deadline_ms:
            raise ToolWorkTimedOut("The original chosen tool work deadline expired")
        task, intent = await current_task_intent(self.store, self.journal, run, self.record.task_id)
        if task.attempt != self.record.attempt or intent.digest != self.record.intent_digest:
            raise ValueError("Tool ownership selects another native task generation")
        await self.store.assert_ownership()
        if self.store.execution_ownership_token is not self.ownership:
            raise ValueError("Tool owner changed during authorization")

    async def renew(self) -> None:
        next_progress: Data
        async with self.store._unit() as session:
            run = await self.journal._run(self.run_id)
            row = await self.records.invocation(identifier(self.run_id), self.correlation_id)
            if row is None:
                raise ValueError("Original pending tool invocation is unavailable")
            database_time = cast(datetime, await session.scalar(select(func.clock_timestamp()))).timestamp()
            await self.validate_locked(run, row, database_time)
            next_progress = {
                "generation": self.record.generation,
                "sequence": self.progress["sequence"] + 1,
                "lease_expires_at": database_time + self.record.owner_grace_seconds,
            }
            row.data = {**row.data, "ownership": next_progress, "lease_expires_at": next_progress["lease_expires_at"]}
            await self.records.save(row)
        self.progress = copy.deepcopy(next_progress)
