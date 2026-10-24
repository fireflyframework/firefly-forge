# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Actual Git snapshot and DI-bound OAuth admission; no model, database, or CRM."""

# ruff: noqa: F401, F811

from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from test_engine_diff_artifacts import snapshot_case  # noqa: F401

from firefly_forge import engine as module


@pytest.mark.parametrize("secret_kind", ["none", "retired", "current", "missing_vault"])
async def test_actual_snapshot_admits_large_patch_before_write_tree(tmp_path, monkeypatch, secret_kind, snapshot_case):
    workspace, base = snapshot_case.root, snapshot_case.base
    secret = "protected-patch-test-secret"
    marker = secret if secret_kind in {"retired", "current"} else "public-tail"
    (workspace / "implementation.py").write_text("value = '" + "public" * 190000 + marker + "'\n")
    records = []

    async def execute(statement):
        return SimpleNamespace(mappings=lambda: records)

    async def scalar(statement):
        return uuid4()

    @asynccontextmanager
    async def unit(**kwargs):
        yield SimpleNamespace(execute=execute, scalar=scalar)

    store, service = snapshot_case.store, snapshot_case.oauth
    monkeypatch.setattr(store, "_unit", unit)
    if secret_kind == "retired":
        packet = service.matcher.packet(uuid4().hex, "registration", uuid4().hex, 1, [secret])
        records.append(
            {
                "record_kind": "evidence",
                "row_id": UUID(packet["row_id"]),
                "connection_id": UUID(packet["connection_id"]),
                "source_kind": "registration",
                "source_id": UUID(packet["source_id"]),
                "revision": 1,
                "data": packet,
            }
        )
    if secret_kind == "current":
        service._secrets["FORGE_CURRENT_SECRET"] = secret
    typed_calls = []
    original = store.source_patch_scanner

    async def typed(**kwargs):
        typed_calls.append(True)
        return await original(**kwargs)

    monkeypatch.setattr(store, "source_patch_scanner", typed)
    if secret_kind == "missing_vault":
        store.vault_source_patch_admission = None
        store.vault_credential_admission = None
        store.vault_source_diff_history = None
    writes = []
    original_checked = module.checked

    async def inspect(argv, *args, **kwargs):
        if argv[:2] == ["git", "write-tree"]:
            writes.append(True)
        return await original_checked(argv, *args, **kwargs)

    monkeypatch.setattr(module, "checked", inspect)
    engine = snapshot_case.engine
    if secret_kind == "none":
        _, patch, tree = await engine._snapshot(workspace, base)
        assert patch.artifact.byte_count > 1048576 and len(tree) == 40
        assert patch.preview_truncated is True
        assert typed_calls == [True] and writes == [True]
    else:
        with pytest.raises(ValueError, match="unavailable|credential"):
            await engine._snapshot(workspace, base)
        assert writes == []
        if secret_kind != "missing_vault":
            assert typed_calls == [True]
