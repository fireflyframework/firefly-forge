# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Project updates preserve the native identity independently of UUID path spelling."""

import copy
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import UUID

import pytest
from pyfly.data.relational.sqlalchemy import LockMode

from firefly_forge.models import RunInput
from firefly_forge.project_models import ProjectDefinition
from firefly_forge.project_persistence import ForgeProjectEntity
from firefly_forge.project_snapshot import PreparedProjectRevision
from firefly_forge.projects import Projects

PROJECT, REPOSITORY = "abcdef0123456789abcdef0123456789", "b" * 32


@pytest.fixture
def service():
    definition = ProjectDefinition.model_validate(
        {
            "name": "Owned product",
            "description": "One explicitly configured component",
            "topology": "monorepo",
            "owners": ["Product owner"],
            "components": [
                {
                    "id": "api",
                    "name": "API",
                    "repository_id": REPOSITORY,
                    "path": ".",
                    "responsibility": "Own the product API",
                }
            ],
        }
    )

    @asynccontextmanager
    async def unit():
        yield None

    records = SimpleNamespace(find_by_id=AsyncMock(), save=AsyncMock())
    revisions = SimpleNamespace(save=AsyncMock(), revision=AsyncMock())
    store = SimpleNamespace(
        _unit=unit,
        roles=SimpleNamespace(snapshot=Mock()),
        policies=SimpleNamespace(selection=Mock(return_value={"retained": True})),
        admit_credentials=AsyncMock(),
        repository=AsyncMock(),
    )
    return Projects(store, records=records, revisions=revisions), definition


@pytest.mark.parametrize("spelling", ["hex", "hyphenated", "uppercase", "uppercase-hyphenated"])
async def test_update_uses_locked_row_identity_and_keeps_revision_consumable(service, spelling):
    manager, definition = service
    original = await manager.configure(definition)
    original.update(
        id=PROJECT,
        revision=4,
        git_delivery_policy={"enabled": False, "retained_grant": "delivery"},
        campaign_execution_policy={"enabled": False, "retained_grant": "campaign"},
    )
    historical = copy.deepcopy(original)
    row = ForgeProjectEntity(id=UUID(PROJECT), data=original)
    manager.records.find_by_id.return_value = row
    manager.records.save.reset_mock()
    manager.revisions.save.reset_mock()
    path_id = str(row.id) if "hyphenated" in spelling else row.id.hex
    if "uppercase" in spelling:
        path_id = path_id.upper()
    changed = definition.model_copy(update={"name": "Renamed product"})

    updated = await manager.configure(changed, path_id)

    manager.records.find_by_id.assert_awaited_once_with(UUID(PROJECT), lock=LockMode.PESSIMISTIC_WRITE)
    manager.records.save.assert_awaited_once_with(row)
    saved_revision = manager.revisions.save.await_args.args[0]
    manager.revisions.revision.return_value = saved_revision
    prepared = await manager.prepared_revision(updated)
    assert updated["id"] == row.id.hex == PROJECT
    assert row.data == saved_revision.data == updated
    assert saved_revision.project_id == row.id and saved_revision.revision == 5
    assert updated["revision"] == historical["revision"] + 1
    assert updated["created_at"] == historical["created_at"]
    assert updated["definition"] == changed.model_dump()
    for key in ("git_delivery_policy", "campaign_execution_policy"):
        assert updated[key] == historical[key]
        assert updated[key] is not historical[key]
    assert original == historical
    request = RunInput(
        repository_id=REPOSITORY, project_id=updated["id"], component_id="api", brief="Maintain the product"
    )
    assert prepared.component(request)["project_id"] == PROJECT
    updated["definition"]["name"] = "Caller mutation"
    assert row.data["definition"]["name"] == saved_revision.data["definition"]["name"] == "Renamed product"


async def test_new_project_keeps_canonical_generated_identity(service):
    manager, definition = service
    created = await manager.configure(definition)
    row = manager.records.save.await_args.args[0]
    assert created["id"] == row.id.hex
    assert created["revision"] == 1
    PreparedProjectRevision.capture(created)
    manager.records.find_by_id.assert_not_awaited()


async def test_malformed_present_project_identity_is_not_replaced(service):
    manager, definition = service
    with pytest.raises(KeyError, match="Record not found"):
        await manager.configure(definition, "not-a-project-uuid")
    manager.records.find_by_id.assert_not_awaited()
    manager.records.save.assert_not_awaited()
    manager.revisions.save.assert_not_awaited()
