# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Read-only native source observations recheck the current reader and lineage."""

import copy
from types import SimpleNamespace
from uuid import UUID

import pytest
from test_runner_object_artifacts_host import allowed, authority_fence
from test_source_archive_publisher_host import fixture

from firefly_forge.identity import Principal
from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied
from firefly_forge.source_explorer import SourceExplorer


async def prepared(tmp_path):
    publisher, workspace, run, records = fixture(tmp_path)
    run["execution_actor"] = {"user_id": UUID(int=7).hex}
    ref = await publisher.publish(run, workspace, "a" * 40, "b" * 40, authority_fence(), authorize=allowed)
    state = SimpleNamespace(run=copy.deepcopy(run), valid=True, checks=0, receipts=[])

    async def get(value):
        assert value == run["id"]
        return copy.deepcopy(state.run)

    async def authorize(current, actor, permission):
        state.checks += 1
        assert permission == "read" and actor["user_id"] == UUID(int=7).hex
        if not state.valid:
            raise AccessDenied("revoked")

    async def view(*args, **kwargs):
        return {"items": copy.deepcopy(state.receipts)}

    manager = SimpleNamespace(
        publisher=publisher,
        tenant=UUID(int=1),
        settings=publisher.content.helper.objects.settings,
        policy=SimpleNamespace(enabled=True, excluded_projects=frozenset()),
    )
    deliveries = SimpleNamespace(store=SimpleNamespace(get_run=get), _authorize=authorize, view=view)
    return SourceExplorer(manager, deliveries), ref, state, publisher, workspace


async def test_catalog_and_binary_windows_have_recorded_lineage_and_no_mutable_workspace_read(tmp_path):
    explorer, ref, state, publisher, workspace = await prepared(tmp_path)
    reader = Principal(UUID(int=7), "issuer", "user")
    token = CURRENT_PRINCIPAL.set(reader)
    try:
        (workspace / "data.bin").unlink()
        observation = await explorer.catalog(ref.run_id, ref.snapshot_id, ref.catalog.manifest_sha256, page=1)
        assert observation["catalog"]["entries"][0]["path"] == "data.bin"
        data = await explorer.file(
            ref.run_id, ref.snapshot_id, ref.catalog.manifest_sha256, page=1, entry_index=0, offset=1, limit=3
        )
        assert data["binary"] is True and data["text"] is None and data["byte_count"] == 3
        assert data["reference"]["tree"] == "b" * 40 and state.checks > 3
    finally:
        CURRENT_PRINCIPAL.reset(token)


@pytest.mark.parametrize("mutation", ["tenant", "project", "manifest", "reader", "revoked"])
async def test_foreign_or_revoked_snapshot_never_returns_content(tmp_path, mutation):
    explorer, ref, state, publisher, _ = await prepared(tmp_path)
    token = CURRENT_PRINCIPAL.set(Principal(UUID(int=7), "issuer", "user"))
    expected = ref.catalog.manifest_sha256
    if mutation == "tenant":
        explorer.manager.tenant = UUID(int=9)
    elif mutation == "project":
        state.run["project_id"] = UUID(int=9).hex
    elif mutation == "manifest":
        expected = "f" * 64
    elif mutation == "reader":
        CURRENT_PRINCIPAL.set(None)
    elif mutation == "revoked":
        state.valid = False
    try:
        with pytest.raises((AccessDenied, ValueError)):
            await explorer.file(ref.run_id, ref.snapshot_id, expected, page=0, entry_index=0, offset=0, limit=3)
    finally:
        CURRENT_PRINCIPAL.reset(token)


async def test_revocation_during_object_read_and_foreign_checkpoint_tree_hold(tmp_path):
    explorer, ref, state, publisher, _ = await prepared(tmp_path)
    token = CURRENT_PRINCIPAL.set(Principal(UUID(int=7), "issuer", "user"))
    try:
        state.receipts = [{"request_id": "d" * 32, "source_tree": "f" * 40}]
        with pytest.raises(ValueError, match="checkpoint"):
            await explorer.catalog(
                ref.run_id, ref.snapshot_id, ref.catalog.manifest_sha256, page=0, request_id="d" * 32
            )
        read = publisher.content.helper.objects.read_chunk

        async def revoked(*args, **kwargs):
            result = await read(*args, **kwargs)
            state.valid = False
            return result

        publisher.content.helper.objects.read_chunk = revoked
        with pytest.raises(AccessDenied):
            await explorer.file(
                ref.run_id, ref.snapshot_id, ref.catalog.manifest_sha256, page=0, entry_index=0, offset=0, limit=3
            )
        assert not list(publisher.content.helper.scratch.iterdir())
    finally:
        CURRENT_PRINCIPAL.reset(token)
