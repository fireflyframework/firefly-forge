# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Actual health and metric observations linked to deployment and maintenance evidence."""

from __future__ import annotations

import asyncio
import copy
import math
import re
import time
import uuid
from datetime import datetime
from typing import Any, Literal
from urllib.parse import parse_qsl, urlsplit, urlunsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator
from pyfly.container import service
from pyfly.data.relational.sqlalchemy import LockMode
from pyfly.kernel.lifecycle import CONSUMER_PHASE
from sqlalchemy import text

from firefly_forge.application_persistence import (
    ForgeIncidentEntity,
    ForgeIncidentRepository,
    ForgeObservationEntity,
    ForgeObservationRepository,
)
from firefly_forge.configuration import validate_endpoint
from firefly_forge.engine import Engine
from firefly_forge.models import Backend, Command, Frontend, RunInput
from firefly_forge.operations import DeploymentManager
from firefly_forge.store import Store, identifier, now


class MetricThreshold(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(pattern=r"^[a-zA-Z_:][a-zA-Z0-9_:]{0,199}$")
    operator: Literal["gt", "lt"] = "gt"
    value: float = Field(allow_inf_nan=False)


class ObservabilityLinks(BaseModel):
    model_config = ConfigDict(extra="forbid")
    logs_url: str | None = Field(default=None, max_length=2048)
    traces_url: str | None = Field(default=None, max_length=2048)
    dashboard_url: str | None = Field(default=None, max_length=2048)

    @field_validator("logs_url", "traces_url", "dashboard_url")
    @classmethod
    def safe_links(cls, value: str | None) -> str | None:
        if not value:
            return None
        parsed = urlsplit(value)
        validate_endpoint(urlunsplit(parsed._replace(query="")))
        if any(ord(character) < 32 for character in value):
            raise ValueError("Dashboard links must not contain control characters")
        for key, _ in parse_qsl(parsed.query, keep_blank_values=True):
            normalized = key.casefold().replace("-", "_")
            if any(
                part in normalized for part in ("token", "secret", "password", "api_key", "credential")
            ) or normalized in {"key", "sig", "signature", "authorization", "auth"}:
                raise ValueError("Dashboard links must not contain query credentials")
        return value


class ApplicationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=120)
    repository_id: str = Field(min_length=1, max_length=100)
    target_id: str = Field(pattern=r"^[a-z][a-z0-9-]{0,63}$")
    backend: Backend = "pyfly"
    frontend: Frontend = "react"
    health_url: str = Field(min_length=1, max_length=2048)
    metrics_url: str | None = Field(default=None, max_length=2048)
    interval_seconds: int = Field(default=60, ge=30, le=86400)
    max_latency_ms: int = Field(default=3000, ge=1, le=60000)
    metric_thresholds: list[MetricThreshold] = Field(default_factory=list, max_length=30)
    observability: ObservabilityLinks = Field(default_factory=ObservabilityLinks)

    @field_validator("health_url", "metrics_url")
    @classmethod
    def safe_url(cls, value: str | None) -> str | None:
        return validate_endpoint(value) if value else None


class MaintenanceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    brief: str = Field(min_length=1, max_length=20000)
    agent: Literal["codex", "claude", "bridge", "agentic"] = "codex"
    commands: list[Command] = Field(min_length=1, max_length=20)


class IncidentResolution(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    resolution: str = Field(min_length=1, max_length=8000)


def prometheus_metrics(value: str) -> dict[str, float]:
    """Keep unlabelled finite gauges; labelled series need an explicit aggregation upstream."""
    result: dict[str, float] = {}
    for line in value.splitlines():
        match = re.fullmatch(r"([a-zA-Z_:][a-zA-Z0-9_:]*)\s+([^\s]+)(?:\s+\d+)?", line.strip())
        if match:
            try:
                number = float(match[2])
            except ValueError:
                continue
            if math.isfinite(number):
                result[match[1]] = number
        if len(result) >= 200:
            break
    return result


@service
class ApplicationOperations:
    phase = CONSUMER_PHASE + 2

    def __init__(
        self,
        store: Store,
        engine: Engine,
        deployments: DeploymentManager,
        observations: ForgeObservationRepository | None = None,
        incidents: ForgeIncidentRepository | None = None,
    ) -> None:
        self.store, self.engine, self.deployments = store, engine, deployments
        self.observations = observations or ForgeObservationRepository()
        self.incident_records = incidents or ForgeIncidentRepository()
        self.path = store.configuration.directory / "applications.json"
        self.transport: httpx.AsyncBaseTransport | None = None
        self.poller: asyncio.Task[None] | None = None
        self.probing = asyncio.Semaphore(4)

    def _configuration(self) -> dict[str, Any]:
        with self.store.configuration.lock:
            return self.store.configuration._read(self.path)

    async def configure(self, app_id: str, request: ApplicationInput) -> dict[str, Any]:
        if not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", app_id):
            raise ValueError("Use a lowercase application identifier")
        await self.store.repository(request.repository_id)
        target = next((item for item in self.deployments.targets() if item["id"] == request.target_id), None)
        if not target or target["repository_id"] != request.repository_id:
            raise ValueError("Choose a deployment target belonging to this project")
        with self.store.configuration.lock:
            data = self.store.configuration._read(self.path)
            previous = data.get(app_id)
            if previous and previous["repository_id"] != request.repository_id:
                raise ValueError("An application's project cannot change")
            data[app_id] = {**request.model_dump(), "revision": uuid.uuid4().hex, "updated_at": now()}
            self.store.configuration._write(self.path, data)
        return await self.application(app_id)

    async def applications(self) -> list[dict[str, Any]]:
        return [await self.application(app_id) for app_id in self._configuration()]

    async def application(self, app_id: str) -> dict[str, Any]:
        config = self._configuration().get(app_id)
        if not config:
            raise KeyError("Application not found")
        async with self.store._unit():
            samples = [copy.deepcopy(row.data) for row in await self.observations.for_application(app_id)]
            incidents = [copy.deepcopy(row.data) for row in await self.incident_records.latest(app_id)]
        latest = samples[0] if samples else None
        stale = (
            not latest
            or latest["configuration_revision"] != config["revision"]
            or time.time() - datetime.fromisoformat(latest["created_at"]).timestamp() > config["interval_seconds"] * 2
        )
        jobs = [job for job in await self.store.deployments() if job["target_id"] == config["target_id"]]
        return {
            "id": app_id,
            **config,
            "latest": latest,
            "stale": bool(stale),
            "observations": samples,
            "incidents": incidents,
            "latest_deployment": jobs[0] if jobs else None,
        }

    async def _get(self, url: str) -> tuple[int, str]:
        async with (
            httpx.AsyncClient(timeout=5, follow_redirects=False, transport=self.transport) as client,
            client.stream("GET", url, headers={"Accept": "text/plain,application/json"}) as response,
        ):
            payload = bytearray()
            async for chunk in response.aiter_bytes():
                payload.extend(chunk)
                if len(payload) > 131072:
                    raise ValueError("Probe response exceeds 128 KB")
            return response.status_code, payload.decode("utf-8", errors="replace")

    async def check(self, app_id: str) -> dict[str, Any]:
        config = self._configuration().get(app_id)
        if not config:
            raise KeyError("Application not found")
        reasons: list[str] = []
        metrics: dict[str, float] = {}
        unavailable = False
        async with self.probing:
            start = time.monotonic()
            try:
                code, _ = await self._get(config["health_url"])
                if not 200 <= code < 300:
                    unavailable = True
                    reasons.append(f"Health endpoint returned HTTP {code}")
            except (httpx.HTTPError, ValueError):
                unavailable = True
                reasons.append("Health endpoint could not be checked within the configured limits")
            latency = round((time.monotonic() - start) * 1000, 2)
            if latency > config["max_latency_ms"]:
                reasons.append(f"Health latency exceeds {config['max_latency_ms']} ms")
            if config.get("metrics_url"):
                try:
                    code, metric_text = await self._get(config["metrics_url"])
                    if 200 <= code < 300:
                        metrics = prometheus_metrics(metric_text)
                    else:
                        reasons.append(f"Metrics endpoint returned HTTP {code}")
                except (httpx.HTTPError, ValueError):
                    reasons.append("Metrics endpoint could not be checked within the configured limits")
            for threshold in config["metric_thresholds"]:
                value = metrics.get(threshold["name"])
                if value is None:
                    reasons.append(f"Metric {threshold['name']} is missing")
                elif (threshold["operator"] == "gt" and value > threshold["value"]) or (
                    threshold["operator"] == "lt" and value < threshold["value"]
                ):
                    reasons.append(f"Metric {threshold['name']} crossed its configured threshold")
        sample = {
            "id": uuid.uuid4().hex,
            "application_id": app_id,
            "configuration_revision": config["revision"],
            "status": "unavailable" if unavailable else "degraded" if reasons else "healthy",
            "latency_ms": latency,
            "metrics": metrics,
            "reasons": reasons,
            "created_at": now(),
        }
        async with self.store._unit() as session:
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": self.store.schema + ":application:" + app_id},
            )
            await self.observations.save(
                ForgeObservationEntity(id=identifier(sample["id"]), application_id=app_id, data=sample)
            )
            incident = await self.incident_records.open_for(app_id)
            if sample["status"] != "healthy":
                data = {
                    "id": incident.id.hex if incident else uuid.uuid4().hex,
                    "application_id": app_id,
                    "repository_id": config["repository_id"],
                    "target_id": config["target_id"],
                    "status": "open",
                    "summary": "; ".join(reasons),
                    "latest_observation_id": sample["id"],
                    "created_at": incident.data["created_at"] if incident else now(),
                    "updated_at": now(),
                    "maintenance_run_ids": incident.data.get("maintenance_run_ids", []) if incident else [],
                }
                if incident:
                    incident.data = data
                else:
                    incident = ForgeIncidentEntity(
                        id=identifier(data["id"]), application_id=app_id, status="open", data=data
                    )
                await self.incident_records.save(incident)
        return sample

    async def incidents(self) -> list[dict[str, Any]]:
        async with self.store._unit():
            return [copy.deepcopy(row.data) for row in await self.incident_records.latest()]

    async def maintenance(self, incident_id: str, request: MaintenanceRequest) -> dict[str, Any]:
        async with self.store._unit():
            incident = await self.incident_records.find_by_id(identifier(incident_id), lock=LockMode.PESSIMISTIC_WRITE)
            if not incident:
                raise KeyError("Incident not found")
            if incident.status != "open":
                raise ValueError("Maintenance requires an open incident")
            app = self._configuration()[incident.application_id]
            run = await self.store.create_run(
                RunInput(
                    repository_id=incident.data["repository_id"],
                    brief=(
                        f"Application: {app['name']}\nIncident: {incident.data['summary']}\n"
                        f"Requested maintenance: {request.brief}"
                    ),
                    agent=request.agent,
                    backend=app.get("backend", "pyfly"),
                    frontend=app.get("frontend", "react"),
                    operation="maintain",
                    commands=request.commands,
                    publish=False,
                )
            )
            incident.data = {
                **incident.data,
                "maintenance_run_ids": [*incident.data["maintenance_run_ids"], run["id"]],
                "updated_at": now(),
            }
            await self.incident_records.save(incident)
        return run

    async def resolve(self, incident_id: str, resolution: str) -> dict[str, Any]:
        IncidentResolution(resolution=resolution)
        async with self.store._unit() as session:
            incident = await self.incident_records.find_by_id(identifier(incident_id))
            if not incident:
                raise KeyError("Incident not found")
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": self.store.schema + ":application:" + incident.application_id},
            )
            incident = await self.incident_records.find_by_id(identifier(incident_id), lock=LockMode.PESSIMISTIC_WRITE)
            assert incident is not None
            app = await self.application(incident.application_id)
            if incident.status != "open" or app["stale"] or not app["latest"] or app["latest"]["status"] != "healthy":
                raise ValueError("Resolve an open incident only after a recent healthy check")
            incident.status = "resolved"
            incident.data = {
                **incident.data,
                "status": "resolved",
                "resolution": resolution,
                "resolved_at": now(),
                "updated_at": now(),
            }
            await self.incident_records.save(incident)
            return copy.deepcopy(incident.data)

    async def start(self) -> None:
        if self.store.runtime_roles and not self.store.runtime_roles.executes_jobs:
            return
        if self.poller is None or self.poller.done():
            self.poller = asyncio.create_task(self._poll())

    async def stop(self) -> None:
        if self.poller:
            self.poller.cancel()
            await asyncio.gather(self.poller, return_exceptions=True)

    async def _poll(self) -> None:
        while True:
            await self.store.assert_ownership()
            due = []
            for app in await self.applications():
                latest = app["latest"]
                if (
                    not latest
                    or time.time() - datetime.fromisoformat(latest["created_at"]).timestamp() >= app["interval_seconds"]
                ):
                    due.append(app["id"])
            for index in range(0, len(due), 4):
                await self.store.assert_ownership()
                await asyncio.gather(*(self.check(app_id) for app_id in due[index : index + 4]))
            await asyncio.sleep(5)
