# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Exact current Factory run/WAL custody before controller ownership renewal."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any
from uuid import UUID

from pyfly.data.relational.sqlalchemy import LockMode
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

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
from firefly_forge.runner_factory_models import validate_factory_pool
from firefly_forge.runner_pool_models import FactoryFence, RunnerInvocation
from firefly_forge.runner_run_liveness import (
    RUN_LIVENESS_PROTOCOL,
    run_cut_digest,
    run_digest,
    run_stage_active,
    run_task_key,
)
from firefly_forge.sandbox_persistence import ForgeSandboxRepository
from firefly_forge.store import identifier

if TYPE_CHECKING:
    from firefly_forge.runner_pools import RunnerPools


def validate_run_authority(
    invocation: RunnerInvocation,
    fence: FactoryFence,
    run: dict[str, Any],
    task: dict[str, Any],
    wal: dict[str, Any],
) -> None:
    binding = invocation.run_liveness
    if binding is None or any(type(value) is not dict for value in (run, task, wal)):
        raise ValueError("Explicit current run ownership is required")
    if (
        run.get("id") != invocation.run_id.hex
        or not run_stage_active(run, binding.task_id)
        or run.get("cancel_request", {}).get("status") == "requested"
        or UUID(str(run.get("repository_id"))) != invocation.repository_id
        or (UUID(str(run["project_id"])) if run.get("project_id") else None) != invocation.project_id
        or task.get("task_id") != binding.task_id
        or task.get("status") != "running"
        or type(task.get("attempt")) is not int
        or task["attempt"] != binding.attempt
        or task.get("idempotency_key") != run_task_key(invocation.run_id.hex, binding.task_id, binding.attempt)
        or run_digest(run.get("execution_actor")) != binding.actor_digest
        or run_cut_digest(run) != binding.run_cut_digest
        or run_digest(fence.model_dump(mode="json")) != binding.factory_fence_digest
    ):
        raise ValueError("Run ownership no longer has its exact active authority")
    snapshot = run.get("sandbox_snapshot") or {}
    policy = validate_factory_pool(snapshot)
    if policy.profile.run_liveness != RUN_LIVENESS_PROTOCOL or snapshot.get("digest") != binding.snapshot_digest:
        raise ValueError("Run ownership differs from the explicit frozen runner policy")
    policy.profile.validate_invocation(invocation)
    payload = wal.get("request") or {}
    command = payload.get("command") or {}
    if (
        wal.get("execution_id") != invocation.execution_id
        or wal.get("run_id") != invocation.run_id.hex
        or wal.get("task_id") != binding.task_id
        or wal.get("status") != "running"
        or wal.get("parent_attempt") != binding.attempt
        or wal.get("snapshot_digest") != binding.snapshot_digest
        or type(wal.get("ordinal")) is not int
        or not 1 <= wal["ordinal"] <= policy.command_limit
        or payload.get("ordinal") != wal["ordinal"]
        or payload.get("parent_attempt") != binding.attempt
        or payload.get("snapshot_digest") != binding.snapshot_digest
        or payload.get("factory_fence") != fence.model_dump(mode="json")
        or payload.get("request") != invocation.model_dump(mode="json")
        or payload.get("request_digest") != run_digest(invocation.model_dump(mode="json"))
        or command.get("argv") != list(invocation.argv)
        or command.get("cwd") != invocation.cwd
        or command.get("timeout") != invocation.timeout
        or command.get("snapshot_digest") != binding.snapshot_digest
        or wal.get("effects", {}).get("start", {}).get("status") != "intent"
        or "cleanup" in wal.get("effects", {})
    ):
        raise ValueError("Run ownership differs from the original active command WAL")


class RunnerRunAuthority:
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

    async def authorize(self, invocation: RunnerInvocation, fence: FactoryFence) -> None:
        if invocation.run_liveness is None or fence.factory_schema != self.schema:
            raise ValueError("Exact versioned Factory run ownership is required")
        async with self._unit():
            row = await ForgeRunRepository().find_by_id(invocation.run_id, lock=LockMode.PESSIMISTIC_WRITE)
            wal = await ForgeSandboxRepository().find_by_id(identifier(invocation.execution_id))
            tasks = await ForgeTaskRepository().find_for_run(invocation.run_id)
            task = next((item for item in tasks if item.task_id == invocation.run_liveness.task_id), None)
            if row is None or wal is None or task is None or row.id != invocation.run_id:
                raise ValueError("Original Factory run, task or command WAL is unavailable")

            def current() -> dict[str, Any]:
                run = {**row.data, "id": row.id.hex, "status": row.status}
                validate_run_authority(
                    invocation,
                    fence,
                    run,
                    {**task.data, "task_id": task.task_id, "status": task.status, "attempt": task.attempt},
                    Journal._sandbox_data(wal),
                )
                return run

            run = current()
            history = [
                {**entry.data, "digest": entry.digest}
                for entry in await ForgeJournalRepository().find_for_run(invocation.run_id)
            ]
            if not Journal.verify_chain(history) or not any(
                event["run_id"] == invocation.run_id.hex
                and event["task_id"] == invocation.run_liveness.task_id
                and event["kind"] == "sandbox_intent"
                and event["payload"].get("execution_id") == invocation.execution_id
                and event["payload"].get("effect") == "start"
                and event["payload"].get("request") == wal.data["request"]
                for event in history
            ):
                raise ValueError("Original active Factory command has no intact start intent")
            access = ProjectAccess(self)
            await access.authorize_execution(run, "release" if run.get("publish") else "execute")
            if await ForgeRepositoryRepository().find_by_id(invocation.repository_id) is None:
                raise ValueError("Original Factory repository is unavailable")
            actor = run.get("execution_actor")
            if actor and not await access.authorize_actor(actor, run.get("project_id"), "read"):
                user = await ForgeIdentityUserRepository().find_by_id(identifier(actor["user_id"]))
                if user is None:
                    raise ValueError("Original Factory repository reader is unavailable")
                await access.require_repository(
                    Principal(user.id, user.issuer, user.subject), invocation.repository_id.hex, "read"
                )
            current()
