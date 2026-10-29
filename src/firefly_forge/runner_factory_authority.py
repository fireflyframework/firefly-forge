# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Native retained run/project/stage authority before any controller dispatch or transfer."""

from __future__ import annotations

from typing import Any
from uuid import NAMESPACE_URL, uuid5

from pyfly.container import service
from sqlalchemy import text

from firefly_forge.identity import Principal
from firefly_forge.identity_persistence import ForgeIdentityUserRepository
from firefly_forge.journal import Journal
from firefly_forge.project_access import ProjectAccess
from firefly_forge.runner_pool_models import FactoryFence
from firefly_forge.runner_pool_persistence import ForgeRunnerInvocationRepository
from firefly_forge.store import Store, identifier


@service
class RunnerFactoryAuthority:
    def __init__(self, store: Store, journal: Journal) -> None:
        self.store, self.journal = store, journal

    async def authorize(self, run_id: str, task_id: str) -> dict[str, Any]:
        if task_id not in {"implement", "verify"}:
            raise ValueError("Pool commands require an implementation or verification stage")
        await self.store.assert_ownership()
        run = await self.store.get_run(run_id)
        configuration = (run.get("sandbox_snapshot") or {}).get("configuration", {})
        legacy = (
            run.get("work_duration") is None
            and configuration.get("work_duration") is None
            and configuration.get("profile", {}).get("work_duration") is None
        )
        if (
            run["status"] != {"implement": "developing", "verify": "validating"}[task_id]
            and not (legacy and run["status"] == "running")
            or run.get("active_task") != task_id
            or run.get("cancel_request", {}).get("status") == "requested"
        ):
            raise RuntimeError("Factory command dispatch is cancelled or the run is not active")
        project, component = run.get("project_id"), run.get("component_snapshot")
        if project and (
            not component
            or identifier(component["project_id"]) != identifier(project)
            or identifier(component["repository_id"]) != identifier(run["repository_id"])
        ):
            raise ValueError("Retained component does not grant this project/repository binding")
        await self.store.repository(run["repository_id"])
        access = ProjectAccess(self.store)
        await access.authorize_execution(run)
        actor = run.get("execution_actor")
        if actor and not await access.authorize_actor(actor, project, "read"):
            async with self.store._unit():
                user = await ForgeIdentityUserRepository().find_by_id(identifier(actor["user_id"]))
                if user is None:
                    raise RuntimeError("The retained repository read identity is unavailable")
                principal = Principal(user.id, user.issuer, user.subject)
            await access.require_repository(principal, run["repository_id"], "read")
        stage = next((value for value in (await self.journal.plan(run_id))["tasks"] if value["id"] == task_id), None)
        if stage is None or stage["status"] != "running":
            raise RuntimeError("Pool dispatch requires the retained active stage")
        expected_key = uuid5(NAMESPACE_URL, f"forge:{identifier(run_id).hex}:{task_id}:{stage['attempt']}").hex
        if stage.get("idempotency_key") != expected_key:
            raise RuntimeError("Pool dispatch requires its exact original stage intent")
        fence = await self.exclusive_fence()
        return {"run": run, "attempt": stage["attempt"], "fence": fence}

    async def exclusive_fence(self) -> FactoryFence:
        """Owner-only cleanup authority; this never grants project dispatch or artifact import."""
        await self.store.assert_ownership()
        pid = self.store._ownership_pid
        if pid is None:
            raise RuntimeError("Factory owner identity is unavailable")
        async with self.store._unit() as session:
            started = await session.scalar(
                text("SELECT backend_start FROM pg_stat_activity WHERE pid=:pid"), {"pid": pid}
            )
            fence = FactoryFence(factory_schema=self.store.schema, pid=pid, backend_started_at=started)
            if not await ForgeRunnerInvocationRepository().factory_owned(fence):
                raise RuntimeError("Exact exclusive factory owner identity was lost")
        await self.store.assert_ownership()
        return fence
