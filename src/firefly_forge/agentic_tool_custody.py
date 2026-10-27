# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Native append-only custody at the public Agentic tool capability boundary."""

from __future__ import annotations

import asyncio
import copy
import json
import uuid
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from pathlib import Path
from typing import Any, cast

from fireflyframework_agentic.model_utils import serialize_model_messages
from pydantic import TypeAdapter
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.messages import ToolCallPart
from pydantic_ai.models import ModelRequestContext
from pydantic_ai.tools import RunContext, ToolDefinition

from firefly_forge.agentic_checkpoint_invocation import current_checkpoint_invocation, invocation_cut, workspace_cut
from firefly_forge.agentic_tool_results import load as load_result
from firefly_forge.agentic_tool_results import retain as retain_result
from firefly_forge.bridge_completions import _outside_unit
from firefly_forge.journal import Journal, digest
from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied, ProjectAccess

Data = dict[str, Any]
EFFECTS = {
    "list_files": "source_read",
    "read_file": "source_read",
    "search_text": "source_read",
    "git_diff": "source_read",
    "write_file": "source_write",
    "edit_lines": "source_write",
    "execute_command": "command",
    "knowledge_lookup": "knowledge_read",
    "read_run_evidence": "evidence_read",
}
INTENT, RESULT, UNKNOWN = "agentic_tool_intent", "agentic_tool_result", "agentic_tool_unknown"
KINDS = frozenset({INTENT, RESULT, UNKNOWN})
_current: ContextVar[AgenticToolCustody | None] = ContextVar("native_agentic_tool_custody", default=None)
_parent: ContextVar[str | None] = ContextVar("native_agentic_tool_parent", default=None)


@contextmanager
def agentic_tool_custody_scope(value: AgenticToolCustody | None) -> Iterator[None]:
    token = _current.set(value)
    try:
        yield
    finally:
        _current.reset(token)


def current_agentic_tool_custody() -> AgenticToolCustody | None:
    return _current.get()


def nested_effect_parent(kind: str) -> str | None:
    """Link only effects executed in this call's context, never a time-based match."""
    parent = _parent.get()
    if parent is not None and kind in {
        "tool_intent",
        "tool_receipt",
        "tool_unknown",
        "sandbox_intent",
        "sandbox_receipt",
        "sandbox_reconcile",
    }:
        return parent
    return None


async def link_retained_broker_effects(journal: Any, run_id: str, correlation_id: str) -> None:
    """Bind an admitted cached child receipt to the current exact public call."""
    parent = _parent.get()
    if parent is None:
        return
    entries = await journal.entries(run_id)
    if not Journal.verify_chain(entries):
        raise ValueError("Cached broker journal custody changed")
    children = [
        row
        for row in entries
        if row["idempotency_key"] == correlation_id and row["kind"] in {"tool_intent", "tool_receipt"}
    ]
    if (
        len(children) != 2
        or {row["kind"] for row in children} != {"tool_intent", "tool_receipt"}
        or next(row for row in children if row["kind"] == "tool_receipt")["payload"].get("status") != "succeeded"
    ):
        raise ValueError("Cached broker receipt custody is unavailable")
    for child in children:
        effect = {key: child[key] for key in ("kind", "idempotency_key", "digest")}
        key = digest({"parent": parent, "effect": effect})
        if not any(row["kind"] == "agentic_tool_effect_link" and row["idempotency_key"] == key for row in entries):
            await journal._append(
                run_id,
                child["task_id"],
                key,
                "agentic_tool_effect_link",
                {"version": 1, "custody_id": parent, "effect": effect},
            )


def canonical(value: Any) -> Any:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return json.loads(raw)


class AgenticToolCustody:
    """One native implementation invocation; no broker budgets or effect retries."""

    def __init__(
        self, store: Any, journal: Any, run: Data, task: Data, workspace: Path, guard: Callable[[], Awaitable[None]]
    ) -> None:
        self.store, self.journal, self.workspace, self.guard = store, journal, workspace, guard
        self.run, self.task = copy.deepcopy(run), copy.deepcopy(task)
        self.reader = CURRENT_PRINCIPAL.get()
        self.invocation_id = uuid.uuid4().hex
        self.execution_lock = asyncio.Lock()

    async def current(self) -> None:
        await self.guard()
        await self.store.assert_ownership()
        if CURRENT_PRINCIPAL.get() != self.reader:
            raise AccessDenied("Agentic tool execution actor changed")
        async with self.store._unit():
            await self.journal._run(self.run["id"])
            current = await self.store.get_run(self.run["id"])
            if invocation_cut(current) != invocation_cut(self.run):
                raise AccessDenied("Agentic tool configuration or execution scope changed")
            await ProjectAccess(self.store).authorize_execution(current)
            if current.get("active_task") != "implement" or current.get("attempt", 1) != self.run.get("attempt", 1):
                raise ValueError("Agentic tool task attempt changed")
            task = await self.journal._task(self.run["id"], "implement")
            if (
                task.status != "running"
                or task.data.get("idempotency_key") != self.task["idempotency_key"]
                or task.attempt != self.task.get("attempt", self.run.get("attempt", 1))
            ):
                raise ValueError("Agentic tool task authority changed")
            await self.store.assert_ownership()

    async def entries(self) -> list[Data]:
        entries = list(await self.journal.entries(self.run["id"]))
        if entries and not Journal.verify_chain(entries):
            raise ValueError("Agentic tool journal chain changed")
        async with self.store._unit():
            rows = await self.journal.records.find_for_run(uuid.UUID(self.run["id"]))
            if len(rows) < len(entries):
                raise ValueError("Agentic tool journal custody changed")
            for native, entry in zip(rows[: len(entries)], entries, strict=True):
                scalar = {
                    "run_id": native.run_id.hex,
                    "task_id": native.task_id,
                    "kind": native.kind,
                    "idempotency_key": native.idempotency_key,
                    "sequence": native.sequence,
                    "digest": native.digest,
                    "previous_hash": native.previous_hash,
                }
                if any(entry.get(k) != v for k, v in scalar.items()):
                    raise ValueError("Agentic tool journal scalar custody changed")
        return entries

    async def assert_effects_settled(self) -> None:
        await self.current()
        entries = await self.entries()
        intents = [
            row
            for row in entries
            if row["kind"] == INTENT
            and row["task_id"] == "implement"
            and row["payload"].get("task_attempt") == self.run.get("attempt", 1)
            and row["payload"].get("effect") in {"source_write", "command"}
        ]
        for intent in intents:
            results = [
                row
                for row in entries
                if row["kind"] == RESULT
                and row["idempotency_key"] == intent["idempotency_key"]
                and row["payload"].get("intent_digest") == intent["digest"]
                and row["payload"].get("status") == "retained"
            ]
            if len(results) != 1:
                raise ValueError("An unresolved native Agentic effect requires inspection before further work")

    async def append(self, key: str, kind: str, payload: Data) -> Data:
        await self.current()
        async with self.store._unit():
            await self.journal._run(self.run["id"])
            await self.current()
            self.store.reject_credentials(payload)
            existing = [r for r in await self.entries() if r["idempotency_key"] == key and r["kind"] == kind]
            if existing:
                raise ValueError("Agentic tool call already has a retained outcome")
            row = await self.journal._append(self.run["id"], "implement", key, kind, payload)
            await self.current()
            return cast(Data, row)

    def retained(self, value: Any, field: str) -> Data:
        if len(json.dumps(value).encode()) <= 128_000:
            return {field: value}
        descriptor = retain_result(self.store.path.parent / "agentic-tool-results", value)
        return {field + "_artifact": descriptor}

    async def invoke(
        self, ctx: RunContext[Any], call: ToolCallPart, args: Any, handler: Callable[[], Awaitable[Any]]
    ) -> Any:
        _outside_unit()
        await self.assert_effects_settled()
        if not call.tool_call_id or len(call.tool_call_id) > 512 or call.tool_name not in EFFECTS:
            raise ValueError("Agentic tool custody needs an exact registered public call identity")
        await self.current()
        if "role_snapshot" in self.run:
            from firefly_forge.agent_roles import stage_allowed_tools

            provider = "agentic" if self.run.get("agent") == "api" else self.run.get("agent", "agentic")
            if call.tool_name not in (stage_allowed_tools(self.run["role_snapshot"], "implement", provider) or set()):
                raise ValueError("Agentic tool call is outside its frozen tool grant")
        source = await workspace_cut(self.store, self.workspace, assert_current=self.current)
        invocation = current_checkpoint_invocation()
        binding = invocation.binding if invocation is not None else None
        raw_arguments = canonical(call.args)
        public_arguments = canonical(TypeAdapter(Any).dump_python(args, mode="json"))
        common = {
            "version": 1,
            "custody_id": digest(
                {
                    "run_id": self.run["id"],
                    "task_key": self.task["idempotency_key"],
                    "task_attempt": self.run.get("attempt", 1),
                    "tool_call_id": call.tool_call_id,
                }
            ),
            "tool_call_id": call.tool_call_id,
            "tool": call.tool_name,
            "effect": EFFECTS[call.tool_name],
            "invocation_id": binding.invocation_id if binding is not None else self.invocation_id,
            "task_key": self.task["idempotency_key"],
            "task_attempt": self.run.get("attempt", 1),
            "request_step": ctx.run_step,
            "arguments_sha256": digest({"arguments": public_arguments}),
            "raw_arguments_sha256": digest({"raw_arguments": raw_arguments}),
            "binding_sha256": invocation_cut(self.run),
            "request_context_sha256": digest({"history": serialize_model_messages(ctx.messages)}),
            "checkpoint_digest": invocation.predecessor if invocation is not None else None,
        }
        await self.store.admit_credentials([common, raw_arguments, public_arguments, source])
        await self.current()
        key = common["custody_id"]
        entries = await self.entries()
        prior = [r for r in entries if r["kind"] in KINDS and r["idempotency_key"] == key]
        if prior:
            if any(sum(r["kind"] == kind for r in prior) > 1 for kind in KINDS):
                raise ValueError("Agentic tool journal has duplicate custody records")
            intent = next((r for r in prior if r["kind"] == INTENT), None)
            if intent is None or any(intent["payload"].get(k) != v for k, v in common.items()):
                raise ValueError("Agentic tool identity belongs to another call or changed scope")
            if "arguments_artifact" in intent["payload"]:
                retained_arguments = load_result(
                    self.store.path.parent / "agentic-tool-results", intent["payload"]["arguments_artifact"]
                )
            else:
                retained_arguments = intent["payload"].get("arguments")
            if "raw_arguments_artifact" in intent["payload"]:
                retained_raw = load_result(
                    self.store.path.parent / "agentic-tool-results", intent["payload"]["raw_arguments_artifact"]
                )
            else:
                retained_raw = intent["payload"].get("raw_arguments")
            if retained_raw != raw_arguments or retained_arguments != public_arguments:
                raise ValueError("Agentic tool retained arguments changed")
            result = next((r for r in prior if r["kind"] == RESULT), None)
            if result is None:
                raise ValueError("Agentic tool call has an unresolved result; callback cannot run again")
            payload = result["payload"]
            retained = (
                load_result(self.store.path.parent / "agentic-tool-results", payload["result_artifact"])
                if "result_artifact" in payload
                else payload.get("result")
            )
            if (
                any(payload.get(k) != v for k, v in common.items())
                or payload.get("status") != "retained"
                or payload.get("intent_digest") != intent["digest"]
                or payload.get("source_after") != source
                or payload.get("result_sha256") != digest({"result": retained})
            ):
                raise ValueError("Agentic tool retained result or source changed")
            await self.store.admit_credentials(retained)
            await self.current()
            fresh = await workspace_cut(self.store, self.workspace, assert_current=self.current)
            if fresh != source:
                raise ValueError("Agentic tool retained source changed during admission")
            return copy.deepcopy(retained)

        self.store.reject_credentials([raw_arguments, public_arguments])
        arguments_content = {
            **self.retained(public_arguments, "arguments"),
            **self.retained(raw_arguments, "raw_arguments"),
        }
        await self.current()
        intent = await self.append(
            key,
            INTENT,
            {
                **common,
                "status": "pending",
                **arguments_content,
                "source_before": source,
                "authority": {
                    "actor": copy.deepcopy(self.run.get("execution_actor")),
                    "configuration": self.run.get("config_revision"),
                    "source": self.run.get("source_tree"),
                },
            },
        )
        token = _parent.set(key)
        try:
            await self.current()
            before = await workspace_cut(self.store, self.workspace, assert_current=self.current)
            if before != source:
                raise ValueError("Agentic tool source changed before callback")
            result = canonical(await handler())
            await self.store.admit_credentials(result)
            await self.current()
            after = await workspace_cut(self.store, self.workspace, assert_current=self.current)
            if EFFECTS[call.tool_name] in {"source_read", "knowledge_read", "evidence_read"} and after != source:
                raise ValueError("Agentic tool read source changed before result custody")
            nested = [
                r["payload"]["effect"]
                for r in await self.entries()
                if r["kind"] == "agentic_tool_effect_link" and r["payload"].get("custody_id") == key
            ]
            self.store.reject_credentials(result)
            result_content = self.retained(result, "result")
            await self.current()
            await self.append(
                key,
                RESULT,
                {
                    **common,
                    "status": "retained",
                    "intent_digest": intent["digest"],
                    "source_before": source,
                    "source_after": after,
                    **result_content,
                    "result_sha256": digest({"result": result}),
                    "parent_effects": nested,
                },
            )
            return result
        except BaseException:
            # Loss of current authority leaves the committed intent unresolved.
            with suppress(BaseException):
                await asyncio.shield(
                    self.append(
                        key,
                        UNKNOWN,
                        {
                            **common,
                            "status": "unknown",
                            "intent_digest": intent["digest"],
                            "reason": "callback_result_not_durably_admitted",
                        },
                    )
                )
            raise
        finally:
            _parent.reset(token)


class AgenticToolCapability(AbstractCapability[Any]):
    def __init__(self, custody: AgenticToolCustody, tools: frozenset[str]) -> None:
        self.custody, self.tools = custody, tools

    async def before_model_request(
        self, ctx: RunContext[Any], request_context: ModelRequestContext
    ) -> ModelRequestContext:
        await self.custody.assert_effects_settled()
        return request_context

    async def wrap_tool_execute(
        self, ctx: RunContext[Any], *, call: ToolCallPart, tool_def: ToolDefinition, args: Any, handler: Any
    ) -> Any:
        async with self.custody.execution_lock:
            await self.custody.assert_effects_settled()
            if call.tool_name not in self.tools:
                return await handler(args)
            if call.tool_name != tool_def.name:
                raise ValueError("Agentic public tool name changed")
            return await self.custody.invoke(ctx, call, args, lambda: handler(args))
