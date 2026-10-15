# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Native health and metrics agree without inferring a remote worker's readiness."""

import asyncio

import httpx
import pytest

from firefly_forge import __version__
from firefly_forge.api import create_application
from firefly_forge.application_controllers import ApplicationController
from firefly_forge.controllers import CatalogController
from firefly_forge.worker_runtime import WorkerSupervision

TOKEN = "owned-role-readiness-fixture-token"


@pytest.mark.parametrize("role", ["api", "combined"])
async def test_native_health_and_metrics_report_admission_and_local_execution(postgres_schema, tmp_path, role):
    app = create_application(tmp_path, TOKEN, role=role)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app),
            base_url="http://127.0.0.1",
            headers={"Authorization": f"Bearer {TOKEN}"},
        ) as client,
    ):
        context = app.state.runtime.context
        supervision = context.get_bean(WorkerSupervision)
        assert context.get_bean(ApplicationController).supervision is supervision
        assert context.get_bean(CatalogController).supervision is supervision
        assert supervision.engine is app.state.engine
        health = (await client.get("/api/v1/health")).json()
        assert health == {
            "status": "ok",
            "version": __version__,
            "runtime": "PyFly",
            "storage": "PostgreSQL",
            "detail": None,
            "service_role": role,
            "admission_ready": True,
            "execution_ready": role == "combined",
            "execution": {
                "local_enabled": role == "combined",
                "state": "ready" if role == "combined" else "api_only",
                "owner_scope": "postgres_schema",
                "single_owner": True,
                "max_parallel_jobs": app.state.engine.concurrency if role == "combined" else 0,
                "remote_worker_state": "unknown",
            },
        }
        metrics = await client.get("/api/v1/metrics")
        assert metrics.status_code == 200
        assert f"forge_execution_ready {int(health['execution_ready'])}\n" in metrics.text
        assert f"forge_admission_ready {int(health['admission_ready'])}\n" in metrics.text


async def test_native_metrics_keep_stopped_consumer_degradation(postgres_schema, tmp_path):
    app = create_application(tmp_path, TOKEN)
    async with app.router.lifespan_context(app):
        supervision = app.state.runtime.context.get_bean(WorkerSupervision)
        consumer = supervision.tasks()["deployments"]
        assert consumer is not None
        consumer.cancel()
        await asyncio.gather(consumer, return_exceptions=True)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app),
            base_url="http://127.0.0.1",
            headers={"Authorization": f"Bearer {TOKEN}"},
        ) as client:
            health = (await client.get("/api/v1/health")).json()
            assert health["status"] == "degraded"
            assert health["execution_ready"] is False
            assert "deployments" in health["detail"]
            metrics = await client.get("/api/v1/metrics")
            assert "forge_execution_ready 0\n" in metrics.text
            assert "forge_admission_ready 1\n" in metrics.text


async def test_native_api_diagnostic_remains_visible_and_metrics_stay_local(postgres_schema, tmp_path):
    app = create_application(tmp_path, TOKEN, role="api")
    async with app.router.lifespan_context(app):
        app.state.engine.ownership_failure = "Inspect retained fixture work"
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app),
            base_url="http://127.0.0.1",
            headers={"Authorization": f"Bearer {TOKEN}"},
        ) as client:
            health = (await client.get("/api/v1/health")).json()
            assert health["status"] == "degraded"
            assert health["detail"] == "Inspect retained fixture work"
            assert health["admission_ready"] is True
            assert health["execution_ready"] is False
            assert health["execution"]["remote_worker_state"] == "unknown"
            metrics = await client.get("/api/v1/metrics")
            assert "forge_execution_ready 0\n" in metrics.text
            assert "forge_admission_ready 1\n" in metrics.text
