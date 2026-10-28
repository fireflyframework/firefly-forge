# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Renewable host process ownership; new managers never adopt or restart handles."""

from __future__ import annotations

import asyncio
import codecs
import hashlib
import json
import os
import sys
import time
import uuid
from collections.abc import Awaitable, Callable
from contextlib import ExitStack, suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from firefly_forge.execution_environment import execution_environment
from firefly_forge.runner_monitor_guardian import require_supported
from firefly_forge.runner_monitor_models import (
    ExitCondition,
    FileCondition,
    MonitorObservation,
    MonitorStartInput,
    MonitorWaitEvidence,
    MonitorWaitInput,
)
from firefly_forge.secrets import contains_known_credential, runtime_environment
from firefly_forge.workspace_tools import WorkspaceTools, _source_stamp

Data = dict[str, Any]
Recorder = Callable[[str, Data], Awaitable[None]]


async def _allow() -> None:
    pass


@dataclass
class _Handle:
    identity: str
    process: asyncio.subprocess.Process
    issued_at_ms: int
    ownership_expires_at_ms: int
    work_deadline_ms: int | None
    environment: ExitStack
    token: str
    command_pid: int
    status: str = "running"
    output: bytearray = field(default_factory=bytearray)
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    terminal: asyncio.Event = field(default_factory=asyncio.Event)
    stop: asyncio.Event = field(default_factory=asyncio.Event)
    limit: asyncio.Event = field(default_factory=asyncio.Event)
    receipt: Data = field(default_factory=dict)
    reader: asyncio.Task[None] | None = None
    control: asyncio.Task[None] | None = None
    task: asyncio.Task[None] | None = None
    heartbeat_ack: asyncio.Event = field(default_factory=asyncio.Event)
    sent_sequence: int = 0
    acknowledged_sequence: int = 0
    sent_at_ms: int = 0
    sent_at_monotonic_ns: int = 0
    ownership_deadline_ns: int = 0


class OwnedProcessMonitor:
    def __init__(
        self,
        workspace: Path,
        *,
        record: Recorder,
        guard: Callable[[], Awaitable[None]] = _allow,
        max_output_bytes: int = 1_000_000,
        credential_registry: Callable[[], dict[str, str]] = dict,
        retained_ids: frozenset[str] = frozenset(),
        owner_grace_seconds: int = 30,
    ) -> None:
        if type(max_output_bytes) is not int or not 1024 <= max_output_bytes <= 2_000_000:
            raise ValueError("Monitor output must use the bounded runner output policy")
        if type(owner_grace_seconds) is not int or owner_grace_seconds < 1:
            raise ValueError("Monitor orphan grace must be a positive integer")
        self.owner_grace_seconds = owner_grace_seconds
        self.files = WorkspaceTools(workspace)
        self.workspace = self.files.root
        self.record, self.guard = record, guard
        self.max_output_bytes, self.credentials = max_output_bytes, credential_registry
        self.retained_ids = retained_ids
        self.epoch = uuid.uuid4().hex
        self._handles: dict[str, _Handle] = {}
        self._start_lock = asyncio.Lock()
        self._closed = False

    async def __aenter__(self) -> OwnedProcessMonitor:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    def _owned(self, identity: str) -> _Handle:
        if identity not in self._handles:
            raise ValueError("Monitor has no process owned by this invocation; reconciliation is required")
        return self._handles[identity]

    def _observation(self, handle: _Handle, *, final: bool = False) -> Data:
        visible, truncated = self._safe_output(handle)
        output = visible.encode()
        settled = final or handle.terminal.is_set()
        return MonitorObservation.model_validate(
            {
                "version": 1,
                "monitor_id": handle.identity,
                "owner_epoch": self.epoch,
                "status": handle.status if settled or handle.status == "running" else "settling",
                "issued_at_ms": handle.issued_at_ms,
                "ownership_expires_at_ms": handle.ownership_expires_at_ms,
                "work_deadline_ms": handle.work_deadline_ms,
                "exit_code": handle.receipt.get("exit_code") if settled else None,
                "output_bytes": len(output),
                "output_sha256": hashlib.sha256(output).hexdigest(),
                "output_truncated": truncated or handle.limit.is_set(),
            }
        ).model_dump(mode="json")

    async def start(self, identity: str, request: MonitorStartInput) -> Data:
        request = MonitorStartInput.model_validate(request.model_dump(mode="json"))
        if len(identity) != 32 or any(c not in "0123456789abcdef" for c in identity):
            raise ValueError("Monitor needs an exact allocated command identity")
        async with self._start_lock:
            require_supported()
            if self._closed or identity in self._handles:
                raise ValueError("Monitor is closed or command identity already started")
            if identity in self.retained_ids:
                raise ValueError("Retained monitor cannot dispatch again after restart")
            if contains_known_credential(request.model_dump(), self.credentials()):
                raise ValueError("Monitor arguments cannot contain configured credentials")
            await self.guard()
            anchored_monotonic_ns = time.monotonic_ns()
            issued_ns = time.time_ns()
            issued = issued_ns // 1_000_000
            work_deadline = issued + request.timeout_seconds * 1000 if request.timeout_seconds is not None else None
            work_deadline_ns = (
                anchored_monotonic_ns + work_deadline * 1_000_000 - issued_ns if work_deadline is not None else None
            )
            owner_expires = issued + self.owner_grace_seconds * 1000
            common = {
                "version": 1,
                "monitor_id": identity,
                "owner_epoch": self.epoch,
                "issued_at_ms": issued,
                "owner_grace_seconds": self.owner_grace_seconds,
                "work_deadline_ms": work_deadline,
                "request": request.model_dump(mode="json"),
            }
            await self.record("monitor_spawn_intent", common)
            await self.guard()
            if work_deadline_ns is not None and time.monotonic_ns() >= work_deadline_ns:
                raise ValueError("Chosen work deadline elapsed before dispatch")
            environment = ExitStack()
            token = uuid.uuid4().hex
            try:
                child_environment = environment.enter_context(execution_environment(runtime_environment()))
                process = await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-B",
                    "-I",
                    str(Path(__file__).with_name("runner_monitor_guardian.py")),
                    json.dumps(
                        {
                            "argv": request.argv,
                            "work_deadline_ns": work_deadline_ns,
                            "token": token,
                            "owner_grace_seconds": self.owner_grace_seconds,
                        }
                    ),
                    cwd=self.workspace,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    stdin=asyncio.subprocess.PIPE,
                    start_new_session=True,
                    env=child_environment,
                )
            except BaseException:
                environment.close()
                raise
            try:
                assert process.stderr is not None
                started = json.loads(await asyncio.wait_for(process.stderr.readline(), self.owner_grace_seconds))
                if (
                    set(started) != {"version", "stage", "token", "pid", "pgid"}
                    or type(started["version"]) is not int
                    or started["version"] != 1
                    or started["stage"] != "started"
                    or started["token"] != token
                    or type(started["pid"]) is not int
                    or started["pid"] <= 0
                    or started["pgid"] != started["pid"]
                ):
                    raise ValueError("Exact owned guardian process-group receipt required")
            except BaseException:
                if process.stdin is not None:
                    process.stdin.close()

                async def discard(stream: asyncio.StreamReader | None) -> None:
                    if stream is not None:
                        while await stream.read(65536):
                            pass

                await asyncio.wait_for(
                    asyncio.gather(discard(process.stdout), discard(process.stderr), process.wait()), 5
                )
                environment.close()
                raise
            handle = _Handle(
                identity, process, issued, owner_expires, work_deadline, environment, token, started["pid"]
            )
            handle.ownership_deadline_ns = time.monotonic_ns() + self.owner_grace_seconds * 1_000_000_000
            handle.reader = asyncio.create_task(self._output(handle))
            handle.control = asyncio.create_task(self._control(handle))
            self._handles[identity] = handle
            try:
                await self.guard()
                await self.record(
                    "monitor_started",
                    {
                        **common,
                        "pid": handle.command_pid,
                        "pgid": handle.command_pid,
                        "guardian_pid": process.pid,
                        "status": "running",
                    },
                )
            except BaseException:
                try:
                    await self._drain(handle)
                finally:
                    environment.close()
                    handle.status = "needs_reconciliation"
                    handle.terminal.set()
                raise
            handle.task = asyncio.create_task(self._supervise(handle))
            return await self._confirm_started(handle, work_deadline_ns)

    async def _confirm_started(self, handle: _Handle, work_deadline_ns: int | None) -> Data:
        ready = asyncio.create_task(handle.heartbeat_ack.wait())
        terminal = asyncio.create_task(handle.terminal.wait())
        try:
            done, _ = await asyncio.wait(
                (ready, terminal), timeout=self.owner_grace_seconds, return_when=asyncio.FIRST_COMPLETED
            )
            if not done:
                raise ValueError("Current guardian ownership handshake is unavailable")
            await self.guard()
            if not handle.terminal.is_set() and (
                handle.control is not None
                and handle.control.done()
                or handle.process.returncode is not None
                or time.monotonic_ns() >= handle.ownership_deadline_ns
                or work_deadline_ns is not None
                and time.monotonic_ns() >= work_deadline_ns
            ):
                assert handle.task is not None
                await asyncio.shield(handle.task)
            return self._observation(handle)
        except BaseException:
            await asyncio.shield(self.cancel(handle.identity))
            raise
        finally:
            ready.cancel()
            terminal.cancel()
            await asyncio.gather(ready, terminal, return_exceptions=True)

    async def _drain(self, handle: _Handle) -> None:
        if handle.process.stdin is not None and not handle.process.stdin.is_closing():
            with suppress(BrokenPipeError, ConnectionResetError):
                handle.process.stdin.write((json.dumps({"kind": "cancel", "token": handle.token}) + "\n").encode())
                await handle.process.stdin.drain()
            handle.process.stdin.close()
        assert handle.reader is not None and handle.control is not None
        outcomes = await asyncio.wait_for(
            asyncio.gather(handle.reader, handle.control, handle.process.wait(), return_exceptions=True), 5
        )
        if any(isinstance(result, BaseException) for result in outcomes):
            raise ValueError("Owned guardian control or stream drain requires reconciliation")
        if handle.process.returncode != 0 or not handle.receipt.get("process_group_drained"):
            raise ValueError("Owned guardian group and pipe drain could not be verified")

    async def _output(self, handle: _Handle) -> None:
        assert handle.process.stdout is not None
        while chunk := await handle.process.stdout.read(65536):
            remaining = self.max_output_bytes - len(handle.output)
            handle.output.extend(chunk[:remaining])
            handle.changed.set()
            if len(chunk) > remaining:
                handle.limit.set()

    async def _control(self, handle: _Handle) -> None:
        assert handle.process.stderr is not None
        while True:
            raw = await handle.process.stderr.readline()
            if len(raw) > 4096:
                raise ValueError("Guardian control receipt exceeds its protocol bounds")
            value = json.loads(raw)
            if value.get("stage") != "heartbeat_ack":
                await self._terminal_control(handle, value)
                return
            if (
                set(value) != {"version", "stage", "token", "pid", "pgid", "sequence"}
                or type(value["version"]) is not int
                or value["version"] != 1
                or value["token"] != handle.token
                or type(value["pid"]) is not int
                or value["pid"] != handle.command_pid
                or value["pgid"] != handle.command_pid
                or type(value["sequence"]) is not int
                or value["sequence"] != handle.sent_sequence
                or value["sequence"] <= handle.acknowledged_sequence
            ):
                raise ValueError("Owned guardian heartbeat identity or sequence changed")
            handle.acknowledged_sequence = value["sequence"]
            handle.ownership_expires_at_ms = handle.sent_at_ms + self.owner_grace_seconds * 1000
            handle.ownership_deadline_ns = handle.sent_at_monotonic_ns + self.owner_grace_seconds * 1_000_000_000
            handle.heartbeat_ack.set()

    async def _terminal_control(self, handle: _Handle, value: Data) -> None:
        assert handle.process.stderr is not None
        if (
            set(value)
            != {
                "version",
                "stage",
                "token",
                "pid",
                "pgid",
                "status",
                "exit_code",
                "term_signal",
                "kill_signal",
                "child_reaped",
                "process_group_drained",
            }
            or type(value["version"]) is not int
            or value["version"] != 1
            or value["stage"] != "terminal"
            or value["token"] != handle.token
            or value["pid"] != handle.command_pid
            or value["pgid"] != handle.command_pid
            or type(value["exit_code"]) is not int
            or value["child_reaped"] is not True
            or value["process_group_drained"] is not True
            or value["term_signal"] not in {"sent", "absent", "quiescent"}
            or value["kill_signal"] not in {"sent", "absent", "quiescent"}
            or value["status"] not in {"exited", "cancelled", "timed_out", "owner_lost", "needs_reconciliation"}
            or await handle.process.stderr.read(1)
        ):
            raise ValueError("Owned guardian terminal identity or group cleanup changed")
        handle.receipt = value

    async def _supervise(self, handle: _Handle) -> None:
        async def current() -> None:
            while True:
                await self.guard()
                assert handle.process.stdin is not None
                handle.heartbeat_ack.clear()
                handle.sent_sequence += 1
                handle.sent_at_ms = time.time_ns() // 1_000_000
                handle.sent_at_monotonic_ns = time.monotonic_ns()
                try:
                    handle.process.stdin.write(
                        (
                            json.dumps({"kind": "heartbeat", "token": handle.token, "sequence": handle.sent_sequence})
                            + "\n"
                        ).encode()
                    )
                    await handle.process.stdin.drain()
                except (BrokenPipeError, ConnectionResetError):
                    return
                await asyncio.wait_for(handle.heartbeat_ack.wait(), self.owner_grace_seconds)
                await asyncio.sleep(min(self.owner_grace_seconds * 1_000_000_000 // 3, 60_000_000_000) / 1_000_000_000)

        assert handle.reader is not None and handle.control is not None
        reader = handle.reader
        authority = asyncio.create_task(current())
        stopped = asyncio.create_task(handle.stop.wait())
        limited = asyncio.create_task(handle.limit.wait())
        try:
            done, _ = await asyncio.wait(
                (reader, handle.control, authority, stopped, limited), return_when=asyncio.FIRST_COMPLETED
            )
            if stopped in done:
                handle.status = "cancelled"
            elif limited in done:
                handle.status = "output_limit"
            elif handle.control in done:
                await handle.control
                handle.status = "exited"
            elif reader in done:
                await reader
                handle.status = "exited"
            else:
                await authority
                handle.status = "exited"
        except OverflowError:
            handle.status = "output_limit"
        except asyncio.CancelledError:
            handle.status = "cancelled"
        except Exception:
            handle.status = "needs_reconciliation"
        finally:
            drained = False
            try:
                await self._drain(handle)
                drained = True
                if handle.status == "exited":
                    handle.status = (
                        handle.receipt["status"]
                        if handle.receipt["status"] in {"exited", "cancelled", "timed_out"}
                        else "needs_reconciliation"
                    )
            except Exception:
                handle.status = "needs_reconciliation"
            finally:
                for task in (reader, authority, stopped, limited):
                    task.cancel()
                await asyncio.gather(reader, authority, stopped, limited, return_exceptions=True)
                handle.environment.close()
            try:
                await self.record(
                    "monitor_terminal",
                    {
                        **self._observation(handle, final=True),
                        "exit_code": handle.receipt.get("exit_code"),
                        "process_group_drained": drained,
                        "guardian_receipt": {k: v for k, v in handle.receipt.items() if k != "token"},
                    },
                )
            except (Exception, asyncio.CancelledError):
                handle.status = "needs_reconciliation"
            handle.terminal.set()
            handle.changed.set()

    async def status(self, identity: str) -> Data:
        await self.guard()
        if identity in self.retained_ids and identity not in self._handles:
            return {"version": 1, "monitor_id": identity, "status": "needs_reconciliation", "owner_epoch": None}
        return self._observation(self._owned(identity))

    async def logs(self, identity: str) -> Data:
        await self.guard()
        handle = self._owned(identity)
        output, truncated = self._safe_output(handle)
        return {
            **self._observation(handle),
            "output": output,
            "output_bytes": len(output.encode()),
            "output_sha256": hashlib.sha256(output.encode()).hexdigest(),
            "output_truncated": truncated or handle.limit.is_set(),
        }

    def _safe_output(self, handle: _Handle) -> tuple[str, bool]:
        from firefly_forge.configuration import redact_secrets
        from firefly_forge.secrets import credential_representations, credential_values, redact_environment

        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        output = decoder.decode(handle.output, final=False)
        registry = self.credentials()
        # A credential can be split across pipe reads; withhold an unfinished suffix.
        withheld = 0
        for secret in credential_values(registry):
            for representation in credential_representations(secret):
                if output.endswith(representation):
                    continue
                size = min(len(output), len(representation) - 1)
                while size > withheld and not output.endswith(representation[:size]):
                    size -= 1
                withheld = max(withheld, size)
        if withheld:
            output = output[:-withheld] + "[redacted]"

        redacted = redact_secrets(redact_environment(output), registry).encode()
        return redacted[: self.max_output_bytes].decode("utf-8", errors="ignore"), (
            len(redacted) > self.max_output_bytes or bool(decoder.getstate()[0])
        )

    async def _observe_file(self, condition: FileCondition, end: int | None) -> tuple[bool, Data]:
        from firefly_forge.secrets import KnownCredentialScan, credential_values

        if self.files._source_absence(condition.path) is not None:
            return False, {}
        try:
            with self.files._source_file(condition.path) as stream:
                stamp = _source_stamp(os.fstat(stream.fileno()))
                content = hashlib.sha256()
                decoder = codecs.getincrementaldecoder("utf-8")(errors="ignore")
                scanner = KnownCredentialScan(credential_values(self.credentials()), bearer=False)
                while chunk := stream.read(65536):
                    if scanner.feed(decoder.decode(chunk)):
                        raise ValueError("File wait evidence containing configured credentials is unavailable")
                    content.update(chunk)
                    await asyncio.sleep(0)
                    if end is not None and time.monotonic_ns() >= end:
                        return False, {}
                if scanner.feed(decoder.decode(b"", final=True)) or scanner.feed(None):
                    raise ValueError("File wait evidence containing configured credentials is unavailable")
                with self.files._source_file(condition.path) as selected:
                    if _source_stamp(os.fstat(selected.fileno())) != stamp:
                        return False, {}
            observed = content.hexdigest()
            return condition.sha256 is None or condition.sha256 == observed, {"file_sha256": observed}
        except ValueError as exc:
            if str(exc) == "Selected source changed while reading":
                return False, {}
            raise

    async def wait(self, request: MonitorWaitInput) -> Data:
        request = MonitorWaitInput.model_validate(request.model_dump(mode="json"))
        if contains_known_credential(request.model_dump(), self.credentials()):
            raise ValueError("Wait conditions cannot contain configured credentials")
        handle = self._owned(request.monitor_id)
        end = (
            time.monotonic_ns() + request.timeout_seconds * 1_000_000_000
            if request.timeout_seconds is not None
            else None
        )
        await self.guard()
        while True:
            handle.changed.clear()
            extra: Data = {}
            condition = request.condition
            if isinstance(condition, ExitCondition):
                satisfied = handle.terminal.is_set() and handle.status != "needs_reconciliation"
            elif isinstance(condition, FileCondition):
                satisfied, extra = await self._observe_file(condition, end)
            else:
                satisfied = condition.text in self._safe_output(handle)[0]
            if satisfied or handle.terminal.is_set() or (end is not None and time.monotonic_ns() >= end):
                await self.guard()
                return MonitorWaitEvidence.model_validate(
                    {
                        "version": 1,
                        "condition": condition.model_dump(mode="json"),
                        "outcome": "unavailable"
                        if handle.terminal.is_set() and handle.status == "needs_reconciliation"
                        else "satisfied"
                        if satisfied
                        else "process_ended"
                        if handle.terminal.is_set()
                        else "timeout",
                        "observation": self._observation(handle),
                        **extra,
                    }
                ).model_dump(mode="json", exclude_unset=True)
            remaining = max(1, end - time.monotonic_ns()) if end is not None else None
            with suppress(TimeoutError):
                if isinstance(condition, FileCondition):
                    await asyncio.wait_for(
                        handle.changed.wait(), min(50_000_000, remaining or 50_000_000) / 1_000_000_000
                    )
                elif remaining is None:
                    await handle.changed.wait()
                else:
                    await asyncio.wait_for(handle.changed.wait(), min(remaining, 60_000_000_000) / 1_000_000_000)

    async def cancel(self, identity: str) -> Data:
        handle = self._owned(identity)
        if not handle.terminal.is_set() and handle.task is not None:
            handle.stop.set()
            await asyncio.shield(handle.task)
        return self._observation(handle)

    async def close(self) -> None:
        self._closed = True
        for identity in self._handles:
            await self.cancel(identity)
