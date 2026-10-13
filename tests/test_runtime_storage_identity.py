# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""A matching mount name cannot substitute for retained private storage identity."""

import asyncio
import hashlib
import os
from pathlib import Path

import pytest

from firefly_forge.runtime_role_persistence import ForgeRuntimeTopologyEntity
from firefly_forge.runtime_roles import TOPOLOGY_ID, RuntimeRoles
from firefly_forge.store import now


async def legacy_topology(store):
    data = {
        "revision": 1,
        "mode": "split",
        "storage_binding": hashlib.sha256(str(store.path.parent.resolve()).encode()).hexdigest(),
        "owner_uid": os.getuid(),
        "created_at": now(),
    }
    async with store._unit():
        await RuntimeRoles(store).records.save(ForgeRuntimeTopologyEntity(id=TOPOLOGY_ID, mode="split", data=data))
    return data


async def test_new_topology_binds_private_marker_and_shared_root_accepts_replicas(store):
    from firefly_forge.runtime_roles import STORAGE_IDENTITY_FILE

    first, second = RuntimeRoles(store, "api"), RuntimeRoles(store, "worker")
    await first.start()
    path = store.path.parent / STORAGE_IDENTITY_FILE
    marker = path.read_text()
    assert len(marker) == 64 and path.stat().st_mode & 0o777 == 0o600
    async with store._unit():
        row = await first.records.find_by_id(TOPOLOGY_ID)
        assert row.data["storage_identity"] == marker
    await second.start()
    assert path.read_text() == marker


async def test_independent_filesystems_with_matching_mount_path_and_uid_are_rejected(
    tmp_path, store_factory, monkeypatch
):
    first = await store_factory(tmp_path / "actual-root-a" / "factory.db")
    second = await store_factory(tmp_path / "actual-root-b" / "factory.db")
    resolve = Path.resolve

    def shared_name(path, *args, **kwargs):
        return Path("/srv/forge") if path in {first.path.parent, second.path.parent} else resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", shared_name)
    await RuntimeRoles(first, "api").start()
    with pytest.raises(RuntimeError, match="storage identity"):
        await RuntimeRoles(second, "worker").start()


async def test_legacy_adoption_is_serialized_and_keeps_original_topology_immutable(store, tmp_path, store_factory):
    from sqlalchemy import text

    from firefly_forge.runtime_roles import STORAGE_ATTESTATION_ID, STORAGE_IDENTITY_FILE

    original = await legacy_topology(store)
    async with store._unit() as session:
        original_bytes = await session.scalar(
            text("SELECT row_to_json(t)::text FROM forge_runtime_topology t WHERE id=:id"), {"id": TOPOLOGY_ID}
        )
    peer = await store_factory(store.path)
    first, second = RuntimeRoles(store, "api"), RuntimeRoles(peer, "worker")
    await asyncio.gather(first.start(), second.start())
    async with store._unit() as session:
        row = await first.records.find_by_id(TOPOLOGY_ID)
        assert row.data == original
        assert original_bytes == await session.scalar(
            text("SELECT row_to_json(t)::text FROM forge_runtime_topology t WHERE id=:id"), {"id": TOPOLOGY_ID}
        )
        attestation = await first.records.find_by_id(STORAGE_ATTESTATION_ID)
        assert attestation.data["storage_identity"] == (store.path.parent / STORAGE_IDENTITY_FILE).read_text()
        assert attestation.data["topology_id"] == TOPOLOGY_ID.hex


async def test_legacy_adoption_requires_existing_execution_owner_to_stop(store):
    from firefly_forge.runtime_roles import STORAGE_ATTESTATION_ID

    original = await legacy_topology(store)
    await store.acquire_ownership()
    try:
        roles = RuntimeRoles(store, "worker")
        with pytest.raises(RuntimeError, match="Stop.*legacy"):
            await roles.start()
        async with store._unit():
            assert (await roles.records.find_by_id(TOPOLOGY_ID)).data == original
            assert await roles.records.find_by_id(STORAGE_ATTESTATION_ID) is None
    finally:
        await store.release_ownership()


async def test_legacy_adoption_excludes_new_execution_owners_until_attestation_commits(
    store, store_factory, monkeypatch
):
    from firefly_forge.runtime_roles import STORAGE_ATTESTATION_ID
    from firefly_forge.store import ExecutionOwnerBusy

    await legacy_topology(store)
    peer = await store_factory(store.path)
    roles = RuntimeRoles(store, "api")
    saving, finish = asyncio.Event(), asyncio.Event()
    save = roles.records.save

    async def blocked_save(row):
        if row.id == STORAGE_ATTESTATION_ID:
            saving.set()
            await finish.wait()
        await save(row)

    monkeypatch.setattr(roles.records, "save", blocked_save)
    starting = asyncio.create_task(roles.start())
    try:
        await asyncio.wait_for(saving.wait(), 2)
        with pytest.raises(ExecutionOwnerBusy):
            await peer.acquire_ownership()
    finally:
        finish.set()
        await starting
        await peer.release_ownership()
    await peer.acquire_ownership()
    await peer.assert_ownership()
    await peer.release_ownership()


@pytest.mark.parametrize("invalid", ["public", "symlink", "hardlink", "oversize", "empty"])
async def test_storage_identity_rejects_unsafe_or_partial_marker(store, invalid):
    from firefly_forge.runtime_roles import STORAGE_IDENTITY_FILE

    path = store.path.parent / STORAGE_IDENTITY_FILE
    if invalid == "symlink":
        target = store.path.parent / "target"
        target.write_text("a" * 64)
        target.chmod(0o600)
        path.symlink_to(target)
    else:
        path.write_text("a" * 65 if invalid == "oversize" else "" if invalid == "empty" else "a" * 64)
        path.chmod(0o644 if invalid == "public" else 0o600)
        if invalid == "hardlink":
            os.link(path, store.path.parent / "another-name")
    with pytest.raises(ValueError, match="private|identity"):
        await RuntimeRoles(store, "api").start()
