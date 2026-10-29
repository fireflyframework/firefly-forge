# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Late native-writer fences revalidate metadata without reading executable payloads."""

import asyncio
import contextvars
import hashlib
import json
import os
import stat
import threading
from types import SimpleNamespace

import pytest

from firefly_forge import verification_dependencies as module
from firefly_forge.source_catalog import SourceCatalogBudget


async def prepare(tmp_path, public=False):
    root = tmp_path / ("source" if public else "dependencies")
    root.mkdir()
    (root / "module.py").write_text("VALUE = 1\n")
    name = "prepare_public_source_cut" if public else "prepare_dependency_capture"
    factory = getattr(module, name, None)
    assert callable(factory), "Prepared input capture API is missing"
    budget = SourceCatalogBudget(min_free_bytes=0, sort_entries=2)
    if public:
        captured = await factory(root, tmp_path / "catalog", budget)
    else:

        async def current():
            pass

        captured = await factory([root], tmp_path / "catalog", budget, current)
    return root, captured


async def test_existing_fingerprint_stays_a_byte_digest_and_prepared_capture_keeps_its_cut(tmp_path):
    root, captured = await prepare(tmp_path)

    async def current():
        pass

    assert (
        await module.dependency_fingerprint([root], tmp_path / "legacy", SourceCatalogBudget(min_free_bytes=0), current)
        == captured.digest
    )
    entries = [
        {"path": str(root), "kind": "directory", "mode": root.stat().st_mode},
        {
            "path": str(root / "module.py"),
            "kind": "file",
            "mode": (root / "module.py").stat().st_mode,
            "sha256": hashlib.sha256(b"VALUE = 1\n").hexdigest(),
        },
    ]
    assert (
        captured.digest
        == hashlib.sha256(
            b"".join(
                json.dumps(entry, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode() + b"\n"
                for entry in entries
            )
        ).hexdigest()
    )
    assert len(captured.digest) == len(captured.metadata_cut) == 64
    await captured.assert_current()


@pytest.mark.parametrize("public", [False, True])
async def test_fence_reads_no_input_payload_or_regular_file_descriptor(tmp_path, monkeypatch, public):
    root, captured = await prepare(tmp_path, public)
    original_open = os.open

    def open_file(path, flags, *args, **kwargs):
        assert path != "module.py", "Metadata fence opened an input payload"
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", open_file)
    await captured.assert_current()


@pytest.mark.parametrize("public", [False, True])
@pytest.mark.parametrize("change", ["same_size_restored_mtime", "new_member", "removed_member", "mode"])
async def test_fence_rejects_late_public_or_dependency_drift(tmp_path, public, change):
    root, captured = await prepare(tmp_path, public)
    path = root / "module.py"
    before = path.stat()
    if change == "same_size_restored_mtime":
        path.write_text("VALUE = 2\n")
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    elif change == "new_member":
        (root / "later.py").write_text("LATER = True\n")
    elif change == "removed_member":
        path.unlink()
    else:
        path.chmod(0o400)
    with pytest.raises(ValueError, match="metadata|input|changed"):
        await captured.assert_current()


async def test_public_cut_ignores_private_and_derived_membership_but_keeps_examples_public(tmp_path):
    root, captured = await prepare(tmp_path, True)
    for name in [".git", "node_modules", ".cache", "dist", "__pycache__"]:
        folder = root / name
        folder.mkdir()
        (folder / "ignored").write_text("excluded output")
    (root / ".env.secret").write_text("private fixture")
    await captured.assert_current()
    (root / ".env.example").write_text("PUBLIC_EXAMPLE=1\n")
    with pytest.raises(ValueError, match="metadata|input|changed"):
        await captured.assert_current()


async def test_dependency_cut_includes_executable_cache_bytes_and_resolved_link_membership(tmp_path):
    root, captured = await prepare(tmp_path)
    (root / ".vite").mkdir()
    (root / ".vite" / "transform.js").write_text("changed cache")
    with pytest.raises(ValueError, match="metadata|input|changed"):
        await captured.assert_current()


@pytest.mark.parametrize("change", ["retarget", "target_content", "target_replaced"])
async def test_dependency_fence_binds_link_and_resolved_target_without_payload_reads(tmp_path, change):
    root = tmp_path / "dependencies"
    root.mkdir()
    target = tmp_path / "installed.py"
    target.write_text("VALUE = 1\n")
    alias = root / "runtime.py"
    alias.symlink_to(target)

    async def current():
        pass

    captured = await module.prepare_dependency_capture(
        [root], tmp_path / "catalog", SourceCatalogBudget(min_free_bytes=0), current
    )
    if change == "retarget":
        other = tmp_path / "other.py"
        other.write_text("VALUE = 1\n")
        alias.unlink()
        alias.symlink_to(other)
    elif change == "target_content":
        target.write_text("VALUE = 2\n")
    else:
        replacement = tmp_path / "replacement.py"
        replacement.write_text("VALUE = 1\n")
        replacement.replace(target)
    with pytest.raises(ValueError, match="metadata|input|changed"):
        await captured.assert_current()


async def test_public_fence_rejects_late_symlink_without_following_it(tmp_path):
    root, captured = await prepare(tmp_path, True)
    (root / "escape.py").symlink_to(tmp_path / "not-a-readable-source-target")
    with pytest.raises(ValueError, match="Public source"):
        await captured.assert_current()


async def test_metadata_fence_rejects_late_dataless_flag_without_open(tmp_path, monkeypatch):
    _, captured = await prepare(tmp_path)
    original_stat, original_open = os.stat, os.open

    def information(path, *args, **kwargs):
        value = original_stat(path, *args, **kwargs)
        if path == "module.py":
            fields = {name: getattr(value, name) for name in dir(value) if name.startswith("st_")}
            return SimpleNamespace(**{**fields, "st_flags": 1 << 30})
        return value

    def open_file(path, *args, **kwargs):
        assert path != "module.py", "Dataless input was opened"
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(stat, "SF_DATALESS", 1 << 30, raising=False)
    monkeypatch.setattr(os, "stat", information)
    monkeypatch.setattr(os, "open", open_file)
    with pytest.raises(ValueError, match="unavailable locally"):
        await captured.assert_current()


async def test_fence_has_fresh_context_and_does_not_call_outside_unit_guard(tmp_path, monkeypatch):
    _, captured = await prepare(tmp_path)
    marker = contextvars.ContextVar("fixture_sql_context", default=None)
    original_stat = os.stat
    observed = []

    def information(*args, **kwargs):
        observed.append(marker.get())
        return original_stat(*args, **kwargs)

    async def forbidden(*args):
        raise AssertionError("Native-writer metadata fence used the outside-unit helper")

    monkeypatch.setattr(module, "_offloop", forbidden)
    monkeypatch.setattr(os, "stat", information)
    token = marker.set("native-writer-unit")
    try:
        await captured.assert_current()
    finally:
        marker.reset(token)
    assert observed and all(value is None for value in observed)


async def test_cancelled_fence_joins_worker_and_removes_private_sort_files(tmp_path, monkeypatch):
    _, captured = await prepare(tmp_path)
    entered, release = threading.Event(), threading.Event()
    original_stat = os.stat

    def information(path, *args, **kwargs):
        if path == "module.py":
            entered.set()
            assert release.wait(2), "Fixture must release the controlled metadata stat"
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", information)
    task = asyncio.create_task(captured.assert_current())
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done(), "Cancellation returned before the worker could close its descriptors"
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert list((tmp_path / "catalog").iterdir()) == []
