# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Only a persisted project revision supplies optional automatic-team approval."""

import hashlib
import json

from firefly_forge.project_models import ProjectDefinition
from firefly_forge.project_snapshot import PreparedProjectRevision


def definition():
    return {
        "name": "Fictional API",
        "description": "Frozen project",
        "topology": "monorepo",
        "owners": ["Team"],
        "components": [{"id": "api", "name": "API", "repository_id": "a" * 32, "responsibility": "Approved API"}],
    }


def snapshot(value):
    model = ProjectDefinition.model_validate(value)
    encoded = model.model_dump(mode="json")
    project = {
        "id": "b" * 32,
        "revision": 1,
        "definition": encoded,
        "digest": hashlib.sha256(json.dumps(encoded, sort_keys=True).encode()).hexdigest(),
    }
    return encoded, PreparedProjectRevision.capture(project).selected("api")


def test_legacy_project_bytes_and_component_shape_do_not_acquire_team_authority():
    encoded, component = snapshot(definition())
    assert "factory_team" not in encoded
    assert "project_factory_team" not in component


def test_nullable_or_large_team_policy_is_preserved_in_immutable_component_revision():
    value = definition()
    value["factory_team"] = {
        "protocol": "forge-factory-team-v1",
        "automatic": True,
        "budget": {"requests": None, "tool_calls": 2**40, "commands": None, "broker_calls": 30},
        "broker_per_tool_limit": 10,
    }
    encoded, component = snapshot(value)
    assert encoded["factory_team"] == value["factory_team"]
    assert component["project_factory_team"] == value["factory_team"]
    assert component["project_revision"] == 1
