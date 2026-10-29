# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Compact component snapshots bound to an existing immutable project revision."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from firefly_forge.models import RunInput
from firefly_forge.project_models import ProjectDefinition

Data = dict[str, Any]


@dataclass(frozen=True, slots=True)
class PreparedProjectRevision:
    metadata: bytes
    components: Mapping[str, bytes]

    def __post_init__(self) -> None:
        if type(self.metadata) is not bytes or any(type(value) is not bytes for value in self.components.values()):
            raise ValueError("Prepared project revisions require immutable bytes")
        object.__setattr__(self, "components", MappingProxyType(dict(self.components)))

    @classmethod
    def capture(cls, project: Data) -> PreparedProjectRevision:
        definition = project.get("definition")
        if (
            not isinstance(definition, dict)
            or type(project.get("revision")) is not int
            or project["revision"] < 1
            or not isinstance(project.get("id"), str)
            or re.fullmatch(r"[a-f0-9]{32}", project["id"]) is None
        ):
            raise ValueError("Project snapshot requires an immutable project revision")
        model = ProjectDefinition.model_validate(definition)
        expected = hashlib.sha256(json.dumps(definition, sort_keys=True).encode()).hexdigest()
        if project.get("digest") != expected:
            raise ValueError("Project revision content does not match its digest")
        reference = project_revision_reference(project)
        metadata = {
            "project_id": project["id"],
            "project_name": model.name,
            "project_revision": project["revision"],
            "project_digest": expected,
            "topology": model.topology,
            "product_repository_id": model.product_repository_id,
            "project_revision_ref": reference,
            "project_role_selection": model.roles.model_dump() if model.roles else None,
            **(
                {"project_factory_team": model.factory_team.model_dump(mode="json")}
                if model.factory_team is not None
                else {}
            ),
        }
        return cls(
            json.dumps(metadata, separators=(",", ":"), ensure_ascii=False).encode(),
            MappingProxyType({component.id: component.model_dump_json().encode() for component in model.components}),
        )

    def selected(self, component_id: str) -> Data:
        encoded = self.components.get(component_id)
        if encoded is None:
            raise ValueError("The selected component does not belong to this project revision")
        return {**json.loads(self.metadata), **json.loads(encoded)}

    def component(self, request: RunInput) -> Data:
        if not request.component_id:
            raise ValueError("The project snapshot requires a selected component")
        snapshot = self.selected(request.component_id)
        if snapshot["project_id"] != request.project_id or snapshot["repository_id"] != request.repository_id:
            raise ValueError("The run's repository must belong to the selected product component")
        for key in ("backend", "frontend"):
            if key in request.model_fields_set and getattr(request, key) != snapshot[key]:
                raise ValueError("The run's stack must match its configured component")
        return snapshot


def project_revision_reference(project: Data) -> Data:
    return {
        "kind": "project_revision_reference",
        "version": 1,
        "project_id": project["id"],
        "revision": project["revision"],
        "digest": project["digest"],
    }


def prepared_revision(project: Data, historical: Data | None) -> PreparedProjectRevision:
    if (
        historical is None
        or historical.get("id") != project.get("id")
        or type(historical.get("revision")) is not int
        or historical["revision"] != project.get("revision")
        or historical.get("digest") != project.get("digest")
        or json.dumps(historical.get("definition"), sort_keys=True)
        != json.dumps(project.get("definition"), sort_keys=True)
    ):
        raise ValueError("Project snapshot requires its exact persisted immutable revision")
    return PreparedProjectRevision.capture(historical)


def snapshot_definition(
    component: Data,
    project: Data,
    *,
    project_id: str | None = None,
    component_id: str | None = None,
    repository_id: str | None = None,
) -> Data:
    """Resolve either shape against the exact historical row, never current defaults."""
    prepared = PreparedProjectRevision.capture(project)
    if (
        component.get("project_id", project["id"]) != project["id"]
        or type(component.get("project_revision")) is not int
        or component["project_revision"] != project["revision"]
        or component.get("project_digest") != project["digest"]
    ):
        raise ValueError("Component snapshot does not match its immutable project revision")
    if project_id is not None and project["id"] != project_id:
        raise ValueError("Project snapshot belongs to another execution project")
    if component_id is not None:
        selected = prepared.selected(component_id)
        if (
            ("id" in component and component["id"] != component_id)
            or (repository_id is not None and selected["repository_id"] != repository_id)
            or ("repository_id" in component and component["repository_id"] != selected["repository_id"])
        ):
            raise ValueError("Project snapshot belongs to another execution component or repository")
    if "project_revision_ref" in component:
        selected_id = component.get("id")
        if not isinstance(selected_id, str) or json.dumps(component, sort_keys=True) != json.dumps(
            prepared.selected(selected_id), sort_keys=True
        ):
            raise ValueError("Component reference or frozen execution contract changed")
    elif json.dumps(component.get("project_definition"), sort_keys=True) != json.dumps(
        project["definition"], sort_keys=True
    ):
        raise ValueError("Legacy component graph does not match its immutable project revision")
    return copy.deepcopy(project["definition"])


def snapshot_roles(component: Data | None) -> Data | None:
    if component is None:
        return None
    if "project_revision_ref" in component:
        value = component.get("project_role_selection")
    else:
        value = (component.get("project_definition") or {}).get("roles")
    if value is not None and not isinstance(value, dict):
        raise ValueError("Project role selection requires its frozen object")
    return copy.deepcopy(value)
