# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Ordinary synthetic filesystem catalog checks; no services or credentials."""

import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest


def catalog_module():
    path = Path(__file__).parents[1] / "src/firefly_forge/source_catalog.py"
    assert path.is_file(), "The streamed source catalog helper is not implemented"
    if "owned_source_catalog" not in sys.modules:
        spec = importlib.util.spec_from_file_location("owned_source_catalog", path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
    return sys.modules["owned_source_catalog"]


async def current():
    pass


async def test_small_catalog_preserves_exact_legacy_canonical_json(tmp_path):
    m = catalog_module()
    source = tmp_path / "source"
    source.mkdir()
    root = tmp_path / "catalogs"
    root.mkdir(mode=0o700)
    (source / "é").mkdir()
    (source / "é" / "file.txt").write_text("public 世界\n")
    (source / "a.txt").write_text("ordinary text\n")
    (source / "é").chmod(0o750)
    (source / "a.txt").chmod(0o640)
    expected = {"é/": str((source / "é").stat().st_mode)}
    for name in ["a.txt", "é/file.txt"]:
        p = source / name
        expected[name] = hashlib.sha256(p.read_bytes()).hexdigest() + str(p.stat().st_mode)
    raw = json.dumps(expected, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ref = await m.retain_source_catalog(
        source,
        root,
        budget=m.SourceCatalogBudget(min_free_bytes=0),
        assert_current=current,
    )
    assert ref.manifest_digest == hashlib.sha256(raw).hexdigest()
    assert (
        ref.source_digest
        == hashlib.sha256(b'{"files":' + raw + b',"protocol":"forge-verification-source-v1"}').hexdigest()
    )
    assert ref.file_count == 2 and ref.entry_count == 3
    page = m.read_source_catalog_page(root, ref, 0)
    assert [e.path for e in page.entries] == sorted(expected)
    assert str(source) not in ref.model_dump_json()


async def test_catalog_exceeds_previous_size_limit_with_a_sparse_synthetic_file(tmp_path, record_property):
    import tracemalloc

    m = catalog_module()
    source = tmp_path / "source"
    source.mkdir()
    root = tmp_path / "catalogs"
    root.mkdir(mode=0o700)
    path = source / "large-public.bin"
    with path.open("wb") as output:
        output.truncate(101_000_001)
    try:
        tracemalloc.start()
        ref = await m.retain_source_catalog(
            source,
            root,
            budget=m.SourceCatalogBudget(min_free_bytes=0),
            assert_current=current,
        )
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        assert ref.byte_count == 101_000_001 and ref.file_count == 1 and peak < 8 * 1024 * 1024
        with path.open("rb") as stream:
            expected = hashlib.file_digest(stream, "sha256").hexdigest()
        assert m.read_source_catalog_page(root, ref, 0).entries[0].sha256 == expected
        record_property("sparse_source_bytes", ref.byte_count)
        record_property("traced_peak_bytes", peak)
    finally:
        if tracemalloc.is_tracing():
            tracemalloc.stop()
        path.unlink()


async def test_many_entries_use_external_runs_and_authenticated_pages(tmp_path, record_property):
    import tracemalloc

    m = catalog_module()
    source = tmp_path / "source"
    source.mkdir()
    root = tmp_path / "catalogs"
    root.mkdir(mode=0o700)
    for number in reversed(range(10001)):
        (source / f"entry-{number:05d}.txt").touch()
    budget = m.SourceCatalogBudget(
        min_free_bytes=0,
        chunk_bytes=4096,
        sort_entries=8,
        page_entries=7,
        merge_fan_in=3,
    )
    tracemalloc.start()
    try:
        ref = await m.retain_source_catalog(source, root, budget=budget, assert_current=current)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 8 * 1024 * 1024
    record_property("metadata_entries", ref.entry_count)
    record_property("metadata_traced_peak_bytes", peak)
    assert ref.file_count == ref.entry_count == 10001
    previous = ""
    count = 0
    for page in range(ref.page_count):
        value = m.read_source_catalog_page(root, ref, page)
        assert len(value.entries) <= 7 and value.next_page == (page + 1 if page + 1 < ref.page_count else None)
        for entry in value.entries:
            assert entry.path > previous
            previous = entry.path
            count += 1
    assert count == 10001
    assert max(path.stat().st_size for path in (root / ref.artifact_id).rglob("*") if path.is_file()) <= 4096


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo"])
async def test_nonregular_or_shared_synthetic_entries_are_rejected_without_publication(tmp_path, kind):
    import os

    m = catalog_module()
    source = tmp_path / "source"
    source.mkdir()
    root = tmp_path / "catalogs"
    root.mkdir(mode=0o700)
    outside = tmp_path / "outside"
    outside.write_text("untouched synthetic value")
    if kind == "symlink":
        (source / "entry").symlink_to(outside)
    elif kind == "hardlink":
        os.link(outside, source / "entry")
    else:
        os.mkfifo(source / "entry")
    with pytest.raises(ValueError):
        await m.retain_source_catalog(
            source,
            root,
            budget=m.SourceCatalogBudget(min_free_bytes=0),
            assert_current=current,
        )
    assert not list(root.iterdir()) and outside.read_text() == "untouched synthetic value"


@pytest.mark.parametrize("change", ["content", "mode", "new_file", "directory"])
async def test_source_change_at_final_current_check_discards_catalog(tmp_path, change):
    m = catalog_module()
    source = tmp_path / "source"
    source.mkdir()
    root = tmp_path / "catalogs"
    root.mkdir(mode=0o700)
    path = source / "entry"
    path.write_text("public text")
    calls = 0

    async def check():
        nonlocal calls
        calls += 1
        if calls == 2:
            if change == "content":
                path.write_text("different public text")
            elif change == "mode":
                path.chmod(0o600)
            elif change == "new_file":
                (source / "added").touch()
            else:
                (source / "added-dir").mkdir()

    with pytest.raises(ValueError, match="changed"):
        await m.retain_source_catalog(
            source,
            root,
            budget=m.SourceCatalogBudget(min_free_bytes=0),
            assert_current=check,
        )
    assert list(root.iterdir()) == []


@pytest.mark.parametrize("failure", ["source_budget", "storage_budget", "floor", "timeout", "cancel"])
async def test_resource_and_cancellation_failures_leave_no_published_or_partial_catalog(tmp_path, monkeypatch, failure):
    import asyncio

    m = catalog_module()
    source = tmp_path / "source"
    source.mkdir()
    root = tmp_path / "catalogs"
    root.mkdir(mode=0o700)
    (source / "entry").write_text("ordinary public data" * 100)
    budget = m.SourceCatalogBudget(
        min_free_bytes=2**63 if failure == "floor" else 0,
        max_source_bytes=1 if failure == "source_budget" else None,
        max_artifact_bytes=1 if failure == "storage_budget" else None,
        timeout_seconds=1,
    )

    async def check():
        if failure == "cancel":
            raise asyncio.CancelledError
        if failure == "timeout":
            await asyncio.sleep(2)

    with pytest.raises((ValueError, TimeoutError, asyncio.CancelledError)):
        await m.retain_source_catalog(source, root, budget=budget, assert_current=check)
    assert not list(root.iterdir())


async def test_restart_reference_and_page_integrity(tmp_path):
    m = catalog_module()
    source = tmp_path / "source"
    source.mkdir()
    root = tmp_path / "catalogs"
    root.mkdir(mode=0o700)
    (source / "entry").write_text("public value")
    ref = await m.retain_source_catalog(
        source,
        root,
        budget=m.SourceCatalogBudget(min_free_bytes=0),
        assert_current=current,
    )
    resumed = m.SourceCatalogRefV1.model_validate_json(ref.model_dump_json())
    page = m.read_source_catalog_page(root, resumed, 0)
    assert page.entries[0].path == "entry"
    manifest = json.loads((root / ref.artifact_id / "manifest.json").read_bytes())
    path = root / ref.artifact_id / manifest["data_run"] / "page-0000000000000000.jsonl"
    path.chmod(0o600)
    raw = path.read_bytes()
    path.write_bytes(bytes([raw[0] ^ 1]) + raw[1:])
    path.chmod(0o400)
    with pytest.raises(ValueError, match="integrity"):
        m.read_source_catalog_page(root, resumed, 0)


async def test_a_growing_file_does_not_expand_the_configured_source_read_budget(tmp_path, monkeypatch):
    m = catalog_module()
    source = tmp_path / "source"
    source.mkdir()
    root = tmp_path / "catalogs"
    root.mkdir(mode=0o700)
    path = source / "entry"
    path.write_bytes(b"old")
    inode = path.stat().st_ino
    original = m.os.read
    read_bytes = []
    grew = False

    def read(fd, limit):
        nonlocal grew
        raw = original(fd, limit)
        if m.os.fstat(fd).st_ino == inode:
            read_bytes.append(len(raw))
            if not grew:
                grew = True
                with path.open("ab") as output:
                    output.write(b"x" * 200000)
        return raw

    monkeypatch.setattr(m.os, "read", read)
    with pytest.raises(ValueError):
        await m.retain_source_catalog(
            source,
            root,
            budget=m.SourceCatalogBudget(min_free_bytes=0, max_source_bytes=3),
            assert_current=current,
        )
    assert sum(read_bytes) <= 4
    assert list(root.iterdir()) == []


async def test_cancellation_during_an_active_stream_cleans_its_owned_stage(tmp_path):
    import asyncio

    m = catalog_module()
    source = tmp_path / "source"
    source.mkdir()
    root = tmp_path / "catalogs"
    root.mkdir(mode=0o700)
    path = source / "directory"
    path.mkdir()
    with (path / "large").open("wb") as output:
        output.truncate(5_000_000)
    entered = asyncio.Event()

    async def check():
        entered.set()

    task = asyncio.create_task(
        m.retain_source_catalog(
            source,
            root,
            budget=m.SourceCatalogBudget(min_free_bytes=0),
            assert_current=check,
        )
    )
    await entered.wait()
    await asyncio.sleep(0)
    assert any(root.iterdir()) and not task.done()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert list(root.iterdir()) == []


@pytest.mark.parametrize("suffix", [".pem", ".key"])
@pytest.mark.parametrize("entry_kind", ["file", "directory"])
async def test_legacy_suffix_selection_excludes_synthetic_entries_and_keeps_public_examples(
    tmp_path, suffix, entry_kind
):
    m = catalog_module()
    source = tmp_path / "source"
    source.mkdir()
    root = tmp_path / "catalogs"
    root.mkdir(mode=0o700)
    excluded = source / ("ordinary-fixture" + suffix)
    if entry_kind == "file":
        excluded.write_text("Harmless public fixture text.\n")
    else:
        excluded.mkdir()
        (excluded / "public.txt").write_text("Harmless public directory fixture.\n")
    kept = [".env.example", "README.txt", "notes.PEM"]
    for name in kept:
        (source / name).write_text("Public example content.\n")
    expected = {
        name: hashlib.sha256((source / name).read_bytes()).hexdigest() + str((source / name).stat().st_mode)
        for name in kept
    }
    encoded = json.dumps(expected, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ref = await m.retain_source_catalog(
        source,
        root,
        budget=m.SourceCatalogBudget(min_free_bytes=0),
        assert_current=current,
    )
    entries = m.read_source_catalog_page(root, ref, 0).entries
    assert [entry.path for entry in entries] == sorted(kept)
    assert ref.file_count == ref.entry_count == 3
    assert ref.manifest_digest == hashlib.sha256(encoded).hexdigest()
    assert (
        ref.source_digest
        == hashlib.sha256(b'{"files":' + encoded + b',"protocol":"forge-verification-source-v1"}').hexdigest()
    )
