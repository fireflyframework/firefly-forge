# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Run-scoped tool intents, bounded outcomes and explicit unknown-effect handling."""

from __future__ import annotations

import asyncio
import copy
import json
import re
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from pydantic import BaseModel, ConfigDict, Field
from pyfly.container import service
from sqlalchemy import func, select

from firefly_forge.agent_roles import roles_for_stage, stage_allowed_tools
from firefly_forge.configuration import ConfigurationStore
from firefly_forge.journal import Journal, digest
from firefly_forge.project_access import CURRENT_PRINCIPAL, ProjectAccess
from firefly_forge.search_tools import SearchProviderError
from firefly_forge.secrets import contains_known_credential
from firefly_forge.store import Store, identifier, now
from firefly_forge.tool_persistence import ForgeToolInvocationEntity, ForgeToolInvocationRepository
from firefly_forge.verification_models import AutomaticVerificationPolicy

Data = dict[str, Any]


class ToolReconciliationRequired(RuntimeError):
    pass


class ToolPolicyInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    calls_limit: int = Field(default=30, ge=1, le=100, strict=True)
    per_tool_limit: int = Field(default=10, ge=1, le=30, strict=True)
    timeout_seconds: int = Field(default=30, ge=1, le=60, strict=True)
    max_result_bytes: int = Field(default=256_000, ge=1024, le=750_000, strict=True)


class FactoryToolPolicyInput(ToolPolicyInput):
    automatic_verification: AutomaticVerificationPolicy = Field(default_factory=AutomaticVerificationPolicy)


def tool_policy_snapshot(directory: Path | ConfigurationStore) -> Data:
    private = directory if isinstance(directory, ConfigurationStore) else ConfigurationStore(directory)
    with private.lock:
        record = private._read(private.directory / "tool-policy.json")
    settings = FactoryToolPolicyInput.model_validate(record.get("settings", {})).model_dump()
    payload = {"version": 1, "revision": record.get("revision"), "settings": settings}
    return {**payload, "digest": digest(payload)}


def frozen_policy(snapshot: Data | None) -> Data:
    if snapshot is None:
        payload = {"version": 1, "revision": None, "settings": ToolPolicyInput().model_dump()}
        return {**payload, "digest": digest(payload)}
    payload = {key: value for key, value in snapshot.items() if key != "digest"}
    if (
        set(payload) != {"version", "revision", "settings"}
        or payload["version"] != 1
        or digest(payload) != snapshot.get("digest")
    ):
        raise ValueError("Tool policy snapshot was changed")
    FactoryToolPolicyInput.model_validate(payload["settings"])
    return copy.deepcopy(snapshot)


@service
class ToolInvocationBroker:
    def __init__(
        self, store: Store, journal: Journal, directory: Path, records: ForgeToolInvocationRepository | None = None
    ) -> None:
        self.store, self.journal = store, journal
        self.private = ConfigurationStore(directory)
        self.records = records or ForgeToolInvocationRepository()
        self.credential_registry: Callable[[], dict[str, str]] = dict
        self.external_mcp: Any = None

    def snapshot(self) -> Data:
        return tool_policy_snapshot(self.private)

    def configure(self, request: ToolPolicyInput) -> Data:
        with self.private.lock:
            path = self.private.directory / "tool-policy.json"
            current = FactoryToolPolicyInput.model_validate(self.private._read(path).get("settings", {}))
            settings = {**current.model_dump(), **request.model_dump()}
            if "automatic_verification" not in request.model_fields_set:
                settings["automatic_verification"] = current.automatic_verification.model_dump()
            self.private._write(
                path,
                {"revision": uuid.uuid4().hex, "settings": settings},
            )
        return self.snapshot()

    async def invocations(self, run_id: str) -> list[Data]:
        async with self.store._unit():
            await self.store.get_run(run_id)
            return [
                {**copy.deepcopy(row.data), "status": row.status, "correlation_id": row.correlation_id}
                for row in await self.records.for_run(identifier(run_id))
            ]

    async def unresolved(self, run_id: str) -> bool:
        return any(item["status"] in {"pending", "unknown"} for item in await self.invocations(run_id))

    async def invoke(
        self,
        run_id: str,
        task_id: str,
        tool: str,
        correlation_id: str,
        arguments: Data,
        callback: Callable[[], Awaitable[Data]],
        *,
        source: Data,
        configuration: Data,
        observation: Callable[[], Data] | None = None,
        validate_receipt: Callable[[Data], Awaitable[Callable[[], None] | None]] | None = None,
        validate_result: Callable[[Data], Awaitable[Callable[[], None] | None]] | None = None,
        validate_admission: Callable[[], Awaitable[Callable[[], None] | None]] | None = None,
    ) -> Data:
        if not re.fullmatch(r"[a-f0-9]{32}", correlation_id):
            raise ValueError("Tool correlation ID requires 32 lowercase hexadecimal characters")
        external = re.fullmatch(r"external_mcp:([a-z][a-z0-9-]{0,30}):([a-z][a-z0-9_]{0,80})", tool)
        knowledge_lookup = tool == "knowledge_lookup"
        from firefly_forge.source_edit_models import READ_TOOLS, SourceReadRequest

        source_read = tool in READ_TOOLS
        if tool != "internet_search" and not knowledge_lookup and external is None and not source_read:
            raise ValueError("Select a registered broker tool")
        if source_read and (validate_admission is None or validate_result is None or validate_receipt is None):
            raise ValueError("Source readers require sealed admission and result authority")
        if any(
            not isinstance(source.get(key), str) or not re.fullmatch(r"[a-f0-9]{40}|[a-f0-9]{64}", source[key])
            for key in ("base_revision", "head", "source_tree")
        ):
            raise ValueError("Tool invocation requires an actual source association")
        if len(json.dumps(arguments).encode()) > 64_000:
            raise ValueError("Tool arguments exceed 64 KB")
        await self.store.admit_credentials([arguments, source, configuration, correlation_id])
        if contains_known_credential([arguments, source, configuration, correlation_id], self.credential_registry()):
            raise ValueError("Tool invocations cannot contain configured credentials")
        from firefly_forge.runner_configuration import SearchQuery

        refresh = {key: arguments[key] for key in ("refresh", "refresh_request_id") if key in arguments}
        if set(refresh) and (
            refresh.get("refresh") is not True
            or not isinstance(refresh.get("refresh_request_id"), str)
            or not re.fullmatch(r"[a-f0-9]{32}", refresh["refresh_request_id"])
        ):
            raise ValueError("Tool refresh requires a stable explicit request identity")
        clean_arguments = {key: value for key, value in arguments.items() if key not in refresh}
        if source_read:
            SourceReadRequest.model_validate({"tool": tool, "request_id": correlation_id, **clean_arguments})
        elif knowledge_lookup:
            from firefly_forge.knowledge_first import KnowledgeQuery

            arguments = {**KnowledgeQuery.model_validate(clean_arguments).model_dump(), **refresh}
        elif external is None:
            arguments = {**SearchQuery.model_validate(clean_arguments).model_dump(), **refresh}
            if not arguments["query"].strip() or len(arguments["query"].split()) > 75:
                raise ValueError("Search query must contain nonempty text and at most 75 words")
        async with self.store._unit() as session:
            run = await self.journal._run(run_id)
            await ProjectAccess(self.store).authorize_execution(run.data)
            policy = frozen_policy(run.data.get("tool_policy_snapshot"))
            expected_configuration = (
                {
                    "knowledge_snapshot": run.data.get("knowledge_snapshot"),
                    "learning_snapshot": run.data.get("learning_snapshot"),
                }
                if knowledge_lookup
                else run.data.get(
                    "source_edit_continuation" if source_read else "mcp_snapshot" if external else "search_snapshot"
                )
            )
            if configuration != expected_configuration:
                raise ValueError("Tool configuration differs from the frozen run")
            if knowledge_lookup:
                from firefly_forge.learning_configuration import frozen_learning

                knowledge = run.data.get("knowledge_snapshot") or {}
                if (
                    not frozen_learning(run.data.get("learning_snapshot"))["settings"]["enabled"]
                    or not knowledge.get("enabled")
                    or not knowledge.get("endpoint")
                    or not (knowledge.get("api_key_configured") or knowledge.get("agent_token_configured"))
                ):
                    raise ValueError("Knowledge lookup requires captured enabled knowledge access and learning policy")
            roles = roles_for_stage(run.data.get("role_snapshot") or {}, task_id)
            provider = "agentic" if run.data["agent"] == "api" else run.data["agent"]
            granted = (
                {name for role in roles for name in role["allowed_tools"]}
                if source_read
                else stage_allowed_tools(run.data["role_snapshot"], task_id, provider) or set()
            )
            if ("external_mcp" if external else tool) not in granted:
                raise ValueError("The frozen execution role does not grant this tool")
            admission_guard = await validate_admission() if validate_admission else None
            if external:
                if (
                    self.external_mcp is None
                    or set(arguments) != {"connector", "tool", "arguments"}
                    or (arguments["connector"], arguments["tool"]) != external.groups()
                ):
                    raise ValueError("Select an exact registered external MCP invocation")
                if (
                    configuration.get("project_id") != run.data.get("project_id")
                    or configuration.get("repository_id") != run.repository_id.hex
                ):
                    raise ValueError("MCP configuration differs from the run project and repository")
                self.external_mcp.authorize(
                    configuration,
                    arguments["connector"],
                    arguments["tool"],
                    arguments["arguments"],
                    roles=[role["id"] for role in roles],
                    stage=task_id,
                )
            request = {
                "run_id": run.id.hex,
                "task_id": task_id,
                "tool": tool,
                "correlation_id": correlation_id,
                "arguments": copy.deepcopy(arguments),
                "configuration": copy.deepcopy(configuration),
                "source": copy.deepcopy(source),
                "repository_id": run.repository_id.hex,
                "project_id": run.data.get("project_id"),
                "component_id": run.data.get("component_id"),
                "roles": [role["id"] for role in roles],
                "policy": policy,
            }
            if run.data.get("learning_snapshot") is not None and tool in {"internet_search", "knowledge_lookup"}:
                request["learning_snapshot"] = copy.deepcopy(run.data["learning_snapshot"])
                request["knowledge_snapshot"] = copy.deepcopy(run.data.get("knowledge_snapshot"))
            request_digest = digest(request)
            rows = await self.records.for_run(run.id)
            previous = next((row for row in rows if row.correlation_id == correlation_id), None)
            if previous is not None:
                if previous.data["request_digest"] != request_digest:
                    raise ValueError("Tool correlation ID already belongs to another invocation")
                if previous.status == "succeeded":
                    completion_guard = None
                    if validate_receipt is not None:
                        completion_guard = await validate_receipt(copy.deepcopy(previous.data["result"]))
                    await ProjectAccess(self.store).authorize_execution(run.data)
                    if completion_guard is not None:
                        completion_guard()
                    await self._authorize_result(previous, "succeeded")
                    return copy.deepcopy(previous.data["result"])
                if previous.status == "rejected":
                    error = previous.data["error"]
                    raise SearchProviderError(
                        error["provider"], error["kind"], error["message"], status=error.get("status")
                    )
                if previous.status == "abandoned":
                    raise ValueError("This invocation was explicitly abandoned; it cannot be replayed")
                raise ToolReconciliationRequired(
                    "Tool invocation has an unresolved outcome; reconcile it before continuing"
                )
            if run.data.get("learning_snapshot") is not None and tool in {"internet_search", "knowledge_lookup"}:
                from firefly_forge.learning_configuration import frozen_learning

                learning = frozen_learning(run.data["learning_snapshot"])["settings"]
                if (
                    observation is not None
                    and sum(row.tool in {"internet_search", "knowledge_lookup"} for row in rows)
                    >= learning["max_lookup_calls"]
                ):
                    raise ValueError("The frozen learning lookup budget is exhausted")
            limits = policy["settings"]
            if source_read:
                limits = {
                    **limits,
                    "calls_limit": min(30, limits["calls_limit"]),
                    "per_tool_limit": min(10, limits["per_tool_limit"]),
                }
            if len(rows) >= limits["calls_limit"] or sum(row.tool == tool for row in rows) >= limits["per_tool_limit"]:
                raise ValueError("The frozen run tool-call budget is exhausted")
            if any(row.status in {"pending", "unknown"} for row in rows):
                raise ToolReconciliationRequired(
                    "Another tool invocation is unresolved; reconcile it before continuing"
                )
            task = await self.journal._task(run_id, task_id)
            if task.status != "running" or run.data.get("active_task") != task_id:
                raise ValueError("A tool invocation requires its active execution stage")
            database_time = cast(datetime, await session.scalar(select(func.clock_timestamp()))).timestamp()
            if admission_guard is not None:
                admission_guard()
            await self.records.save(
                ForgeToolInvocationEntity(
                    id=uuid.uuid4(),
                    run_id=run.id,
                    task_id=task_id,
                    tool=tool,
                    correlation_id=correlation_id,
                    status="pending",
                    data={
                        "admission": request,
                        "request_digest": request_digest,
                        "created_at": now(),
                        "lease_expires_at": database_time + limits["timeout_seconds"] + 10,
                    },
                )
            )
            await self.journal._append(run_id, task_id, correlation_id, "tool_intent", request)
            if admission_guard is not None:
                admission_guard()

        def evidence(payload: Data) -> Data:
            if observation is None:
                return payload
            from firefly_forge.learning_models import ToolObservation

            observed = ToolObservation.model_validate(observation()).model_dump()
            if contains_known_credential(observed, self.credential_registry()):
                raise ValueError("Tool observations cannot reflect configured credentials")
            return {**payload, "observation": observed}

        try:
            async with asyncio.timeout(limits["timeout_seconds"]):
                result = await callback()
            if not isinstance(result, dict):
                raise ValueError("Tool result exceeds its frozen response bound")
            result = {**result, "invocation_id": correlation_id}
            if len(json.dumps(result).encode()) > limits["max_result_bytes"]:
                raise ValueError("Tool result exceeds its frozen response bound")
            await self.store.admit_credentials(result)
            if contains_known_credential(result, self.credential_registry()):
                raise ValueError("Tool result reflected configured credentials")
            await self._complete(run_id, correlation_id, "succeeded", evidence({"result": result}), validate_result)
            return result
        except SearchProviderError as exc:
            if exc.kind in {"rate_limit", "quota", "authentication"}:
                await self._complete(
                    run_id, correlation_id, "rejected", evidence({"error": {**exc.metadata(), "message": str(exc)}})
                )
                raise
            await self._unknown(
                run_id,
                correlation_id,
                "unknown",
                evidence({"error": {"message": "Provider outcome was not admitted; reconcile its usage"}}),
            )
            raise ToolReconciliationRequired("Tool outcome is unknown; reconcile it before requesting again") from exc
        except asyncio.CancelledError:
            await asyncio.shield(
                self._unknown(
                    run_id,
                    correlation_id,
                    "unknown",
                    evidence({"error": {"message": "Tool execution was interrupted"}}),
                )
            )
            raise
        except Exception as exc:
            await self._unknown(
                run_id, correlation_id, "unknown", evidence({"error": {"message": "Tool outcome was not admitted"}})
            )
            raise ToolReconciliationRequired("Tool outcome is unknown; reconcile it before requesting again") from exc

    async def _unknown(self, run_id: str, correlation_id: str, status: str, evidence: Data) -> None:
        try:
            await self._complete(run_id, correlation_id, status, evidence)
        except Exception:
            # The committed intent remains unresolved if a receipt cannot be committed.
            return

    async def _complete(
        self,
        run_id: str,
        correlation_id: str,
        status: str,
        evidence: Data,
        validate_result: Callable[[Data], Awaitable[Callable[[], None] | None]] | None = None,
    ) -> None:
        async with self.store._unit():
            run = await self.journal._run(run_id)
            await ProjectAccess(self.store).authorize_execution(run.data)
            row = next(item for item in await self.records.for_run(run.id) if item.correlation_id == correlation_id)
            if row.status != "pending":
                raise ValueError("Tool invocation already has a recorded outcome")
            completion_guard = await validate_result(copy.deepcopy(evidence["result"])) if validate_result else None
            if completion_guard is not None:
                await ProjectAccess(self.store).authorize_execution(run.data)
                completion_guard()
            await self._authorize_result(row, status)
            if completion_guard is not None:
                completion_guard()
            row.status = status
            row.data = {**row.data, **copy.deepcopy(evidence), "completed_at": now()}
            await self.records.save(row)
            await self.journal._append(
                run_id,
                row.task_id,
                correlation_id,
                "tool_receipt" if status != "unknown" else "tool_unknown",
                {"correlation_id": correlation_id, "tool": row.tool, "status": status, **evidence},
            )
            await self._authorize_result(row, status)
            if completion_guard is not None:
                completion_guard()

    async def _authorize_result(self, row: ForgeToolInvocationEntity, status: str) -> None:
        if status != "succeeded" or not row.tool.startswith("external_mcp:"):
            return
        if self.external_mcp is None:
            raise ValueError("External MCP result requires its registered authority")
        admission = row.data["admission"]
        arguments = admission["arguments"]
        await self.external_mcp.authorize_result(
            admission["configuration"],
            arguments["connector"],
            arguments["tool"],
            arguments["arguments"],
            roles=admission["roles"],
            stage=row.task_id,
            run_id=row.run_id.hex,
        )

    async def reconcile(self, run_id: str, correlation_id: str, reconciliation_id: str) -> Data:
        if not re.fullmatch(r"[a-f0-9]{32}", reconciliation_id):
            raise ValueError("Reconciliation requires a stable request ID")
        async with self.store._unit() as session:
            run = await self.journal._run(run_id)
            rows = await self.records.for_run(run.id)
            row = next((item for item in rows if item.correlation_id == correlation_id), None)
            if row is None:
                raise KeyError("Tool invocation not found in this run")
            if row.status == "abandoned" and row.data.get("reconciliation_id") == reconciliation_id:
                return copy.deepcopy(row.data)
            if row.status not in {"pending", "unknown"}:
                raise ValueError("Only an unresolved tool invocation can be abandoned")
            if any(
                item.correlation_id != correlation_id and item.data.get("reconciliation_id") == reconciliation_id
                for item in rows
            ):
                raise ValueError("Reconciliation request ID already belongs to another invocation")
            database_time = cast(datetime, await session.scalar(select(func.clock_timestamp()))).timestamp()
            if row.status == "pending" and row.data["lease_expires_at"] > database_time:
                raise ValueError("This invocation may still be running; wait for its bounded lease to expire")
            row.status = "abandoned"
            actor = CURRENT_PRINCIPAL.get()
            decision_actor = ProjectAccess.actor_snapshot(actor) if actor else None
            row.data = {
                **row.data,
                "reconciliation_id": reconciliation_id,
                "reconciled_at": now(),
                "decision_actor": decision_actor,
            }
            await self.records.save(row)
            await self.journal._append(
                run_id,
                row.task_id,
                reconciliation_id,
                "tool_reconciled",
                {"correlation_id": correlation_id, "decision": "abandoned_without_replay", "actor": decision_actor},
            )
            return copy.deepcopy(row.data)
