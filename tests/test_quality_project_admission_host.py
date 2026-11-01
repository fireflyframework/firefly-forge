# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Real project configuration and run admission preserve required quality policies."""

import copy
from types import SimpleNamespace
from uuid import UUID

import pytest
from test_run_admission_prepared_host import PROJECT, REPOSITORY
from test_run_admission_prepared_host import admission as admission

from firefly_forge.identity import Principal
from firefly_forge.models import RunInput
from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied, ProjectAccess
from firefly_forge.project_models import ProjectDefinition
from firefly_forge.project_snapshot import PreparedProjectRevision
from firefly_forge.projects import Projects


def definition(**extra):
    return ProjectDefinition.model_validate(
        {
            "name": "Quality fixture",
            "description": "Required component checks",
            "topology": "monorepo",
            "owners": ["Owner"],
            "components": [
                {
                    "id": "api",
                    "name": "API",
                    "repository_id": REPOSITORY,
                    "path": "apps/api",
                    "backend": "pyfly",
                    "frontend": "none",
                    "responsibility": "Typed API",
                }
            ],
            **extra,
        }
    )


def manager(admission, monkeypatch):
    store, state, _, _ = admission
    state["project_rows"] = {}

    async def find(value, **kwargs):
        return state["project_rows"].get(value.hex)

    async def save(row):
        state["project_rows"][row.id.hex] = row

    async def revision(row):
        pass

    async def component(service, request):
        return PreparedProjectRevision.capture(state["project_rows"][request.project_id].data).component(request)

    async def identity(records, value, **kwargs):
        return SimpleNamespace(id=value, enabled=True, issuer="fixture-issuer", subject="fixture-person")

    monkeypatch.setattr("firefly_forge.projects.uuid", SimpleNamespace(uuid4=lambda: UUID(PROJECT)))
    monkeypatch.setattr(Projects, "component_snapshot", component)
    monkeypatch.setattr("firefly_forge.identity_persistence.ForgeIdentityUserRepository.find_by_id", identity)
    return Projects(
        store, records=SimpleNamespace(find_by_id=find, save=save), revisions=SimpleNamespace(save=revision)
    )


async def test_new_project_baseline_survives_an_old_client_edit_and_empty_run_skills(admission, monkeypatch):
    service = manager(admission, monkeypatch)
    first = await service.configure(definition())
    policy = first["definition"]["quality_policy"]
    assert policy["baseline"] == "builtin-v1"
    updated = definition(name="Renamed without knowing the new field")
    assert "quality_policy" not in updated.model_fields_set
    second = await service.configure(updated, first["id"])
    assert second["definition"]["quality_policy"] == policy
    request = RunInput(
        repository_id=REPOSITORY, project_id=PROJECT, component_id="api", brief="Change this API", skills=[]
    )
    run = await admission[0].create_run(request)
    assert run["component_snapshot"]["path"] == "apps/api"
    assert run["policy_snapshot"]["project_quality_policy"] == policy
    assert {s["id"] for s in run["policy_snapshot"]["skills"]} == {
        "quality-python-lint-v1",
        "quality-python-format-v1",
        "quality-python-types-v1",
    }
    assert all(
        c["quality"]["tool"]["package"] in {"ruff", "mypy"}
        for s in run["policy_snapshot"]["skills"]
        for c in s["commands"]
    )


async def test_legacy_projects_without_quality_policy_are_not_silently_repolicied(admission, monkeypatch):
    service = manager(admission, monkeypatch)
    first = await service.configure(definition())
    row = admission[1]["project_rows"][PROJECT]
    row.data["definition"].pop("quality_policy")
    result = await service.configure(definition(name="Ordinary legacy edit"), first["id"])
    assert "quality_policy" not in result["definition"]


async def test_quality_changes_need_members_authority_in_addition_to_ordinary_edit(admission, monkeypatch):
    service = manager(admission, monkeypatch)
    first = await service.configure(definition())
    before = copy.deepcopy(first)
    actor = Principal(UUID("c" * 32), "fixture-issuer", "fixture-person", authentication_method="oidc_session")
    calls = []

    async def no_admin(self, user):
        return False

    async def denied(self, user, project, permission):
        calls.append((project, permission))
        raise AccessDenied("The fixture person may edit source but cannot waive required policy")

    monkeypatch.setattr(ProjectAccess, "current_administrator", no_admin)
    monkeypatch.setattr(ProjectAccess, "require_project", denied)
    token = CURRENT_PRINCIPAL.set(actor)
    try:
        with pytest.raises(AccessDenied):
            await service.configure(definition(quality_policy={"version": 1, "baseline": None}), first["id"])
    finally:
        CURRENT_PRINCIPAL.reset(token)
    assert calls == [(first["id"], "members")]
    assert admission[1]["project_rows"][PROJECT].data == before
