# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Recognize explicit owned handoffs without completing foreground commands."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Protocol

from firefly_forge.runner_monitor_models import MonitorStartInput
from firefly_forge.sandbox_models import SandboxInput, capture_sandbox
from firefly_forge.work_duration_models import effective_work_timeout

Data = dict[str, Any]


class LiveMonitors(Protocol):
    def current_handoff(self, identity: str) -> Data | None: ...


_live: ContextVar[LiveMonitors | None] = ContextVar("owned_runner_monitor", default=None)


@contextmanager
def monitor_scope(value: LiveMonitors | None) -> Iterator[None]:
    token = _live.set(value)
    try:
        yield
    finally:
        _live.reset(token)


def monitor_admits(row: Any, value: Data, entries: list[Data]) -> bool:
    request, effects = value["request"], value["effects"]
    inner = request["request"]
    handoff = effects.get("start", {}).get("evidence", {}).get("owned_process_handoff")
    if (
        inner.get("monitor") is not True
        or type(handoff) is not dict
        or set(handoff)
        != {"version", "monitor_id", "owner_epoch", "started_digest", "owner_grace_seconds", "work_deadline_ms"}
        or type(handoff.get("version")) is not int
        or handoff["version"] != 1
        or handoff.get("monitor_id") != row.id.hex
        or effects.get("start", {}).get("status") != "done"
        or type(handoff.get("owner_grace_seconds")) is not int
        or handoff["owner_grace_seconds"] < 1
        or type(handoff.get("owner_epoch")) is not str
        or len(handoff["owner_epoch"]) != 32
        or any(c not in "0123456789abcdef" for c in handoff["owner_epoch"])
        or (
            handoff["work_deadline_ms"] is not None
            and (type(handoff["work_deadline_ms"]) is not int or handoff["work_deadline_ms"] < 1)
        )
    ):
        return False
    try:
        configuration = SandboxInput.model_validate(inner.get("configuration"))
        request_model = MonitorStartInput.model_validate(
            {"argv": inner.get("argv"), "timeout_seconds": inner.get("timeout")}
        )
        if (
            configuration.mode != "host"
            or not configuration.process_monitor
            or configuration.work_duration is None
            or capture_sandbox(configuration)["digest"] != row.snapshot_digest
            or configuration.monitor_owner_grace_seconds != handoff["owner_grace_seconds"]
            or effective_work_timeout(
                configuration.work_duration.timeout_seconds,
                configuration.monitor_timeout_seconds,
                request_model.timeout_seconds,
            )
            != request_model.timeout_seconds
            or inner.get("output_limit") != configuration.max_output_bytes
        ):
            return False
    except (ValueError, TypeError):
        return False
    related = [
        e for e in entries if e["kind"].startswith("runner_monitor_") and e["payload"].get("monitor_id") == row.id.hex
    ]
    intents = [e for e in related if e["kind"] == "runner_monitor_spawn_intent"]
    started = [e for e in related if e["kind"] == "runner_monitor_started"]
    terminals = [e for e in related if e["kind"] == "runner_monitor_terminal"]
    if len(intents) != 1 or len(started) != 1 or len(terminals) > 1 or len(related) != 2 + len(terminals):
        return False
    begin = started[0]
    issued = begin["payload"].get("issued_at_ms")
    if type(issued) is not int or issued < 1:
        return False
    work_deadline = issued + request_model.timeout_seconds * 1000 if request_model.timeout_seconds is not None else None
    if work_deadline != handoff["work_deadline_ms"]:
        return False
    expected_request = {"argv": inner["argv"], "timeout_seconds": inner["timeout"]}
    for entry in (intents[0], begin):
        payload = entry["payload"]
        if (
            entry["run_id"] != row.run_id.hex
            or entry["task_id"] != row.task_id
            or payload.get("request") != expected_request
            or payload.get("parent_attempt") != row.parent_attempt
            or payload.get("snapshot_digest") != row.snapshot_digest
            or payload.get("owner_epoch") != handoff["owner_epoch"]
            or payload.get("owner_grace_seconds") != handoff["owner_grace_seconds"]
            or payload.get("work_deadline_ms") != handoff["work_deadline_ms"]
            or payload.get("issued_at_ms") != issued
        ):
            return False
    if begin["digest"] != handoff["started_digest"] or intents[0]["sequence"] >= begin["sequence"]:
        return False
    if terminals:
        end = terminals[0]
        payload = end["payload"]
        cleanup = effects.get("cleanup", {})
        return (
            end["sequence"] > begin["sequence"]
            and end["task_id"] == row.task_id
            and payload.get("owner_epoch") == handoff["owner_epoch"]
            and payload.get("parent_attempt") == row.parent_attempt
            and payload.get("snapshot_digest") == row.snapshot_digest
            and payload.get("work_deadline_ms") == handoff["work_deadline_ms"]
            and type(payload.get("exit_code")) is int
            and payload.get("status") in {"exited", "cancelled", "timed_out", "output_limit"}
            and payload.get("process_group_drained") is True
            and cleanup.get("status") == "done"
            and cleanup.get("evidence")
            == {"mode": "host", "monitor_terminal_digest": end["digest"], "process_group_drained": True}
            and row.status == "removed"
        )
    current = _live.get()
    return (
        "cleanup" not in effects
        and row.status == "finished"
        and type(handoff["owner_grace_seconds"]) is int
        and handoff["owner_grace_seconds"] > 0
        and current is not None
        and current.current_handoff(row.id.hex) == handoff
    )
