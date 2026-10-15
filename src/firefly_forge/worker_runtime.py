# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Headless native PyFly lifecycle with fenced worker promotion."""

from __future__ import annotations

import asyncio
import signal
from pathlib import Path
from typing import Any

from pyfly.kernel.lifecycle import CONSUMER_PHASE


class WorkerSupervision:
    phase = CONSUMER_PHASE + 100

    def __init__(self, engine: Any, consumers: dict[str, Any]) -> None:
        self.engine, self.consumers = engine, consumers
        self.engine.consumer_tasks = self.effect_tasks

    def effect_tasks(self) -> list[asyncio.Task[None]]:
        tasks = {task for task in self.tasks().values() if task is not None}
        for consumer in self.consumers.values():
            tasks.update(getattr(consumer, "active", {}).values())
        return list(tasks)

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        self.engine.prepare_shutdown()

    def health(self) -> dict[str, Any]:
        detail = self.engine.ownership_failure
        if not detail and self.engine.poller and self.engine.poller.done() and not self.engine.stopping:
            detail = "Execution scheduler stopped; inspect interrupted work before restarting"
        requests = self.engine.store.execution_requests
        if not detail and requests and requests.poller and requests.poller.done():
            detail = "Delivery request scheduler stopped; inspect retained execution requests before restarting"
        detail = detail or self.detail()
        role_health = (
            self.engine.store.runtime_roles.health(
                execution_state="degraded" if detail else self.engine.execution_state,
                max_parallel_jobs=self.engine.concurrency,
            )
            if self.engine.store.runtime_roles
            else {}
        )
        return {
            "status": "degraded" if detail else "ok",
            "detail": detail,
            "execution_ready": not bool(detail),
            **role_health,
        }

    def tasks(self) -> dict[str, asyncio.Task[None] | None]:
        return {name: consumer.poller for name, consumer in self.consumers.items()}

    def detail(self) -> str | None:
        roles = self.engine.store.runtime_roles
        if roles and not roles.executes_jobs:
            return None
        if self.engine.execution_state in {"standby", "recovering"}:
            return None
        for name, task in self.tasks().items():
            if task is None or task.done():
                return f"Required worker consumer {name} stopped; inspect retained work before restarting"
        return None


async def run_worker(data_dir: Path | None = None, *, stop: asyncio.Event | None = None) -> None:
    from firefly_forge.api import create_runtime

    stopped = stop or asyncio.Event()
    if stopped.is_set():
        return
    runtime = create_runtime(data_dir, role="worker")
    loop = asyncio.get_running_loop()
    registered = []
    if stop is None:
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stopped.set)
                registered.append(sig)
            except NotImplementedError:
                pass
    try:
        try:
            starting = asyncio.create_task(runtime.startup())
            stopping = asyncio.create_task(stopped.wait())
            try:
                await asyncio.wait([starting, stopping], return_when=asyncio.FIRST_COMPLETED)
                if stopped.is_set():
                    return
                await starting
            finally:
                if not starting.done():
                    starting.cancel()
                stopping.cancel()
                await asyncio.gather(starting, stopping, return_exceptions=True)
            supervision = runtime.context.get_bean(WorkerSupervision)
            if detail := supervision.detail():
                raise RuntimeError(detail)
            schedulers = list(supervision.tasks().values())
            stopping = asyncio.create_task(stopped.wait())
            try:
                await asyncio.wait(
                    [stopping, *(task for task in schedulers if task)], return_when=asyncio.FIRST_COMPLETED
                )
                if not stopped.is_set():
                    raise RuntimeError("A worker scheduler stopped; inspect interrupted work before restarting")
            finally:
                stopping.cancel()
                await asyncio.gather(stopping, return_exceptions=True)
        finally:
            await runtime.shutdown()
    finally:
        for sig in registered:
            loop.remove_signal_handler(sig)
