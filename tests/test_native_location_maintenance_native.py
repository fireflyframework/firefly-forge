# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""A fictional stopped factory transfers its existing native topology without row rewrites."""

import asyncio
import hashlib
import os
import shutil
from pathlib import Path
from uuid import uuid4

import pytest

from firefly_forge.native_location_maintenance import (
    NativeLocationMaintenance,
    native_location_rows,
    preserved_native_digest,
)
from firefly_forge.native_location_models import (
    NativeLocationAcknowledgmentV1,
    NativeLocationFilesV1,
    NativeLocationPlanV1,
)
from firefly_forge.runtime_role_persistence import ForgeRuntimeTopologyRepository
from firefly_forge.runtime_roles import TOPOLOGY_ID, RuntimeRoles
from firefly_forge.source_catalog import SourceCatalogBudget


async def prepared_case(tmp_path, store_factory):
    postgres = os.environ.get("FORGE_TEST_POSTGRES_DATA")
    if not postgres:
        pytest.fail("Set FORGE_TEST_POSTGRES_DATA to the exact owned PostgreSQL 17 test directory")
    source, target = tmp_path / "old", tmp_path / "new"
    old = await store_factory(source / "factory.db")
    for name in ("providers.json", "source-budget-policy.json"):
        path = source / "configuration" / name
        path.write_bytes(b"{}")
        path.chmod(0o600)
    await RuntimeRoles(old).start()
    shutil.copytree(source, target)
    new = await store_factory(target / "factory.db")
    plan = NativeLocationPlanV1.model_validate(
        {
            "version": 1,
            "source_storage": str(source),
            "target_storage": str(target),
            "workspaces": [],
            "verification_runtimes": [],
        }
    )
    files = NativeLocationFilesV1.model_validate(
        {
            "version": 1,
            "required_files": [
                {"path": name, "sha256": hashlib.sha256((target / name).read_bytes()).hexdigest()}
                for name in (
                    "runtime-storage.identity",
                    "configuration/providers.json",
                    "configuration/source-budget-policy.json",
                )
            ],
            "unavailable_files": [{"path": "old/retained-unavailable.json", "sha256": None}],
        }
    )
    service = NativeLocationMaintenance(new)
    prepared = await service.prepare(
        plan, files, Path(postgres), tmp_path / "scratch", SourceCatalogBudget(min_free_bytes=0)
    )
    return old, new, service, prepared


def acknowledge(prepared):
    return NativeLocationAcknowledgmentV1(
        version=1,
        request_id=uuid4().hex,
        candidate_digest=prepared.candidate.digest,
        acknowledge_stopped_runtime=True,
        acknowledge_history_preserved=True,
        acknowledge_no_execution_grant=True,
    )


async def test_native_transfer_one_leaf_preserves_old_rows_and_same_request_result(tmp_path, store_factory):
    old, new, service, prepared = await prepared_case(tmp_path, store_factory)
    records = ForgeRuntimeTopologyRepository()
    async with old._unit() as session:
        before = await preserved_native_digest(session, old.schema)
        base = (await records.find_by_id(TOPOLOGY_ID)).data.copy()
    body = acknowledge(prepared)
    results = await asyncio.gather(*(service.commit(prepared, body) for _ in range(3)))
    assert results[0] == results[1] == results[2]
    async with new._unit() as session:
        assert await preserved_native_digest(session, new.schema) == before
        assert (await records.find_by_id(TOPOLOGY_ID)).data == base
        assert len(native_location_rows(await records.native_locations())) == 2
    admitted = RuntimeRoles(new)
    await admitted.start()
    assert admitted.started
    with pytest.raises(RuntimeError, match="leaf"):
        await RuntimeRoles(old).start()
    with pytest.raises(ValueError, match="candidate changed"):
        await service.commit(prepared, acknowledge(prepared))
    assert not (new.path.parent / "old/retained-unavailable.json").exists()


async def test_live_execution_owner_refuses_offline_transfer_before_any_native_append(tmp_path, store_factory):
    old, new, service, prepared = await prepared_case(tmp_path, store_factory)
    await old.acquire_ownership()
    try:
        with pytest.raises(ValueError, match="execution owner"):
            await service.commit(prepared, acknowledge(prepared))
    finally:
        await old.release_ownership()
    async with new._unit():
        assert await ForgeRuntimeTopologyRepository().native_locations() == []


async def test_post_append_filesystem_fault_rolls_back_native_pair(tmp_path, store_factory, monkeypatch):
    old, new, service, prepared = await prepared_case(tmp_path, store_factory)
    original = service.records.save
    calls = 0

    async def fault(value):
        nonlocal calls
        result = await original(value)
        calls += 1
        if calls == 2:
            (new.path.parent / "configuration/providers.json").write_bytes(b'{"changed":true}')
        return result

    monkeypatch.setattr(service.records, "save", fault)
    with pytest.raises(ValueError, match="changed"):
        await service.commit(prepared, acknowledge(prepared))
    async with old._unit():
        assert await ForgeRuntimeTopologyRepository().native_locations() == []
    await RuntimeRoles(old).start()
