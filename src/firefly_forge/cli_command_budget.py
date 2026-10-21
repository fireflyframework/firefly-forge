# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Truthful admission for retained CLI observations, not preventive tool enforcement."""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Any

from firefly_forge.activity_models import MAX_EVENTS, MAX_RUN_BYTES, validate_descriptor
from firefly_forge.policies import policy_commands
from firefly_forge.sandbox_models import validate_sandbox_snapshot
from firefly_forge.store import identifier

if TYPE_CHECKING:
    from firefly_forge.journal import Journal
    from firefly_forge.store import Store

Data = dict[str, Any]


def _identity(value: Any) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 128:
        raise ValueError("CLI command history is ambiguous")
    return value


def command_observation(events: list[Data]) -> dict[str, int]:
    """Count each paired shell item once within its original completed CLI thread."""
    if len(events) > MAX_EVENTS:
        raise ValueError("CLI command history exceeds the archive limit")
    threads: set[str] = set()
    commands: dict[tuple[str, str], dict[str, Data]] = {}
    active: str | None = None
    for event in events:
        if not isinstance(event, dict):
            raise ValueError("CLI command history is unsupported")
        kind = event.get("type")
        if kind == "thread.started":
            thread = _identity(event.get("thread_id"))
            if active is not None or thread in threads:
                raise ValueError("CLI command history is ambiguous")
            active = thread
            threads.add(thread)
        elif kind in {"turn.completed", "turn.failed"}:
            if active is None or any(
                thread == active and set(phases) != {"item.started", "item.completed"}
                for (thread, _item), phases in commands.items()
            ):
                raise ValueError("CLI command history is incomplete")
            active = None
        elif kind == "turn.started":
            if active is None:
                raise ValueError("CLI command history is incomplete")
        elif kind in {"item.started", "item.completed", "item.updated"}:
            item = event.get("item")
            if active is None or not isinstance(item, dict):
                raise ValueError("CLI command history is incomplete")
            if item.get("type") != "command_execution":
                if item.get("type") not in {
                    "agent_message",
                    "reasoning",
                    "file_change",
                    "todo_list",
                    "web_search",
                    "mcp_tool_call",
                    "error",
                }:
                    raise ValueError("CLI command history is unsupported")
                continue
            key = (active, _identity(item.get("id")))
            phases = commands.setdefault(key, {})
            if kind == "item.updated":
                if "item.started" not in phases or item.get("command") != phases["item.started"].get("command"):
                    raise ValueError("CLI command history is ambiguous")
                continue
            if not isinstance(item.get("command"), str) or not item["command"]:
                raise ValueError("CLI command history is ambiguous")
            if kind in phases and phases[kind] != item:
                raise ValueError("CLI command history is ambiguous")
            if kind == "item.completed":
                if (
                    "item.started" not in phases
                    or phases["item.started"]["command"] != item["command"]
                    or type(item.get("exit_code")) is not int
                    or item.get("status") not in {"completed", "failed"}
                ):
                    raise ValueError("CLI command history is incomplete")
            elif item.get("exit_code") is not None or item.get("status") != "in_progress":
                raise ValueError("CLI command history is ambiguous")
            phases[kind] = item
        else:
            raise ValueError("CLI command history is unsupported")
    if active is not None:
        raise ValueError("CLI command history is incomplete")
    return {"cli_commands": len(commands), "cli_invocations": len(threads)}


async def retained_command_observation(store: Store, run_id: str) -> dict[str, int]:
    events: list[Data] = []
    total = 0
    async with store._unit():
        rows = await store.event_records.provider_budget_events(identifier(run_id))
        if len(rows) > MAX_EVENTS:
            raise ValueError("CLI command history exceeds the archive limit")
        seen: set[str] = set()
        for row in rows:
            event = row.data
            if row.id.hex in seen or event.get("id") != row.id.hex:
                raise ValueError("CLI command history is ambiguous")
            seen.add(row.id.hex)
            descriptor = validate_descriptor(event.get("activity_content"))
            if (
                descriptor["run_id"] != run_id
                or descriptor["event_id"] != row.id.hex
                or descriptor["stage"] != event.get("stage")
                or descriptor["status"] != "available"
            ):
                raise ValueError("CLI command history archive is incomplete")
            total += descriptor["sanitized_message_bytes"]
            if total > MAX_RUN_BYTES:
                raise ValueError("CLI command history exceeds the archive limit")
            payload = await store.event_content_records.payload(
                identifier(run_id),
                row.id,
                descriptor["sanitized_message_bytes"],
                descriptor["sanitized_message_sha256"],
            )
            if hashlib.sha256(payload).hexdigest() != descriptor["sanitized_message_sha256"]:
                raise ValueError("CLI command history archive is ambiguous")
            if descriptor["stage"] == "stdout":
                try:
                    events.append(json.loads(payload))
                except (ValueError, UnicodeError) as exc:
                    raise ValueError("CLI command history is unsupported") from exc
    return command_observation(events)


async def assert_environment_command_budget(
    store: Store, journal: Journal, run: Data, entries: list[Data], *, task_key: str | None = None
) -> None:
    """An observed CLI stream does not authorize its next unmetered shell effects."""
    if run.get("agent") not in {"codex", "claude"}:
        return
    observed = await retained_command_observation(store, run["id"])
    prior: set[str] = set()
    for entry in entries:
        if entry["kind"] != "provider_dispatch_intent":
            continue
        key = entry["payload"].get("task_key")
        if not isinstance(key, str) or len(key) != 32 or any(c not in "0123456789abcdef" for c in key):
            raise ValueError("CLI command history archive is ambiguous")
        if key != task_key:
            prior.add(key)
    if prior and len(prior) != observed["cli_invocations"]:
        raise ValueError("CLI command history archive is incomplete or ambiguous")
    if not observed["cli_invocations"] and not prior:
        return
    native = len(await journal.sandbox_executions(run["id"]))
    final = len(run.get("commands", [])) + len(policy_commands(run.get("policy_snapshot", {}), run["backend"]))
    limit = validate_sandbox_snapshot(run["sandbox_snapshot"]).command_limit
    remaining = limit - native - observed["cli_commands"]
    raise ValueError(
        f"Unmetered retained CLI continuation is held: CLI={observed['cli_commands']}, "
        f"Sandbox={native}, final={final}, remaining={remaining}. "
        "Observed CLI effects are not preventive shared-budget enforcement"
    )
