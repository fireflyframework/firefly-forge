# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Bind finite owned host processes to native command ordinals and journal custody."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from firefly_forge.journal import digest
from firefly_forge.runner_monitor_custody import monitor_scope
from firefly_forge.runner_monitor_models import MonitorStartInput, MonitorWaitInput
from firefly_forge.runner_monitor_transport import OwnedProcessMonitor
from firefly_forge.sandbox_command_slots import assert_command_boundary
from firefly_forge.sandbox_models import SandboxInput, validate_sandbox_snapshot
from firefly_forge.work_duration_models import effective_work_timeout

if TYPE_CHECKING:
    from firefly_forge.sandbox import SandboxExecutor

Data = dict[str, Any]


class RunnerMonitors:
    def __init__(
        self,
        executor: SandboxExecutor,
        run_id: str,
        workspace: Path,
        task_id: str,
        snapshot: Data,
        config: SandboxInput,
        attempt: int,
        retained: frozenset[str],
    ) -> None:
        self.executor, self.run_id, self.task_id = executor, run_id, task_id
        self.workspace, self.snapshot, self.attempt = workspace, snapshot, attempt
        self.config = config
        self.payloads: dict[str, Data] = {}
        self.handoffs: dict[str, Data] = {}
        self.reservations: set[str] = set()
        self.transport = OwnedProcessMonitor(
            workspace,
            record=self._record,
            guard=self._guard,
            max_output_bytes=config.max_output_bytes,
            credential_registry=executor.credential_registry,
            retained_ids=retained,
            owner_grace_seconds=config.monitor_owner_grace_seconds,
        )

    @classmethod
    async def create(cls, executor: SandboxExecutor, run_id: str, workspace: Path, task_id: str) -> RunnerMonitors:
        assert_command_boundary()
        from firefly_forge.agentic_checkpoint_invocation import current_checkpoint_invocation

        if current_checkpoint_invocation() is not None:
            raise ValueError("Process monitoring is unavailable during required-checkpoint invocations")
        run = await executor._admit_execution(run_id, task_id)
        snapshot = run.get("sandbox_snapshot") or {}
        config = validate_sandbox_snapshot(snapshot)
        if config.mode != "host" or not config.process_monitor:
            raise ValueError("Process monitors require the explicit host process_monitor policy")
        stage = next(t for t in (await executor.journal.plan(run_id))["tasks"] if t["id"] == task_id)
        retained = frozenset(row["execution_id"] for row in await executor.journal.sandbox_executions(run_id))
        return cls(executor, run_id, workspace, task_id, snapshot, config, stage["attempt"], retained)

    async def _guard(self) -> None:
        run = await self.executor._admit_execution(self.run_id, self.task_id)
        stage = next(t for t in (await self.executor.journal.plan(self.run_id))["tasks"] if t["id"] == self.task_id)
        if run.get("sandbox_snapshot") != self.snapshot or stage["attempt"] != self.attempt:
            raise ValueError("Monitor frozen runner policy or stage attempt changed")

    def current_handoff(self, identity: str) -> Data | None:
        handoff = self.handoffs.get(identity)
        handle = self.transport._handles.get(identity)
        if (
            handoff is None
            or handle is None
            or handle.status != "running"
            or handle.process.returncode is not None
            or self.transport._closed
            or time.monotonic_ns() >= handle.ownership_deadline_ns
            or (handle.control is not None and handle.control.done())
            or (handoff["work_deadline_ms"] is not None and time.time_ns() // 1_000_000 >= handoff["work_deadline_ms"])
        ):
            return None
        return handoff

    def _release(self, identity: str) -> None:
        if identity in self.reservations:
            self.reservations.remove(identity)
            self.executor._capacity.release()

    async def _record(self, kind: str, payload: Data) -> None:
        identity = payload["monitor_id"]
        try:
            await self._guard()
            async with self.executor.store._unit():
                await self.executor.journal._run(self.run_id)
                await self._guard()
                self.executor.store.reject_credentials(payload)
                row = await self.executor.journal._append(
                    self.run_id,
                    self.task_id,
                    identity + ":" + kind,
                    "runner_" + kind,
                    {**payload, "parent_attempt": self.attempt, "snapshot_digest": self.snapshot["digest"]},
                )
                if kind == "monitor_started":
                    handoff = {
                        "version": 1,
                        "monitor_id": identity,
                        "owner_epoch": payload["owner_epoch"],
                        "started_digest": row["digest"],
                        "owner_grace_seconds": payload["owner_grace_seconds"],
                        "work_deadline_ms": payload["work_deadline_ms"],
                    }
                    await self.executor.journal.sandbox_effect_receipt(
                        self.run_id, self.task_id, identity, effect="start", evidence={"owned_process_handoff": handoff}
                    )
                elif (
                    kind == "monitor_terminal"
                    and payload["process_group_drained"] is True
                    and payload["status"] != "needs_reconciliation"
                ):
                    await self.executor.journal.sandbox_effect_intent(
                        self.run_id, self.task_id, identity, effect="cleanup", payload=self.payloads[identity]
                    )
                    await self.executor.journal.sandbox_effect_receipt(
                        self.run_id,
                        self.task_id,
                        identity,
                        effect="cleanup",
                        evidence={
                            "mode": "host",
                            "monitor_terminal_digest": row["digest"],
                            "process_group_drained": True,
                        },
                    )
                await self._guard()
            if kind == "monitor_started":
                self.handoffs[identity] = handoff
        finally:
            if kind == "monitor_terminal":
                self._release(identity)

    async def start(self, request: MonitorStartInput) -> Data:
        assert_command_boundary()
        request = MonitorStartInput.model_validate(request.model_dump(mode="json"))
        assert self.config.work_duration is not None
        request = request.model_copy(
            update={
                "timeout_seconds": effective_work_timeout(
                    self.config.work_duration.timeout_seconds,
                    self.config.monitor_timeout_seconds,
                    request.timeout_seconds,
                )
            }
        )
        await self._guard()
        async with self.executor._run_locks[self.run_id]:
            await self.executor._capacity.acquire()
            identity: str | None = None
            try:
                inner = {
                    "monitor": True,
                    "argv": request.argv,
                    "cwd": ".",
                    "workspace": str(self.transport.workspace),
                    "configuration": self.snapshot["configuration"],
                    "timeout": request.timeout_seconds,
                    "output_limit": self.config.max_output_bytes,
                }

                def prepare(ordinal: int, key: str, prior: Data | None) -> Data:
                    return {
                        "parent_attempt": self.attempt,
                        "ordinal": ordinal,
                        "container_name": "forge-sandbox-" + key,
                        "snapshot_digest": self.snapshot["digest"],
                        "request_digest": digest(inner),
                        "request": inner,
                    }

                with monitor_scope(self):
                    created = await self.executor.journal.sandbox_command_intent(
                        self.run_id, self.task_id, self.attempt, self.snapshot, prepare
                    )
                if not created["fresh"]:
                    raise ValueError("A retained monitor cannot be dispatched again")
                identity = created["execution"]["execution_id"]
                self.reservations.add(identity)
                self.payloads[identity] = payload = created["execution"]["request"]
                await self.executor.journal.sandbox_effect_receipt(
                    self.run_id, self.task_id, identity, effect="create", evidence={"mode": "host", "monitor": True}
                )
                started = await self.executor.journal.sandbox_effect_intent(
                    self.run_id, self.task_id, identity, effect="start", payload=payload
                )
                if started["disposition"] != "execute":
                    raise ValueError("Uncertain monitor start cannot be dispatched again")
                result = await self.transport.start(identity, request)
                self._release(identity)
                return result
            except BaseException:
                if identity is None:
                    self.executor._capacity.release()
                else:
                    self._release(identity)
                raise

    async def status(self, identity: str) -> Data:
        return await self.transport.status(identity)

    async def logs(self, identity: str) -> Data:
        return await self.transport.logs(identity)

    async def wait(self, request: MonitorWaitInput) -> Data:
        return await self.transport.wait(request)

    async def cancel(self, identity: str) -> Data:
        return await self.transport.cancel(identity)

    async def close(self) -> None:
        await asyncio.shield(self.transport.close())
        if any(handle.status == "needs_reconciliation" for handle in self.transport._handles.values()):
            from firefly_forge.sandbox import SandboxReconciliationRequired

            raise SandboxReconciliationRequired("Owned process terminal custody requires reconciliation")
