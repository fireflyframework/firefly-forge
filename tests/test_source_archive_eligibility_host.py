# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Standalone runs are ineligible for project-scoped source archival, never remapped."""

import copy
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID

import pytest
from test_source_archive_publisher_host import fixture

from firefly_forge.object_storage import ObjectStorageSettings
from firefly_forge.source_archive_service import SourceArchiveManager, SourceArchivePolicy


def manager_case(tmp_path, monkeypatch):
    import firefly_forge.source_archive_service as module

    publisher, workspace, run, records = fixture(tmp_path)
    run["execution_actor"] = {"user_id": UUID(int=7).hex}
    checks = []

    async def current():
        checks.append("current")

    async def get(identifier):
        assert identifier == run["id"]
        return copy.deepcopy(run)

    async def campaign(value):
        assert value == run

    async def scalar(*args, **kwargs):
        return datetime(2026, 10, 30, tzinfo=UTC)

    @asynccontextmanager
    async def unit():
        checks.append("native-unit")
        yield SimpleNamespace(scalar=scalar)

    class Access:
        def __init__(self, store):
            pass

        async def authorize_execution(self, value):
            assert value["execution_actor"] == run["execution_actor"]
            checks.append("project-authority")

    monkeypatch.setattr(module, "ProjectAccess", Access)
    manager = object.__new__(SourceArchiveManager)
    manager.settings = publisher.content.helper.objects.settings
    manager.policy = SourceArchivePolicy()
    manager.publisher = publisher
    manager.store = SimpleNamespace(
        execution_ownership_token=object(),
        assert_ownership=current,
        get_run=get,
        assert_campaign_current=campaign,
        _unit=unit,
        _ownership_pid=123,
        schema="controlled_source",
    )
    return manager, workspace, run, records, checks, current


async def test_configured_archiving_skips_only_explicit_standalone_before_authority_or_artifacts(tmp_path, monkeypatch):
    manager, workspace, run, records, checks, current = manager_case(tmp_path, monkeypatch)
    run["project_id"] = None
    assert manager.settings.provider == "local" and manager.policy.enabled
    manager.publisher = None  # Ineligible runs do not even require an initialized publisher.
    assert await manager.publish(run, workspace, "a" * 40, "b" * 40, assert_current=current) is None
    assert checks == [] and records.rows == {} and records.parts == {}
    assert not list(manager.settings.local_directory.rglob("v1"))


@pytest.mark.parametrize("project", ["", False, 0, "not-a-project", "A" * 32, str(UUID(int=2)), UUID(int=2)])
async def test_present_noncanonical_project_is_refused_before_provider_or_scope_effects(tmp_path, monkeypatch, project):
    manager, workspace, run, records, checks, current = manager_case(tmp_path, monkeypatch)
    run["project_id"] = project
    with pytest.raises(ValueError):
        await manager.publish(run, workspace, "a" * 40, "b" * 40, assert_current=current)
    assert checks == [] and records.rows == {} and records.parts == {}
    assert not list(manager.settings.local_directory.rglob("v1"))


@pytest.mark.parametrize("gate", ["provider", "policy", "excluded-project"])
async def test_existing_disabled_and_excluded_project_gates_remain_effect_free(tmp_path, monkeypatch, gate):
    manager, workspace, run, records, checks, current = manager_case(tmp_path, monkeypatch)
    if gate == "provider":
        manager.settings = ObjectStorageSettings()
    elif gate == "policy":
        manager.policy = SourceArchivePolicy(enabled=False)
    else:
        manager.policy = SourceArchivePolicy(excluded_projects=frozenset({UUID(run["project_id"])}))
    assert await manager.publish(run, workspace, "a" * 40, "b" * 40, assert_current=current) is None
    assert checks == [] and records.rows == {} and records.parts == {}


async def test_canonical_project_still_publishes_real_catalog_and_local_native_objects(tmp_path, monkeypatch):
    manager, workspace, run, records, checks, current = manager_case(tmp_path, monkeypatch)
    result = await manager.publish(run, workspace, "a" * 40, "b" * 40, assert_current=current)
    assert result.project_id == run["project_id"] and result.catalog.file_count == 2
    assert records.rows[result.snapshot_id].data["status"] == "published" and len(records.parts) == 2
    assert "project-authority" in checks and "native-unit" in checks
    assert list(manager.settings.local_directory.rglob("v1"))
