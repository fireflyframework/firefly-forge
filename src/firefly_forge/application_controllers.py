# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Authenticated operational configuration, evidence and maintenance endpoints."""

from __future__ import annotations

from typing import Any

from pyfly.container import rest_controller
from pyfly.web import Body, PathVar, get_mapping, post_mapping, put_mapping, request_mapping
from sqlalchemy import func, select
from starlette.responses import PlainTextResponse

from firefly_forge.application_operations import (
    ApplicationInput,
    ApplicationOperations,
    IncidentResolution,
    MaintenanceRequest,
)
from firefly_forge.deployment_credentials import CredentialInput
from firefly_forge.persistence import ForgeDeploymentEntity, ForgeRunEntity
from firefly_forge.worker_runtime import WorkerSupervision


@rest_controller
@request_mapping("/api/v1")
class ApplicationController:
    def __init__(self, applications: ApplicationOperations, supervision: WorkerSupervision) -> None:
        self.applications = applications
        self.supervision = supervision

    @get_mapping("/applications")
    async def list_applications(self) -> list[dict[str, Any]]:
        return await self.applications.applications()

    @get_mapping("/applications/{id}")
    async def get(self, id: PathVar[str]) -> dict[str, Any]:
        return await self.applications.application(id)

    @put_mapping("/applications/{id}")
    async def configure(self, id: PathVar[str], body: Body[ApplicationInput]) -> dict[str, Any]:
        return await self.applications.configure(id, body)

    @post_mapping("/applications/{id}/check")
    async def check(self, id: PathVar[str]) -> dict[str, Any]:
        return await self.applications.check(id)

    @get_mapping("/incidents")
    async def incidents(self) -> list[dict[str, Any]]:
        return await self.applications.incidents()

    @post_mapping("/incidents/{id}/maintenance", status_code=201)
    async def maintenance(self, id: PathVar[str], body: Body[MaintenanceRequest]) -> dict[str, Any]:
        return await self.applications.maintenance(id, body)

    @post_mapping("/incidents/{id}/resolve")
    async def resolve(self, id: PathVar[str], body: Body[IncidentResolution]) -> dict[str, Any]:
        return await self.applications.resolve(id, body.resolution)

    @get_mapping("/deployment-credentials")
    def credentials(self) -> list[dict[str, Any]]:
        return self.applications.deployments.credentials.catalog()

    @put_mapping("/deployment-credentials/{id}")
    def configure_credentials(self, id: PathVar[str], body: Body[CredentialInput]) -> dict[str, Any]:
        return self.applications.deployments.credentials.configure(id, body)

    @get_mapping("/metrics")
    async def metrics(self) -> PlainTextResponse:
        health = self.supervision.health()
        lines = [
            "# HELP forge_execution_ready Local project execution readiness; does not attest to a remote worker.",
            "# TYPE forge_execution_ready gauge",
            f"forge_execution_ready {int(health['execution_ready'])}",
            "# HELP forge_admission_ready Local runtime admission readiness; does not attest to command execution.",
            "# TYPE forge_admission_ready gauge",
            f"forge_admission_ready {int(health.get('admission_ready', health['status'] == 'ok'))}",
        ]
        async with self.applications.store._unit() as session:
            for name, entity in (("forge_runs", ForgeRunEntity), ("forge_deployments", ForgeDeploymentEntity)):
                lines.append(f"# TYPE {name} gauge")
                for status, count in await session.execute(select(entity.status, func.count()).group_by(entity.status)):
                    lines.append(f'{name}{{status="{status}"}} {count}')
        lines.append("# TYPE forge_application_healthy gauge")
        for application in await self.applications.applications():
            healthy = not application["stale"] and application["latest"]["status"] == "healthy"
            lines.append(f'forge_application_healthy{{application="{application["id"]}"}} {int(healthy)}')
        return PlainTextResponse("\n".join(lines) + "\n", media_type="text/plain; version=0.0.4")
