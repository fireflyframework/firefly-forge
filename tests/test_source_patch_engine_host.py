# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Actual Git snapshot and DI-bound OAuth admission; no model, database, or CRM."""

from contextlib import asynccontextmanager
from types import MethodType, SimpleNamespace
from uuid import UUID, uuid4

import pytest
from cryptography.fernet import Fernet
from test_execution import seed_repository

from firefly_forge import engine as module
from firefly_forge.connection_oauth import ConnectionOAuth
from firefly_forge.engine import Engine
from firefly_forge.process import checked
from firefly_forge.store import Store


@pytest.mark.parametrize("secret_kind", ["none", "retired", "current", "missing_vault"])
async def test_actual_snapshot_admits_large_patch_before_write_tree(tmp_path, monkeypatch, secret_kind):
    workspace = await seed_repository(tmp_path / "source")
    base = await checked(["git", "rev-parse", "HEAD"], workspace)
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

    store = SimpleNamespace(path=tmp_path / "fixture.db", schema="forge_patch_engine", _unit=unit)
    service = ConnectionOAuth(store, encryption_key=Fernet.generate_key().decode(), browser_origin="")
    store.reject_credentials = lambda _value: None
    store.admit_credentials = MethodType(Store.admit_credentials, store)
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
    admitted = getattr(store, "vault_source_patch_admission", None)

    async def typed(patch):
        typed_calls.append(len(patch.text.encode()))
        if admitted is None:
            await service.admit_credentials(patch.text)
        else:
            await admitted(patch)

    store.vault_source_patch_admission = typed
    if secret_kind == "missing_vault":
        store.vault_source_patch_admission = None
        store.vault_credential_admission = None
    writes = []
    original_checked = module.checked

    async def inspect(argv, *args, **kwargs):
        if argv[:2] == ["git", "write-tree"]:
            writes.append(True)
        return await original_checked(argv, *args, **kwargs)

    monkeypatch.setattr(module, "checked", inspect)
    engine = Engine(store, tmp_path / "workspaces")
    if secret_kind == "none":
        _, patch, tree = await engine._snapshot(workspace, base)
        assert len(patch.encode()) > 1048576 and len(tree) == 40
        assert typed_calls == [len(patch.encode())] and writes == [True]
    else:
        with pytest.raises(ValueError, match="unavailable|configured credentials"):
            await engine._snapshot(workspace, base)
        assert writes == []
        if secret_kind != "missing_vault":
            assert len(typed_calls) == 1 and typed_calls[0] > 1048576
