# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Consistent tenant-local read snapshots through native PyFly transactions."""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

from pyfly.container import service
from pyfly.data.transaction import current_unit_of_work
from sqlalchemy import func, select, text

from firefly_forge.factory_dashboard_models import (
    RUN_BUCKETS,
    STAGES,
    DashboardPageInput,
    DashboardScope,
    cursor_decode,
    cursor_encode,
    scope_digest,
)
from firefly_forge.factory_dashboard_persistence import FactoryDashboardRepository
from firefly_forge.identity import Principal
from firefly_forge.project_access import AccessDenied, ProjectAccess
from firefly_forge.store import Store


@service
class FactoryDashboard:
    def __init__(self, store: Store, access: ProjectAccess, records: FactoryDashboardRepository) -> None:
        self.store, self.access, self.records = store, access, records

    @staticmethod
    def _reader(actor: Principal | None) -> Principal:
        if actor is None or actor.authentication_method == "scm_mapping":
            raise AccessDenied("Use your signed-in factory identity to observe the dashboard")
        return actor

    def _binding(self, actor: Principal, scope: DashboardScope, *, search: str = "", bucket: str | None = None) -> str:
        identity = ":".join(
            (
                self.store.schema,
                actor.id.hex,
                actor.issuer,
                actor.subject,
                actor.authentication_method,
                actor.personal_token_id.hex if actor.personal_token_id else "",
            )
        )
        return scope_digest(scope, identity, search=search, bucket=bucket)

    @asynccontextmanager
    async def _observation(self) -> AsyncIterator[Any]:
        if current_unit_of_work() is not None:
            raise ValueError("Dashboard observation requires its own read transaction")
        async with self.store._unit() as session:
            await session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
            yield session

    @staticmethod
    def _session(actor: Principal, session_hash: str | None) -> None:
        if actor.authentication_method == "oidc_session" and session_hash is None:
            raise AccessDenied("The current native session is required for this observation")
        if session_hash is not None and not re.fullmatch(r"[a-f0-9]{64}", session_hash):
            raise AccessDenied("The current native session is unavailable")

    async def _fresh_authority(self, actor: Principal, before: tuple[Any, ...], session_hash: str | None) -> None:
        # This unit starts only after the repeatable-read observation has exited.
        # Reusing its snapshot would conceal a concurrent revoke or logout.
        async with self.store._unit():
            after = await self.records.authorization_witness(actor, session_hash)
        if after is None or after != before:
            raise AccessDenied("Your identity or project access changed during the observation; refresh to continue")

    async def _authority(self, actor: Principal, session_hash: str | None) -> tuple[Any, ...]:
        value = await self.records.authorization_witness(actor, session_hash)
        if value is None:
            raise AccessDenied("Your current identity or session is unavailable")
        return value

    async def projects(
        self, actor: Principal, page: DashboardPageInput, search: str = "", *, session_hash: str | None = None
    ) -> dict[str, Any]:
        actor = self._reader(actor)
        self._session(actor, session_hash)
        if actor.authentication_method == "personal_token":
            raise AccessDenied("Use an explicit product-project or run scope with a personal credential")
        page = DashboardPageInput.model_validate(page.model_dump())
        if page.bucket is not None or len(search) > 200:
            raise ValueError("Choose a project search and a valid project page")
        binding = self._binding(actor, DashboardScope(), search=search)
        after = cursor_decode(page.cursor, binding)
        if after is not None:
            try:
                UUID(hex=after)
            except ValueError:
                raise ValueError("Invalid product-project page position") from None
        async with self._observation():
            authority = await self._authority(actor, session_hash)
            administrator = await self.access.current_administrator(actor)
            rows = await self.records.projects(actor, administrator, after=after, search=search, limit=page.limit)
            items = [
                {
                    "id": row["id"].hex,
                    "name": row["name"],
                    "component_count": row["component_count"],
                    "repository_count": row["repository_count"],
                }
                for row in rows[: page.limit]
            ]
            result = {
                "version": 1,
                "items": items,
                "next_cursor": cursor_encode(binding, rows[page.limit - 1]["id"].hex)
                if len(rows) > page.limit
                else None,
            }
        await self._fresh_authority(actor, authority, session_hash)
        return result

    async def observe(
        self, actor: Principal, scope: DashboardScope, page: DashboardPageInput, *, session_hash: str | None = None
    ) -> dict[str, Any]:
        actor = self._reader(actor)
        self._session(actor, session_hash)
        scope = DashboardScope.model_validate(scope.model_dump())
        personal = actor.authentication_method == "personal_token"
        if personal and scope.kind not in {"project", "run"}:
            raise AccessDenied("Use an explicit product-project or run scope with a personal credential")
        allowed_projects = (
            tuple(row["project_id"] for row in await self.access.memberships(actor)) if personal else None
        )
        if personal and scope.project_id and scope.project_id not in (allowed_projects or ()):
            raise AccessDenied("This personal credential does not authorize the product project")
        page = DashboardPageInput.model_validate(page.model_dump())
        binding = self._binding(actor, scope, bucket=page.bucket)
        position = cursor_decode(page.cursor, binding)
        if position is not None and (not position.isdecimal() or int(position) < 1):
            raise ValueError("Invalid recorded run page position")
        async with self._observation() as session:
            authority = await self._authority(actor, session_hash)
            administrator = await self.access.current_administrator(actor)
            if scope.kind == "standalone" and not administrator:
                raise AccessDenied("Standalone work requires current factory-administrator access")
            project = None
            if scope.project_id:
                project = await self.records.project(
                    actor, administrator, scope.project_id, allowed_projects=allowed_projects
                )
                if project is None:
                    raise AccessDenied("This product project is not available to your current identity")
            observed_at = await session.scalar(select(func.clock_timestamp()))
            raw = await self.records.snapshot(
                actor,
                administrator,
                scope,
                after=int(position) if position else None,
                limit=page.limit,
                bucket=page.bucket,
                allowed_projects=allowed_projects,
            )
            if scope.kind == "run" and not sum(raw["counts"].values()):
                raise KeyError("Recorded run not available")
        await self._fresh_authority(actor, authority, session_hash)
        required_project = (
            scope.project_id
            if scope.kind == "project"
            else raw.get("scope_project_id")
            if scope.kind == "run"
            else None
        )
        if required_project:
            await self.access.require_project(actor, required_project, "read")
        elif scope.kind in {"standalone", "run"} and not await self.access.current_administrator(actor):
            raise AccessDenied("Standalone work requires current factory-administrator access")
        if personal:
            current = {row["project_id"] for row in await self.access.memberships(actor)}
            required = {scope.project_id} if scope.project_id else set(allowed_projects or ())
            if not required <= current:
                raise AccessDenied("Personal project access changed during the observation")
        await self._fresh_authority(actor, authority, session_hash)
        counts = {name: raw["counts"].get(name, 0) for name in (*RUN_BUCKETS, "unknown")}
        counts["total"] = sum(counts.values())
        stages = {
            name: {
                "id": name,
                **dict.fromkeys(
                    (
                        "active",
                        "done",
                        "waiting",
                        "skipped",
                        "failed",
                        "needs_action",
                        "expired",
                        "quarantined",
                        "unknown",
                    ),
                    0,
                ),
                "observed_until": None,
                "recorded_running": 0,
            }
            for name in STAGES
        }
        unmapped = 0
        for record in raw["stages"]:
            if record["stage"] in stages:
                stages[record["stage"]][record["state"]] += record["count"]
                stages[record["stage"]]["recorded_running"] += record.get("recorded_running", 0)
            elif record["state"] == "active":
                unmapped += record["count"]
        rows = raw["rows"]
        return {
            "version": 1,
            "scope": {**scope.model_dump(), "project": project},
            "observed_at": observed_at.isoformat(),
            "counts": counts,
            "associations": raw["associations"],
            "stages": list(stages.values()),
            "unmapped_active_tasks": unmapped,
            "runs": {
                "items": [self._run(row) for row in rows[: page.limit]],
                "next_cursor": cursor_encode(binding, str(rows[page.limit - 1]["sequence"]))
                if len(rows) > page.limit
                else None,
            },
            "standalone_available": administrator,
            "unavailable_metrics": ["worker_liveness", "queue_wait_duration", "deployment_health", "runtime_metrics"],
        }

    @staticmethod
    def _run(row: dict[str, Any]) -> dict[str, Any]:
        keys = (
            "status",
            "active_task",
            "brief",
            "brief_truncated",
            "created_at",
            "branch",
            "pr_url",
            "project_id",
            "project_name",
            "repository_name",
            "component_id",
            "component_name",
        )
        result = {key: row.get(key) for key in keys}
        result.update(id=row["id"].hex, repository_id=row["repository_id"].hex)
        result["association"] = (
            "standalone"
            if row.get("project_id") is None
            else "project"
            if row.get("project_name")
            else "project_unavailable"
        )
        result["issue_context"] = (
            {"url": row.get("issue_url"), "number": row.get("issue_number"), "title": row.get("issue_title")}
            if row.get("issue_url") or row.get("issue_number") is not None
            else None
        )
        return result
