# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Product topology, source-component contracts and aggregate execution state."""

from __future__ import annotations

import copy
import hashlib
import json
import uuid
from typing import Any

from pyfly.data.relational.sqlalchemy import LockMode

from firefly_forge.models import RunInput
from firefly_forge.project_models import ProjectDefinition
from firefly_forge.project_persistence import (
    ForgeProjectEntity,
    ForgeProjectRepository,
    ForgeProjectRevisionEntity,
    ForgeProjectRevisionRepository,
)
from firefly_forge.store import Store, identifier, now

Data = dict[str, Any]


class Projects:
    def __init__(self, store: Store, records: ForgeProjectRepository | None = None) -> None:
        self.store, self.records = store, records or ForgeProjectRepository()

    async def configure(self, definition: ProjectDefinition, project_id: str | None = None) -> Data:
        self.store.roles.snapshot(definition.roles, self.store.policies.selection())
        self.store.reject_credentials(definition.model_dump())
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
            data["digest"] = hashlib.sha256(json.dumps(data["definition"], sort_keys=True).encode()).hexdigest()
            if row:
                row.data = data
            else:
                row = ForgeProjectEntity(id=identifier(data["id"]), data=data)
            await self.records.save(row)
            await ForgeProjectRevisionRepository().save(
                ForgeProjectRevisionEntity(project_id=row.id, revision=data["revision"], data=copy.deepcopy(data))
            )
            return copy.deepcopy(data)

    async def revision(self, project_id: str, revision: int) -> Data:
        async with self.store._unit():
            row = await ForgeProjectRevisionRepository().revision(identifier(project_id), revision)
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
        definition = ProjectDefinition.model_validate(project["definition"])
        component = next(
            (component for component in definition.components if component.id == request.component_id), None
        )
        if component is None or component.repository_id != request.repository_id:
            raise ValueError("The run's repository must belong to the selected product component")
        for key in ("backend", "frontend"):
            if key in request.model_fields_set and getattr(request, key) != getattr(component, key):
                raise ValueError("The run's stack must match its configured component")
        return {
            "project_id": project["id"],
            "project_name": definition.name,
            "project_revision": project["revision"],
            "project_digest": project["digest"],
            "topology": definition.topology,
            "product_repository_id": definition.product_repository_id,
            "project_definition": copy.deepcopy(project["definition"]),
            **component.model_dump(),
        }

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
