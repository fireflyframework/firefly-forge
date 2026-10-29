# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Maintenance reads retained identity and compares copies without creating defaults."""

import hashlib
import os
from types import SimpleNamespace

import pytest

from firefly_forge.native_location_files import read_storage_identity
from firefly_forge.source_catalog import SourceCatalogBudget
from firefly_forge.verification_dependencies import prepare_dependency_capture


def identity_root(tmp_path):
    root = tmp_path / "storage"
    root.mkdir(mode=0o700)
    return root


def test_missing_identity_is_a_hold_without_even_creating_a_lock_or_directory(tmp_path):
    root = identity_root(tmp_path)
    with pytest.raises(ValueError, match="retained"):
        read_storage_identity(root)
    assert list(root.iterdir()) == []


def test_existing_identity_is_private_stable_and_never_exposed_by_repr(tmp_path):
    root = identity_root(tmp_path)
    path = root / "runtime-storage.identity"
    path.write_bytes(b"a" * 64)
    path.chmod(0o600)
    value = read_storage_identity(root)
    assert value.sha256 == hashlib.sha256(b"a" * 64).hexdigest()
    assert "a" * 64 not in repr(value)
    assert value.matches("a" * 64)
    assert not value.matches("b" * 64)
    value.fence()
    path.write_bytes(b"b" * 64)
    with pytest.raises(ValueError, match="changed"):
        value.fence()


def test_unrelated_private_directory_activity_cannot_replace_or_expire_identity(tmp_path):
    root = identity_root(tmp_path)
    path = root / "runtime-storage.identity"
    path.write_bytes(b"a" * 64)
    path.chmod(0o600)
    value = read_storage_identity(root)
    (root / "artifacts").mkdir()
    value.fence()


@pytest.mark.parametrize("case", ["symlink", "hardlink", "public", "short", "invalid", "root_alias"])
def test_identity_refuses_aliases_or_incomplete_private_material(tmp_path, case):
    root = identity_root(tmp_path)
    path = root / "runtime-storage.identity"
    path.write_bytes(b"a" * 64)
    path.chmod(0o600)
    if case == "symlink":
        target = root / "other"
        path.rename(target)
        path.symlink_to(target)
    elif case == "hardlink":
        os.link(path, root / "other")
    elif case == "public":
        path.chmod(0o644)
    elif case == "short":
        path.write_bytes(b"a" * 63)
    elif case == "invalid":
        path.write_bytes(b"z" * 64)
    else:
        alias = tmp_path / "alias"
        alias.symlink_to(root, target_is_directory=True)
        root = alias
    with pytest.raises(ValueError):
        read_storage_identity(root)


def test_dataless_identity_refuses_before_open(tmp_path, monkeypatch):
    from pathlib import Path

    root = identity_root(tmp_path)
    path = root / "runtime-storage.identity"
    path.write_bytes(b"a" * 64)
    path.chmod(0o600)
    original_stat = Path.lstat
    original_open = os.open

    def unavailable(selected):
        observed = original_stat(selected)
        if selected != path:
            return observed
        fields = {name: getattr(observed, name) for name in dir(observed) if name.startswith("st_")}
        return SimpleNamespace(**{**fields, "st_flags": 0x40000000})

    def guarded_open(selected, *args, **kwargs):
        assert selected != path, "A dataless identity was opened before refusal"
        return original_open(selected, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", unavailable)
    monkeypatch.setattr(os, "open", guarded_open)
    with pytest.raises(ValueError, match="unavailable"):
        read_storage_identity(root)


async def test_portable_dependency_capture_proves_copy_bytes_but_keeps_native_fences(tmp_path):
    source, target, scratch = (tmp_path / name for name in ("source", "target", "scratch"))
    for root in (source, target):
        root.mkdir(mode=0o700)
        (root / "module.py").write_text("VALUE = 1\n")

    async def current():
        pass

    budget = SourceCatalogBudget(min_free_bytes=0)
    old = await prepare_dependency_capture([source], scratch, budget, current, portable_root=source)
    new = await prepare_dependency_capture([target], scratch, budget, current, portable_root=target)
    assert old.digest == new.digest
    assert old.metadata_cut != new.metadata_cut
    await old.assert_current()
    await new.assert_current()
    (target / "module.py").write_text("VALUE = 2\n")
    with pytest.raises(ValueError):
        await new.assert_current()


def test_postgres_identity_requires_owned_local_control_and_running_directory_binding(tmp_path):
    import sys

    from firefly_forge.native_location_files import read_postgres_identity

    root = tmp_path / "postgres"
    root.mkdir(mode=0o700)
    (root / "global").mkdir(mode=0o700)
    (root / "PG_VERSION").write_text("17\n")
    control = root / "global" / "pg_control"
    control.write_bytes((123456789).to_bytes(8, sys.byteorder) + b"x" * 8184)
    control.chmod(0o600)
    (root / "postmaster.pid").write_text(f"123\n{root}\n1000\n18545\n")
    value = read_postgres_identity(root)
    assert value.system_identifier == "123456789"
    assert value.port == 18545
    assert value.started_at == 1000
    (root / "postmaster.pid").write_text(f"123\n{tmp_path}\n1000\n18545\n")
    with pytest.raises(ValueError, match="directory"):
        read_postgres_identity(root)


def test_postgres_identity_refuses_control_alias_before_read(tmp_path):
    from firefly_forge.native_location_files import read_postgres_identity

    root = tmp_path / "postgres"
    root.mkdir(mode=0o700)
    (root / "global").mkdir(mode=0o700)
    (root / "PG_VERSION").write_text("17\n")
    (root / "postmaster.pid").write_text(f"123\n{root}\n1000\n18545\n")
    outside = tmp_path / "control"
    outside.write_bytes(b"x" * 72)
    (root / "global" / "pg_control").symlink_to(outside)
    with pytest.raises(ValueError, match="ordinary"):
        read_postgres_identity(root)
