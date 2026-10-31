# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Source-scope admission contracts, stopped before persistence or inspection."""

import copy
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from firefly_forge.project_models import ProjectDefinition
from firefly_forge.reverse_engineering import AnalysisInput, ReverseEngineering


class ScopeCaptured(Exception):
    def __init__(self, data):
        self.data = copy.deepcopy(data)


def admission(tmp_path, count):
    definition = ProjectDefinition.model_validate(
        {
            "name": "Inventory",
            "description": "Inspect the selected inventory services",
            "topology": "monorepo",
            "owners": ["Inventory team"],
            "components": [
                {
                    "id": f"component-{index}",
                    "name": f"Inventory service {index}",
                    "repository_id": "a" * 32,
                    "path": f"services/component-{index}",
                    "responsibility": "Own this inventory service",
                }
                for index in range(count)
            ],
        }
    )
    project = {
        "id": "b" * 32,
        "revision": 3,
        "digest": "d" * 64,
        "definition": definition.model_dump(),
    }
    effects = []

    async def current(project_id):
        assert project_id == project["id"]
        return copy.deepcopy(project)

    @asynccontextmanager
    async def unit():
        effects.append("persistence-unit")
        yield

    async def capture(row):
        raise ScopeCaptured(row.data)

    manager = ReverseEngineering(
        SimpleNamespace(_unit=unit),
        SimpleNamespace(project=current),
        tmp_path / "analysis",
        records=SimpleNamespace(save=capture),
    )
    return manager, project, effects


@pytest.mark.parametrize("count", [51, 60, 5000])
def test_source_scope_dto_accepts_every_requested_component_without_truncation(count):
    ids = [f"component-{index}" for index in range(count)]
    value = AnalysisInput.model_validate_json(json.dumps({"component_ids": ids}))
    assert value.component_ids == ids
    assert AnalysisInput.model_validate_json(value.model_dump_json()).component_ids == ids


def test_source_scope_still_rejects_unrecognized_fields():
    with pytest.raises(ValidationError, match="extra_forbidden"):
        AnalysisInput.model_validate({"component_ids": [], "unrecognized": True})


def test_legacy_scope_payload_omits_unused_snapshot_guards():
    assert AnalysisInput(component_ids=["stock"]).model_dump() == {"component_ids": ["stock"]}


@pytest.mark.parametrize("count", [51, 60, 5000])
async def test_scope_admission_keeps_project_order_and_unique_ids_before_inspection(tmp_path, count):
    manager, project, effects = admission(tmp_path, count)
    ids = [f"component-{index}" for index in range(count)]
    request = AnalysisInput(component_ids=[*reversed(ids), ids[-1], ids[0]])
    with pytest.raises(ScopeCaptured) as captured:
        await manager.analyze(project["id"], request)
    assert captured.value.data["component_ids"] == ids
    assert captured.value.data["project_snapshot"] == project
    assert captured.value.data["status"] == "analyzing"
    assert effects == ["persistence-unit"]
    assert not manager.directory.exists() and not manager.active


@pytest.mark.parametrize(
    "expectations",
    [
        {},
        {"expected_project_revision": 3},
        {"expected_project_digest": "d" * 64},
        {"expected_project_revision": 3, "expected_project_digest": "d" * 64},
    ],
)
async def test_empty_scope_keeps_all_components_with_optional_matching_snapshot_guards(tmp_path, expectations):
    manager, project, _ = admission(tmp_path, 3)
    with pytest.raises(ScopeCaptured) as captured:
        await manager.analyze(project["id"], AnalysisInput(**expectations))
    assert captured.value.data["component_ids"] == ["component-0", "component-1", "component-2"]
    assert not manager.directory.exists() and not manager.active


@pytest.mark.parametrize(
    "expectations",
    [
        {"expected_project_revision": 2},
        {"expected_project_digest": "e" * 64},
        {"expected_project_revision": 2, "expected_project_digest": "d" * 64},
        {"expected_project_revision": 3, "expected_project_digest": "e" * 64},
    ],
)
async def test_snapshot_drift_rejects_before_membership_or_any_inspection_effect(tmp_path, expectations):
    manager, project, effects = admission(tmp_path, 3)
    with pytest.raises(ValueError, match="Project changed; review component scope again"):
        await manager.analyze(project["id"], AnalysisInput(component_ids=["unknown"], **expectations))
    assert effects == []
    assert not manager.directory.exists() and not manager.active


async def test_unknown_component_rejects_before_persistence_or_inspection(tmp_path):
    manager, project, effects = admission(tmp_path, 3)
    with pytest.raises(ValueError, match="Choose components from this product project"):
        await manager.analyze(project["id"], AnalysisInput(component_ids=["component-0", "unknown"]))
    assert effects == []
    assert not manager.directory.exists() and not manager.active


@pytest.mark.parametrize(
    "expectations",
    [
        {"expected_project_revision": 0},
        {"expected_project_revision": -1},
        {"expected_project_digest": "short"},
        {"expected_project_digest": "D" * 64},
    ],
)
def test_snapshot_guards_validate_the_declared_revision_and_digest_contract(expectations):
    with pytest.raises(ValidationError):
        AnalysisInput(**expectations)


async def test_native_source_route_publishes_scope_schema_and_returns_snapshot_conflict(tmp_path):
    import httpx
    from pyfly.container import Container
    from pyfly.web.adapters.starlette.controller import ControllerRegistrar
    from pyfly.web.openapi import OpenAPIGenerator
    from starlette.applications import Starlette

    from firefly_forge.analysis_controllers import AnalysisController
    from firefly_forge.controllers import OperatorErrors

    manager, project, effects = admission(tmp_path, 60)
    container = Container()
    container.register_instance(AnalysisController, AnalysisController(manager, None))
    container.register_instance(OperatorErrors, OperatorErrors())
    context = SimpleNamespace(container=container, get_bean=container.resolve)
    registrar = ControllerRegistrar()
    metadata = registrar.collect_route_metadata(context)
    document = OpenAPIGenerator(title="Source scope fixture", version="test").generate(metadata)
    schema = document["components"]["schemas"]["AnalysisInput"]
    assert schema["additionalProperties"] is False
    assert "maxItems" not in schema["properties"]["component_ids"]
    assert schema["properties"]["expected_project_revision"]["anyOf"][0]["minimum"] == 1
    assert schema["properties"]["expected_project_digest"]["anyOf"][0]["pattern"] == "^[a-f0-9]{64}$"
    assert not schema.get("required")
    app = Starlette(routes=registrar.collect_routes(context))
    async with httpx.ASGITransport(app=app) as transport:
        response = await transport.handle_async_request(
            httpx.Request(
                "POST",
                f"http://fixture.invalid/api/v1/projects/{project['id']}/analyses",
                json={
                    "component_ids": [f"component-{index}" for index in range(60)],
                    "expected_project_revision": 2,
                    "expected_project_digest": project["digest"],
                },
            )
        )
        await response.aread()
    assert response.status_code == 409
    assert response.json() == {"detail": "Project changed; review component scope again"}
    assert effects == [] and not manager.directory.exists() and not manager.active
