# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Read current native Factory admission before a controller may continue one exact command."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from pyfly.data.relational.sqlalchemy import LockMode
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from firefly_forge.execution_request_persistence import (
    ForgeExecutionRequestEventRepository,
    ForgeExecutionRequestRepository,
)
from firefly_forge.execution_requests import ExecutionRequests, digest
from firefly_forge.identity import Principal
from firefly_forge.identity_persistence import ForgeIdentityUserRepository
from firefly_forge.journal import Journal
from firefly_forge.persistence import (
    ForgeJournalRepository,
    ForgeRepositoryRepository,
    ForgeRunRepository,
    ForgeTaskRepository,
)
from firefly_forge.project_access import ProjectAccess
from firefly_forge.runner_continuations import KIND, continuation_binding
from firefly_forge.runner_pool_models import FactoryFence, RunnerInvocation, pool_digest
from firefly_forge.sandbox_persistence import ForgeSandboxRepository
from firefly_forge.store import identifier

if TYPE_CHECKING:
    from firefly_forge.runner_pools import RunnerPools


class RunnerContinuationAuthority:
    def __init__(self, pools: RunnerPools) -> None:
        self.pools, self.schema = pools, pools.factory_schema

    @asynccontextmanager
    async def _unit(self) -> AsyncIterator[AsyncSession]:
        async with self.pools.sessions.unit() as session:
            previous = await session.scalar(text("SHOW search_path"))
            await session.execute(text(f'SET LOCAL search_path TO "{self.schema}"'))
            try:
                yield session
            finally:
                await session.execute(text("SELECT set_config('search_path',:path,true)"), {"path": previous})

    async def authorize(
        self, invocation: RunnerInvocation, fence: FactoryFence, request_id: str, request_digest: str
    ) -> None:
        async with self._unit():
            run = await ForgeRunRepository().find_by_id(invocation.run_id, lock=LockMode.PESSIMISTIC_WRITE)
            row = await ForgeExecutionRequestRepository().find_by_id(identifier(request_id))
            if (
                run is None
                or row is None
                or row.kind != KIND
                or row.run_id != run.id
                or row.status not in {"executing", "completed", "needs_reconciliation"}
                or row.data["request_digest"] != request_digest
                or digest(row.data["request"]) != request_digest
            ):
                raise RuntimeError("An exact dispatched Factory continuation admission is required")
            request = row.data["request"]
            entries = [event.data for event in await ForgeExecutionRequestEventRepository().events(row.id)]
            if not ExecutionRequests.verify_chain(entries) or not any(
                entry["kind"] == "worker_dispatch" and entry["evidence"].get("request_digest") == request_digest
                for entry in entries
            ):
                raise RuntimeError("Factory continuation has no trusted dispatch intent")
            wal = await ForgeSandboxRepository().find_by_id(identifier(invocation.execution_id))
            tasks = await ForgeTaskRepository().find_for_run(run.id)
            task = next((task for task in tasks if task.task_id == request["task_id"]), None)
            if wal is None or task is None or wal.run_id != run.id:
                raise ValueError("The original active Factory command is unavailable")
            binding = continuation_binding(
                run.data, Journal._sandbox_data(wal), {"status": task.status, "attempt": task.attempt}
            )
            if (
                binding != {key: value for key, value in request.items() if key != "actor"}
                or binding["fence"] != fence.model_dump(mode="json")
                or binding["invocation_digest"] != pool_digest(invocation.model_dump(mode="json"))
            ):
                raise ValueError("Controller continuation differs from original Factory authority")
            history = [
                {**entry.data, "digest": entry.digest} for entry in await ForgeJournalRepository().find_for_run(run.id)
            ]
            if not Journal.verify_chain(history) or not any(
                event["kind"] == "sandbox_intent"
                and event["payload"].get("execution_id") == invocation.execution_id
                and event["payload"].get("effect") == "start"
                and event["payload"].get("request") == wal.data["request"]
                for event in history
            ):
                raise RuntimeError("The original Factory WAL integrity failed")
            access = ProjectAccess(self)
            await access.authorize_actor(
                request["actor"], run.data.get("project_id"), "release" if run.data.get("publish") else "execute"
            )
            await access.authorize_execution(run.data)
            if await ForgeRepositoryRepository().find_by_id(invocation.repository_id) is None:
                raise ValueError("Original repository is unavailable")
            actor = run.data.get("execution_actor")
            if actor and not await access.authorize_actor(actor, run.data.get("project_id"), "read"):
                user = await ForgeIdentityUserRepository().find_by_id(identifier(actor["user_id"]))
                if user is None:
                    raise RuntimeError("Original repository reader is unavailable")
                await access.require_repository(
                    Principal(user.id, user.issuer, user.subject), run.data["repository_id"], "read"
                )
