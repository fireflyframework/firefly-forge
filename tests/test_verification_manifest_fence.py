# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Immutable manifest custody despite post-publication filesystem metadata updates."""

import asyncio
import os

import pytest

from firefly_forge import source_catalog as catalog
from firefly_forge import verification_source_preparation as preparation


async def prepared(tmp_path):
    source, storage = tmp_path / "source", tmp_path / "catalogs"
    source.mkdir()
    storage.mkdir(mode=0o700)
    (source / "public.txt").write_text("public source\n")

    async def current():
        pass

    value = await preparation.prepare_verification_source(
        source, storage, budget=catalog.SourceCatalogBudget(min_free_bytes=0), assert_current=current
    )
    return value, storage / value.reference.artifact_id / "manifest.json", current


async def test_ctime_only_manifest_update_keeps_original_custody(tmp_path):
    value, manifest, current = await prepared(tmp_path)
    before = manifest.stat()
    raw = manifest.read_bytes()
    await asyncio.sleep(0.002)
    manifest.chmod(0o400)
    after = manifest.stat()
    assert before.st_ctime_ns != after.st_ctime_ns
    assert catalog._stamp(before)[:-1] == catalog._stamp(after)[:-1]
    assert manifest.read_bytes() == raw
    value.fence()
    await value.revalidate(assert_current=current)
    assert manifest.read_bytes() == raw


async def test_manifest_same_size_tamper_with_restored_mtime_is_denied(tmp_path):
    value, manifest, _ = await prepared(tmp_path)
    before = manifest.stat()
    raw = manifest.read_bytes()
    changed = raw.replace(b'"version":1', b'"version":2')
    assert len(changed) == len(raw) and changed != raw
    manifest.chmod(0o600)
    manifest.write_bytes(changed)
    manifest.chmod(0o400)
    os.utime(manifest, ns=(before.st_atime_ns, before.st_mtime_ns))
    with pytest.raises(ValueError):
        value.fence()


@pytest.mark.parametrize("change", ["inode", "symlink", "mode", "hardlink", "folder", "root"])
async def test_manifest_or_catalog_custody_changes_are_denied(tmp_path, change):
    value, manifest, _ = await prepared(tmp_path)
    if change in {"inode", "symlink"}:
        other = manifest.with_name("other.json")
        other.write_bytes(manifest.read_bytes())
        other.chmod(0o400)
        manifest.unlink()
        if change == "inode":
            other.rename(manifest)
        else:
            manifest.symlink_to(other)
    elif change == "mode":
        manifest.chmod(0o600)
    elif change == "hardlink":
        os.link(manifest, manifest.with_name("alias.json"))
    elif change == "folder":
        original = manifest.parent
        original.rename(original.with_name("retained-folder"))
        original.mkdir(mode=0o700)
    else:
        original = value.catalog_root
        original.rename(original.with_name("retained-root"))
        original.mkdir(mode=0o700)
    with pytest.raises((ValueError, OSError)):
        value.fence()


async def test_manifest_ctime_change_during_descriptor_read_is_denied(tmp_path, monkeypatch):
    value, manifest, _ = await prepared(tmp_path)
    original = os.pread
    inode = manifest.stat().st_ino

    def changed_during_read(fd, width, offset):
        raw = original(fd, width, offset)
        if os.fstat(fd).st_ino == inode:
            manifest.chmod(0o400)
        return raw

    monkeypatch.setattr(os, "pread", changed_during_read)
    with pytest.raises(ValueError):
        value.fence()


@pytest.mark.parametrize("change", ["file", "folder", "root"])
async def test_path_replacement_during_manifest_read_is_denied(tmp_path, monkeypatch, change):
    value, manifest, _ = await prepared(tmp_path)
    original = os.pread
    inode = manifest.stat().st_ino

    def replaced_during_read(fd, width, offset):
        raw = original(fd, width, offset)
        if os.fstat(fd).st_ino == inode:
            path = {"file": manifest, "folder": manifest.parent, "root": value.catalog_root}[change]
            path.rename(path.with_name("retained-identity"))
            if change == "file":
                path.write_bytes(raw)
                path.chmod(0o400)
            else:
                path.mkdir(mode=0o700)
        return raw

    monkeypatch.setattr(os, "pread", replaced_during_read)
    with pytest.raises((ValueError, OSError)):
        value.fence()


async def test_ctime_change_between_descriptor_read_and_path_fence_is_denied(tmp_path, monkeypatch):
    value, manifest, _ = await prepared(tmp_path)
    original = preparation._read

    def changed_after_read(parent, name, width):
        raw = original(parent, name, width)
        manifest.chmod(0o400)
        return raw

    monkeypatch.setattr(preparation, "_read", changed_after_read)
    with pytest.raises(ValueError):
        value.fence()
