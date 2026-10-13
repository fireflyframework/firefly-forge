# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Headless native PyFly lifecycle for the single execution owner."""

from __future__ import annotations

import asyncio
import signal
from pathlib import Path
from typing import Any


class WorkerSupervision:
    def __init__(self, engine: Any, consumers: dict[str, Any]) -> None:
        self.engine, self.consumers = engine, consumers

    def tasks(self) -> dict[str, asyncio.Task[None] | None]:
        return {name: consumer.poller for name, consumer in self.consumers.items()}

    def detail(self) -> str | None:
        roles = self.engine.store.runtime_roles
        if roles and not roles.executes_jobs:
            return None
        for name, task in self.tasks().items():
            if task is None or task.done():
                return f"Required worker consumer {name} stopped; inspect retained work before restarting"
        return None


async def run_worker(data_dir: Path | None = None, *, stop: asyncio.Event | None = None) -> None:
    from firefly_forge.api import create_runtime

    runtime = create_runtime(data_dir, role="worker")
    stopped = stop or asyncio.Event()
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
            await runtime.startup()
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
