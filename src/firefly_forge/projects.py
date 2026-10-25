# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Product topology, source-component contracts and aggregate execution state."""

from __future__ import annotations

import copy
import hashlib
import json
import uuid
from typing import Any

from pyfly.container import service
from pyfly.data.relational.sqlalchemy import LockMode

from firefly_forge.models import RunInput
from firefly_forge.project_models import ProjectDefinition
from firefly_forge.project_persistence import (
    ForgeProjectEntity,
    ForgeProjectRepository,
    ForgeProjectRevisionEntity,
    ForgeProjectRevisionRepository,
)
from firefly_forge.project_snapshot import PreparedProjectRevision
from firefly_forge.store import Store, identifier, now

Data = dict[str, Any]


@service
class Projects:
    def __init__(
        self,
        store: Store,
        records: ForgeProjectRepository | None = None,
        revisions: ForgeProjectRevisionRepository | None = None,
    ) -> None:
        self.store, self.records = store, records or ForgeProjectRepository()
        self.revisions = revisions or ForgeProjectRevisionRepository()

    async def configure(self, definition: ProjectDefinition, project_id: str | None = None) -> Data:
        self.store.roles.snapshot(definition.roles, self.store.policies.selection())
        await self.store.admit_credentials(definition.model_dump())
        for repository_id in {component.repository_id for component in definition.components}:
            await self.store.repository(repository_id)
        async with self.store._unit():
            row = (
                await self.records.find_by_id(identifier(project_id), lock=LockMode.PESSIMISTIC_WRITE)
                if project_id
                else None
            )
            if project_id and row is None:
                raise KeyError("Product project not found")
            data: Data = {
                "id": project_id or uuid.uuid4().hex,
                "revision": row.data["revision"] + 1 if row else 1,
                "definition": definition.model_dump(),
                "updated_at": now(),
                "created_at": row.data["created_at"] if row else now(),
            }
            if row and row.data.get("git_delivery_policy"):
                data["git_delivery_policy"] = copy.deepcopy(row.data["git_delivery_policy"])
            if row and row.data.get("campaign_execution_policy"):
                data["campaign_execution_policy"] = copy.deepcopy(row.data["campaign_execution_policy"])
            data["digest"] = hashlib.sha256(json.dumps(data["definition"], sort_keys=True).encode()).hexdigest()
            if row:
                row.data = data
            else:
                row = ForgeProjectEntity(id=identifier(data["id"]), data=data)
            await self.records.save(row)
            await self.revisions.save(
                ForgeProjectRevisionEntity(project_id=row.id, revision=data["revision"], data=copy.deepcopy(data))
            )
            return copy.deepcopy(data)

    async def revision(self, project_id: str, revision: int) -> Data:
        async with self.store._unit():
            row = await self.revisions.revision(identifier(project_id), revision)
            if row is None:
                raise KeyError("Product project revision not found")
            return copy.deepcopy(row.data)

    async def project(self, project_id: str) -> Data:
        async with self.store._unit():
            row = await self.records.find_by_id(identifier(project_id))
            if row is None:
                raise KeyError("Product project not found")
            return copy.deepcopy(row.data)

    async def projects(self) -> list[Data]:
        async with self.store._unit():
            return [copy.deepcopy(row.data) for row in await self.records.latest()]

    async def component_snapshot(self, request: RunInput) -> Data | None:
        if not request.project_id:
            return None
        project = await self.project(request.project_id)
        return (await self.prepared_revision(project)).component(request)

    async def prepared_revision(self, project: Data) -> PreparedProjectRevision:
        from firefly_forge.project_snapshot import prepared_revision

        historical = await self.revision(project["id"], project["revision"])
        return prepared_revision(project, historical)

    async def snapshot_definition(
        self,
        component: Data,
        *,
        project_id: str | None = None,
        component_id: str | None = None,
        repository_id: str | None = None,
    ) -> Data:
        from firefly_forge.project_snapshot import snapshot_definition

        scope_project = project_id or component.get("project_id")
        if not isinstance(scope_project, str) or component.get("project_id", scope_project) != scope_project:
            raise ValueError("Project snapshot belongs to another execution project")
        project = await self.revision(scope_project, component["project_revision"])
        return snapshot_definition(
            component, project, project_id=project_id, component_id=component_id, repository_id=repository_id
        )

    async def overview(self, project_id: str) -> Data:
        project = await self.project(project_id)
        from firefly_forge.project_access import ProjectAccess

        runs = await ProjectAccess(self.store).filter_runs(await self.store.runs())
        components = []
        for component in project["definition"]["components"]:
            associated = [
                run
                for run in runs
                if run.get("project_id") == project_id
                and run.get("component_id") == component["id"]
                and run["repository_id"] == component["repository_id"]
                and run.get("component_snapshot", {}).get("path") == component["path"]
            ]
            repository_runs = [run for run in runs if run["repository_id"] == component["repository_id"]]
            components.append(
                {
                    **component,
                    "runs": [
                        {
                            key: run.get(key)
                            for key in ("id", "status", "branch", "source_tree", "pr_url", "operation", "created_at")
                        }
                        for run in associated
                    ],
                    "repository_run_count": len(repository_runs),
                    "latest_status": associated[0]["status"] if associated else "not_started",
                }
            )
        return {
            **project,
            "components": components,
            "counts": {
                status: sum(component["latest_status"] == status for component in components)
                for status in sorted({component["latest_status"] for component in components})
            },
        }
