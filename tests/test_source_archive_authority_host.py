# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""The last asynchronous workspace check cannot carry a stale actor or owner."""

import copy
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID

import pytest

from firefly_forge.object_storage import ObjectStorageSettings
from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied
from firefly_forge.source_archive_service import SourceArchiveManager, SourceArchivePolicy


@pytest.mark.parametrize("drift", ["actor", "scope", "owner"])
async def test_last_async_workspace_check_cannot_admit_revoked_authority(tmp_path, monkeypatch, drift):
    import firefly_forge.source_archive_service as module

    state = SimpleNamespace(valid=True, checks=0, effects=[])
    owner = object()
    run = dict(
        id=UUID(int=1).hex,
        project_id=UUID(int=2).hex,
        repository_id=UUID(int=3).hex,
        attempt=1,
        execution_actor={"user_id": UUID(int=4).hex},
        branch="recorded/topic",
    )

    async def check():
        return None

    async def get(_):
        return copy.deepcopy(run)

    async def campaign(_):
        return None

    async def scalar(*args, **kwargs):
        return datetime(2026, 10, 30, tzinfo=UTC)

    @asynccontextmanager
    async def unit():
        yield SimpleNamespace(scalar=scalar)

    store = SimpleNamespace(
        assert_ownership=check,
        get_run=get,
        assert_campaign_current=campaign,
        _unit=unit,
        execution_ownership_token=owner,
        _ownership_pid=123,
        schema="controlled_source",
    )

    class Access:
        def __init__(self, store):
            pass

        async def authorize_execution(self, value):
            if not state.valid:
                raise AccessDenied("canonical actor revoked")

    monkeypatch.setattr(module, "ProjectAccess", Access)

    async def workspace_current():
        state.checks += 1
        if state.checks == 2:
            if drift == "actor":
                state.valid = False
            elif drift == "scope":
                run["project_id"] = UUID(int=9).hex
            else:
                store.execution_ownership_token = object()

    async def publish(*args, authorize, **kwargs):
        await authorize()
        state.effects.append("would write object")

    manager = object.__new__(SourceArchiveManager)
    manager.store = store
    manager.settings = SimpleNamespace(provider="local")
    manager.policy = SourceArchivePolicy()
    manager.publisher = SimpleNamespace(publish=publish)
    assert CURRENT_PRINCIPAL.get() is None
    with pytest.raises(AccessDenied):
        await manager.publish(copy.deepcopy(run), tmp_path, "a" * 40, "b" * 40, assert_current=workspace_current)
    assert state.effects == []


def test_native_configuration_defaults_to_recording_and_requires_explicit_safe_retention():
    assert SourceArchivePolicy.from_environment({}).enabled is True
    policy = SourceArchivePolicy.from_environment(
        {"FORGE_SOURCE_ARCHIVE_POLICY": '{"excluded_projects":["' + UUID(int=2).hex + '"]}'}
    )
    assert policy.excluded_projects == frozenset({UUID(int=2)})
    with pytest.raises(ValueError):
        SourceArchivePolicy(retention="delete_after_seven_days")


async def test_fresh_configured_source_index_serializes_ddl_before_native_creation(tmp_path, monkeypatch):
    import firefly_forge.source_archive_service as module

    calls = []

    async def execute(statement, parameters):
        calls.append(("sql", str(statement), parameters))

    async def sync(function):
        return function(object())

    async def connection():
        return SimpleNamespace(run_sync=sync)

    @asynccontextmanager
    async def unit():
        yield SimpleNamespace(execute=execute, connection=connection)

    class Pools:
        def __init__(self, *args, **kwargs):
            self._unit = unit

        async def initialize(self):
            return None

    monkeypatch.setattr(module, "RunnerPools", Pools)
    monkeypatch.setattr(
        module.ForgeSourceArchiveEntity.metadata, "create_all", lambda *args, **kwargs: calls.append(("ddl",))
    )
    manager = object.__new__(SourceArchiveManager)
    manager.settings = ObjectStorageSettings(
        provider="local", mode="test", local_directory=tmp_path / "objects", storage_id=UUID(int=8)
    )
    manager.tenant = UUID(int=1)
    manager.store = SimpleNamespace(
        path=tmp_path / "factory.db", schema="controlled_source", session_provider=object(), credential_registry=dict
    )
    await manager.start()
    assert calls[0][0] == "sql" and "pg_advisory_xact_lock" in calls[0][1] and calls[1] == ("ddl",)
