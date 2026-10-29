# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Retained public-source hashes remain authoritative after ctime-only updates."""

import asyncio
import hashlib
import os
import stat

import pytest

from firefly_forge import source_catalog as catalog


async def current():
    pass


async def prepared(tmp_path, payload=b"public content\n" * 1000):
    source, storage = tmp_path / "source", tmp_path / "catalogs"
    source.mkdir()
    storage.mkdir(mode=0o700)
    path = source / "public.txt"
    path.write_bytes(payload)
    budget = catalog.SourceCatalogBudget(min_free_bytes=0, chunk_bytes=1024)
    reference = await catalog.retain_source_catalog(source, storage, budget=budget, assert_current=current)
    return source, storage, path, reference


async def change_ctime(path):
    before = path.stat()
    await asyncio.sleep(0.002)
    path.chmod(stat.S_IMODE(before.st_mode))
    after = path.stat()
    assert before.st_ctime_ns != after.st_ctime_ns
    assert (
        before.st_dev,
        before.st_ino,
        before.st_uid,
        before.st_gid,
        before.st_mode,
        before.st_nlink,
        before.st_size,
        before.st_mtime_ns,
    ) == (
        after.st_dev,
        after.st_ino,
        after.st_uid,
        after.st_gid,
        after.st_mode,
        after.st_nlink,
        after.st_size,
        after.st_mtime_ns,
    )


async def test_ctime_only_public_file_rehashes_in_windows_without_replacing_reference(tmp_path, monkeypatch):
    source, storage, path, reference = await prepared(tmp_path)
    retained = reference.model_dump()
    manifest = storage / reference.artifact_id / "manifest.json"
    manifest_sha = hashlib.sha256(manifest.read_bytes()).hexdigest()
    await change_ctime(path)
    inode = path.stat().st_ino
    original = os.read
    windows = []

    def bounded_read(fd, width):
        if os.fstat(fd).st_ino == inode:
            assert width <= 1024
            windows.append(width)
        return original(fd, width)

    monkeypatch.setattr(os, "read", bounded_read)
    await catalog.revalidate_source_catalog(source, storage, reference, assert_current=current)
    assert len(windows) > 2
    first_read_count = len(windows)
    windows.clear()
    await catalog.revalidate_source_catalog(source, storage, reference, assert_current=current)
    assert len(windows) == first_read_count
    assert reference.model_dump() == retained
    assert hashlib.sha256(manifest.read_bytes()).hexdigest() == manifest_sha


async def test_unchanged_file_identity_does_not_read_payload_again(tmp_path, monkeypatch):
    source, storage, path, reference = await prepared(tmp_path)
    inode = path.stat().st_ino
    original = os.read

    def no_payload_read(fd, width):
        if os.fstat(fd).st_ino == inode:
            raise AssertionError("Unchanged identities must retain the metadata-only fast path")
        return original(fd, width)

    monkeypatch.setattr(os, "read", no_payload_read)
    await catalog.revalidate_source_catalog(source, storage, reference, assert_current=current)


@pytest.mark.parametrize("payload", [b"", b"\x00\xffpublic\x80" * 3000], ids=["empty", "large-binary"])
async def test_ctime_only_empty_and_binary_files_keep_original_hash(tmp_path, payload):
    source, storage, path, reference = await prepared(tmp_path, payload)
    await change_ctime(path)
    await catalog.revalidate_source_catalog(source, storage, reference, assert_current=current)
    assert (
        catalog.read_source_catalog_page(storage, reference, 0).entries[0].sha256 == hashlib.sha256(payload).hexdigest()
    )


async def test_same_size_payload_tamper_with_restored_mtime_is_not_rebaselined(tmp_path):
    source, storage, path, reference = await prepared(tmp_path)
    before, payload = path.stat(), path.read_bytes()
    path.write_bytes(payload.replace(b"public", b"forged", 1))
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    with pytest.raises(ValueError):
        await catalog.revalidate_source_catalog(source, storage, reference, assert_current=current)
    assert (
        catalog.read_source_catalog_page(storage, reference, 0).entries[0].sha256 == hashlib.sha256(payload).hexdigest()
    )


@pytest.mark.parametrize("change", ["inode", "mode", "mtime", "symlink", "hardlink"])
async def test_other_identity_changes_refuse_without_payload_read(tmp_path, monkeypatch, change):
    source, storage, path, reference = await prepared(tmp_path)
    before = path.stat()
    if change == "inode":
        replacement = path.with_name("replacement.txt")
        replacement.write_bytes(path.read_bytes())
        replacement.chmod(stat.S_IMODE(before.st_mode))
        os.utime(replacement, ns=(before.st_atime_ns, before.st_mtime_ns))
        replacement.replace(path)
    elif change == "mode":
        path.chmod(0o600 if stat.S_IMODE(before.st_mode) != 0o600 else 0o640)
    elif change == "mtime":
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000))
    elif change == "symlink":
        retained = source.parent / "retained.txt"
        path.rename(retained)
        path.symlink_to(retained)
    else:
        os.link(path, source.parent / "alias.txt")
    inode, original = path.stat().st_ino, os.read

    def no_payload_read(fd, width):
        if os.fstat(fd).st_ino == inode:
            raise AssertionError("Only a ctime-only change may read the payload")
        return original(fd, width)

    monkeypatch.setattr(os, "read", no_payload_read)
    with pytest.raises((ValueError, OSError)):
        await catalog.revalidate_source_catalog(source, storage, reference, assert_current=current)


@pytest.mark.parametrize("change", ["ctime", "grow", "truncate", "file", "folder", "root"])
async def test_metadata_or_path_races_during_rehash_refuse(tmp_path, monkeypatch, change):
    source, storage, path, reference = await prepared(tmp_path)
    if change == "folder":
        folder = source / "nested"
        folder.mkdir()
        path.rename(folder / path.name)
        path = folder / path.name
        reference = await catalog.retain_source_catalog(
            source,
            storage,
            budget=catalog.SourceCatalogBudget(min_free_bytes=0, chunk_bytes=1024),
            assert_current=current,
        )
    await change_ctime(path)
    inode, original, changed = path.stat().st_ino, os.read, False

    def race(fd, width):
        nonlocal changed
        raw = original(fd, width)
        if os.fstat(fd).st_ino == inode and not changed:
            changed = True
            if change == "ctime":
                path.chmod(stat.S_IMODE(path.stat().st_mode))
            elif change == "grow":
                with path.open("ab") as output:
                    output.write(b"x")
            elif change == "truncate":
                with path.open("wb"):
                    pass
            elif change == "file":
                path.rename(tmp_path / "retained-public.txt")
                path.write_bytes(b"public content\n" * 1000)
            elif change == "folder":
                folder.rename(tmp_path / "retained-folder")
                folder.mkdir()
                path.write_bytes(b"public content\n" * 1000)
            else:
                source.rename(tmp_path / "retained-source")
                source.mkdir()
        return raw

    monkeypatch.setattr(os, "read", race)
    with pytest.raises((ValueError, OSError)):
        await catalog.revalidate_source_catalog(source, storage, reference, assert_current=current)
    assert changed
    assert not list(storage.glob(".*.partial"))


async def test_rehash_honors_cancellation_and_cleans_partial_catalog(tmp_path, monkeypatch):
    source, storage, path, reference = await prepared(tmp_path)
    await change_ctime(path)
    inode, original, cancelled = path.stat().st_ino, os.read, False

    def cancel_after_chunk(fd, width):
        nonlocal cancelled
        raw = original(fd, width)
        if os.fstat(fd).st_ino == inode:
            cancelled = True
        return raw

    monkeypatch.setattr(os, "read", cancel_after_chunk)
    with pytest.raises(asyncio.CancelledError):
        await catalog.revalidate_source_catalog(
            source, storage, reference, assert_current=current, cancel_requested=lambda: cancelled
        )
    assert cancelled
    assert not list(storage.glob(".*.partial"))
    assert (storage / reference.artifact_id / "manifest.json").is_file()


async def test_rehash_honors_original_free_space_floor_between_chunks(tmp_path, monkeypatch):
    from types import SimpleNamespace

    source, storage, path, reference = await prepared(tmp_path)
    reference = await catalog.retain_source_catalog(
        source, storage, budget=catalog.SourceCatalogBudget(min_free_bytes=1, chunk_bytes=1024), assert_current=current
    )
    await change_ctime(path)
    inode, original_read, original_space = path.stat().st_ino, os.read, os.fstatvfs
    exhausted = False

    def exhaust_after_chunk(fd, width):
        nonlocal exhausted
        raw = original_read(fd, width)
        if os.fstat(fd).st_ino == inode:
            exhausted = True
        return raw

    def space(fd):
        return SimpleNamespace(f_bavail=0, f_frsize=1) if exhausted else original_space(fd)

    monkeypatch.setattr(os, "read", exhaust_after_chunk)
    monkeypatch.setattr(os, "fstatvfs", space)
    with pytest.raises(ValueError):
        await catalog.revalidate_source_catalog(source, storage, reference, assert_current=current)
    assert exhausted
    assert not list(storage.glob(".*.partial"))


async def test_rehash_honors_original_deadline_between_chunks(tmp_path, monkeypatch):
    import time

    source, storage, path, reference = await prepared(tmp_path)
    reference = await catalog.retain_source_catalog(
        source,
        storage,
        budget=catalog.SourceCatalogBudget(min_free_bytes=0, timeout_seconds=1, chunk_bytes=1024),
        assert_current=current,
    )
    await change_ctime(path)
    inode, original, delayed = path.stat().st_ino, os.read, False

    def slow_chunk(fd, width):
        nonlocal delayed
        raw = original(fd, width)
        if os.fstat(fd).st_ino == inode and not delayed:
            delayed = True
            time.sleep(1.05)
        return raw

    monkeypatch.setattr(os, "read", slow_chunk)
    with pytest.raises(TimeoutError):
        await catalog.revalidate_source_catalog(source, storage, reference, assert_current=current)
    assert delayed
    assert not list(storage.glob(".*.partial"))


async def test_large_ctime_only_file_revalidation_keeps_bounded_memory(tmp_path, record_property):
    import tracemalloc

    source, storage = tmp_path / "source", tmp_path / "catalogs"
    source.mkdir()
    storage.mkdir(mode=0o700)
    path = source / "public-large.bin"
    with path.open("wb") as output:
        output.truncate(8_388_609)
    reference = await catalog.retain_source_catalog(
        source,
        storage,
        budget=catalog.SourceCatalogBudget(min_free_bytes=0, chunk_bytes=65536),
        assert_current=current,
    )
    await change_ctime(path)
    tracemalloc.start()
    try:
        await catalog.revalidate_source_catalog(source, storage, reference, assert_current=current)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 2 * 1024**2
    record_property("ctime_rehashed_bytes", 8_388_609)
    record_property("ctime_rehash_peak_bytes", peak)
