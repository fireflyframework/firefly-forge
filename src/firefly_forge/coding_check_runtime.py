# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Source-bound configured checks through the existing native tool broker and runner."""

from __future__ import annotations

import asyncio
import copy
import json
import time
import uuid
from typing import Any

from firefly_forge.coding_check_grants import CodingCheckGrants
from firefly_forge.coding_check_models import ConfiguredCheckRequest, configured_command
from firefly_forge.journal import digest
from firefly_forge.policies import policy_commands
from firefly_forge.sandbox_models import validate_sandbox_snapshot
from firefly_forge.store import identifier, now
from firefly_forge.tool_broker import ToolReconciliationRequired, frozen_policy
from firefly_forge.tool_persistence import ForgeToolInvocationEntity
from firefly_forge.verification_models import verification_source

Data = dict[str, Any]
TOOL = "coding_configured_check"


def bounded_check_output(value: str, limit: int) -> str:
    if limit <= 0:
        return ""
    return value.encode("utf-8")[-limit:].decode("utf-8", errors="ignore")


def bounded_check_result(metadata: Data, output: str, maximum: int) -> Data:
    value = {**metadata, "output": ""}
    overhead = len(json.dumps(value).encode())
    if overhead > maximum:
        raise ValueError("Configured-check receipt metadata exceeds the frozen result bound")
    limit = min(32000, maximum - overhead)
    value["output"] = bounded_check_output(output, limit)
    while len(json.dumps(value).encode()) > maximum:
        limit //= 2
        value["output"] = bounded_check_output(output, limit)
    return value


class ConfiguredCheckInvocation:
    def __init__(self, grants: CodingCheckGrants, run_id: str, task_key: str) -> None:
        self.grants, self.run_id, self.task_key = grants, run_id, task_key
        self.recoveries = grants.recoveries
        self.engine = self.recoveries.engine
        self.store, self.journal = self.recoveries.store, self.recoveries.journal
        self.broker = self.engine.tool_broker
        if self.broker is None or self.engine.sandbox is None:
            raise ValueError("Scoped coding checks require the native tool broker and sandbox executor")
        self.lock = asyncio.Lock()

    async def guard(self, *, source: str | None = None) -> tuple[Data, Data, str]:
        await self.store.assert_ownership()
        initial = await self.store.get_run(self.run_id)
        state = initial.get("provider_recovery") or {}
        async with self.recoveries._locked(self.run_id, state["actor"]) as row:
            receipt = await self.grants.validate_invocation(row.data, self.task_key)
            task = await self.journal._task(self.run_id, "implement")
            if (
                (row.data.get("provider_recovery") or {}).get("status") != "consumed"
                or task.data.get("idempotency_key") != self.task_key
                or task.status != "running"
                or row.data.get("active_task") != "implement"
                or (row.data.get("cancel_request") or {}).get("status") == "requested"
            ):
                raise ValueError("The scoped coding invocation is no longer its active native task")
            observed = verification_source(self.engine.workspaces / self.run_id)
            if source is not None and source != observed:
                raise ValueError("The configured-check source changed before its native effect")
            run = copy.deepcopy(row.data)
        await self.store.assert_ownership()
        return run, receipt, observed

    async def run(self, request: ConfiguredCheckRequest) -> Data:
        request = ConfiguredCheckRequest.model_validate(request.model_dump())
        async with self.lock:
            run, grant, source = await self.guard()
            command = configured_command(run["commands"], grant["request"]["command_indices"], request)
            admission = dict(
                run_id=self.run_id,
                task_id="implement",
                tool=TOOL,
                correlation_id=request.request_id,
                arguments=request.model_dump(),
                configuration={"grant_id": grant["request"]["request_id"], "grant_digest": digest(grant)},
                source={"source_digest": source, "task_key": self.task_key, "cut": grant["candidate"]["cut"]},
            )
            await self.store.admit_credentials(admission)
            await self.guard(source=source)
            async with self.recoveries._locked(self.run_id, grant["actor"]) as row:
                await self.grants.validate_invocation(row.data, self.task_key)
                rows = await self.broker.records.for_run(identifier(self.run_id))
                previous = next((value for value in rows if value.correlation_id == request.request_id), None)
                if previous is not None:
                    if previous.data["request_digest"] != digest(admission):
                        raise ValueError("The original configured-check request or source changed")
                    if previous.status != "succeeded":
                        raise ToolReconciliationRequired("The original configured check is unresolved; never replay it")
                    return copy.deepcopy(previous.data["result"])
                if any(value.status in {"pending", "unknown"} for value in rows):
                    raise ToolReconciliationRequired("An existing native tool effect requires reconciliation")
                policy = frozen_policy(run.get("tool_policy_snapshot"))["settings"]
                own = [
                    value
                    for value in rows
                    if value.tool == TOOL and value.data["admission"]["configuration"] == admission["configuration"]
                ]
                if (
                    len(own) >= grant["call_limit"]
                    or len(rows) >= policy["calls_limit"]
                    or sum(value.tool == TOOL for value in rows) >= policy["per_tool_limit"]
                ):
                    raise ValueError("The existing configured-check or shared tool-call budget is exhausted")
                records = await self.journal.sandbox_executions(self.run_id)
                config = validate_sandbox_snapshot(run["sandbox_snapshot"])
                final_count = len(run["commands"]) + len(
                    policy_commands(run.get("policy_snapshot", {}), run["backend"])
                )
                if config.command_limit - len(records) <= final_count:
                    raise ValueError("Remaining shared command budget is needed for independent final verification")
                await self.store.assert_ownership()
                await self.broker.records.save(
                    ForgeToolInvocationEntity(
                        id=uuid.uuid4(),
                        run_id=identifier(self.run_id),
                        task_id="implement",
                        tool=TOOL,
                        correlation_id=request.request_id,
                        status="pending",
                        data={
                            "admission": admission,
                            "request_digest": digest(admission),
                            "created_at": now(),
                            "lease_expires_at": min(time.time() + command.timeout + 10, grant["expires_at_ms"] / 1000),
                        },
                    )
                )
                await self.journal._append(self.run_id, "implement", request.request_id, "tool_intent", admission)

            async def before_write() -> None:
                await self.guard(source=source)

            try:
                await before_write()
                before = {record["execution_id"] for record in records}
                result = await self.engine._project_command(
                    self.run_id,
                    run,
                    self.engine.workspaces / self.run_id,
                    command.argv,
                    command.timeout,
                    "implement",
                    _invocation_guard=before_write,
                )
                _, _, after = await self.guard()
                retained = await self.journal.sandbox_executions(self.run_id)
                value = dict(
                    invocation_id=request.request_id,
                    command_index=request.command_index,
                    exit_code=result.exit_code,
                    source_before=source,
                    source_after=after,
                    sandbox_execution_ids=[
                        record["execution_id"] for record in retained if record["execution_id"] not in before
                    ],
                )
                value = bounded_check_result(value, result.output, policy["max_result_bytes"])
                await self.store.admit_credentials(value)
                await self.guard()
                async with self.recoveries._locked(self.run_id, grant["actor"]) as row:
                    await self.grants.validate_invocation(row.data, self.task_key)
                    await self.store.assert_ownership()
                    await self.broker._complete(self.run_id, request.request_id, "succeeded", {"result": value})
                return value
            except BaseException as exc:
                await asyncio.shield(
                    self.broker._unknown(
                        self.run_id,
                        request.request_id,
                        "unknown",
                        {
                            "error": {
                                "message": "Configured-check outcome not admitted; inspect its original runner receipt"
                            }
                        },
                    )
                )
                if isinstance(exc, asyncio.CancelledError):
                    raise
                raise ToolReconciliationRequired(
                    "Configured-check outcome requires reconciliation; never replay it"
                ) from None
