# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
import httpx
import pytest

from firefly_forge.models import RepositoryInput


async def configured(store, tmp_path):
    from firefly_forge.application_operations import ApplicationInput, ApplicationOperations
    from firefly_forge.engine import Engine
    from firefly_forge.operations import DeploymentManager, DeploymentTarget

    repository = await store.add_repository(RepositoryInput(name="Inventory", url=str(tmp_path)))
    engine = Engine(store, tmp_path / "workspaces")
    deployments = DeploymentManager(store, engine)
    await deployments.configure_target(
        "staging",
        DeploymentTarget(
            name="Staging",
            repository_id=repository["id"],
            commands=[{"argv": ["true"]}],
            verification=[{"argv": ["true"]}],
        ),
    )
    operations = ApplicationOperations(store, engine, deployments)
    app = await operations.configure(
        "inventory",
        ApplicationInput(
            name="Inventory staging",
            backend="larafly",
            frontend="vue",
            repository_id=repository["id"],
            target_id="staging",
            health_url="http://127.0.0.1:9999/health",
            metrics_url="http://127.0.0.1:9999/metrics",
            metric_thresholds=[{"name": "inventory_errors_total", "operator": "gt", "value": 3}],
        ),
    )
    return operations, app


@pytest.mark.asyncio
async def test_actual_probe_metrics_and_deduplicated_incident(store, tmp_path):
    operations, app = await configured(store, tmp_path)
    assert app["latest"] is None and app["stale"] is True

    def response(request):
        return httpx.Response(200, text="inventory_errors_total 4\n" if request.url.path == "/metrics" else "ok")

    operations.transport = httpx.MockTransport(response)
    first = await operations.check("inventory")
    assert first["status"] == "degraded"
    assert first["metrics"]["inventory_errors_total"] == 4
    await operations.check("inventory")
    incidents = await operations.incidents()
    assert len(incidents) == 1 and incidents[0]["status"] == "open"
    with pytest.raises(ValueError, match="healthy"):
        await operations.resolve(incidents[0]["id"], "Fixed")
    operations.transport = httpx.MockTransport(lambda request: httpx.Response(200, text="inventory_errors_total 0\n"))
    await operations.check("inventory")
    resolved = await operations.resolve(incidents[0]["id"], "Verified recovery after corrected configuration")
    assert resolved["status"] == "resolved" and resolved["resolution"]
    detail = await operations.application("inventory")
    assert len(detail["observations"]) == 3 and detail["stale"] is False


@pytest.mark.asyncio
async def test_failed_health_and_missing_metrics_never_claim_healthy(store, tmp_path):
    operations, _ = await configured(store, tmp_path)
    operations.transport = httpx.MockTransport(lambda request: httpx.Response(503, text="unavailable"))
    result = await operations.check("inventory")
    assert result["status"] == "unavailable" and result["reasons"]
    operations.transport = httpx.MockTransport(lambda request: httpx.Response(200, text="# no series\n"))
    result = await operations.check("inventory")
    assert result["status"] == "degraded" and any("missing" in item for item in result["reasons"])


@pytest.mark.asyncio
async def test_cross_project_target_rejected_and_maintenance_requires_checks(store, tmp_path):
    from firefly_forge.application_operations import ApplicationInput, MaintenanceRequest

    operations, app = await configured(store, tmp_path)
    other = await store.add_repository(RepositoryInput(name="Other", url=str(tmp_path)))
    with pytest.raises(ValueError, match="project"):
        await operations.configure(
            "other",
            ApplicationInput(
                name="Other", repository_id=other["id"], target_id=app["target_id"], health_url=app["health_url"]
            ),
        )
    with pytest.raises(ValueError):
        MaintenanceRequest(brief="Fix incident", agent="codex", commands=[])
    operations.transport = httpx.MockTransport(lambda request: httpx.Response(503))
    await operations.check("inventory")
    incident = (await operations.incidents())[0]
    run = await operations.maintenance(
        incident["id"],
        MaintenanceRequest(
            brief="Restore service and add regression coverage", agent="codex", commands=[{"argv": ["true"]}]
        ),
    )
    assert run["repository_id"] == app["repository_id"] and run["operation"] == "maintain"
    assert run["publish"] is False
    assert run["backend"] == "larafly" and run["frontend"] == "vue"
    assert run["id"] in (await operations.incidents())[0]["maintenance_run_ids"]


def test_probe_configuration_rejects_credentials_and_nonfinite_thresholds():
    from firefly_forge.application_operations import ApplicationInput

    base = dict(name="App", repository_id="project", target_id="staging")
    for url in ("https://user:pass@example.org/health", "file:///private", "http://external.example/health"):
        with pytest.raises(ValueError):
            ApplicationInput(**base, health_url=url)
    with pytest.raises(ValueError):
        ApplicationInput(
            **base,
            health_url="https://example.org/health",
            metric_thresholds=[{"name": "errors", "operator": "gt", "value": float("nan")}],
        )


def test_observability_links_support_dashboard_filters_without_query_credentials():
    from firefly_forge.application_operations import ObservabilityLinks

    assert ObservabilityLinks(dashboard_url="https://metrics.example.org/d/inventory?orgId=1&from=now-1h").dashboard_url
    with pytest.raises(ValueError):
        ObservabilityLinks(logs_url="https://logs.example.org/?api_key=private-token")
