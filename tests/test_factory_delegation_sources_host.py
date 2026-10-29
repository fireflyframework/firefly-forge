# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""A child starts from the parent's public bytes without sharing writable files."""

import hashlib
from pathlib import Path

import pytest

from firefly_forge.delegation_sources import capture_child_source, verify_child_source
from firefly_forge.process import checked
from firefly_forge.workspace_tools import WorkspaceTools


async def parent(tmp_path):
    root = tmp_path / "parent"
    root.mkdir()
    (root / "tracked.txt").write_text("committed\n")
    (root / "deleted.txt").write_text("remove me\n")
    await checked(["git", "init", "--initial-branch=main"], root)
    await checked(["git", "add", "."], root)
    await checked(
        ["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-m", "Baseline"], root
    )
    (root / "tracked.txt").write_text("parent pending bytes\n")
    (root / "deleted.txt").unlink()
    (root / "new.bin").write_bytes(b"\x00\xff")
    (root / "run.sh").write_text("#!/bin/sh\nexit 0\n")
    (root / "run.sh").chmod(0o755)
    return root


async def test_child_snapshot_includes_dirty_untracked_binary_mode_and_deletions(tmp_path):
    root = await parent(tmp_path)
    metadata = {
        p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in (root / ".git").rglob("*")
        if p.is_file()
    }
    snapshot = await capture_child_source(WorkspaceTools(root), tmp_path / "snapshot")
    assert snapshot["files"]["tracked.txt"]["sha256"] == hashlib.sha256(b"parent pending bytes\n").hexdigest()
    assert snapshot["files"]["new.bin"]["sha256"] == hashlib.sha256(b"\x00\xff").hexdigest()
    assert snapshot["files"]["run.sh"]["executable"] is True
    assert "deleted.txt" not in snapshot["files"]
    await verify_child_source(snapshot)
    child = tmp_path / "child"
    await checked(["git", "clone", "--no-hardlinks", str(tmp_path / "snapshot"), str(child)], tmp_path)
    assert (child / "new.bin").read_bytes() == b"\x00\xff"
    assert not (child / "deleted.txt").exists()
    (child / "tracked.txt").write_text("child edit\n")
    assert (root / "tracked.txt").read_text() == "parent pending bytes\n"
    assert {
        p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in (root / ".git").rglob("*")
        if p.is_file()
    } == metadata


async def test_snapshot_refuses_existing_destination_and_linked_source(tmp_path):
    root = await parent(tmp_path)
    destination = tmp_path / "snapshot"
    destination.mkdir()
    with pytest.raises(ValueError, match="fresh"):
        await capture_child_source(WorkspaceTools(root), destination)
    (root / "linked.txt").symlink_to(root / "tracked.txt")
    with pytest.raises(ValueError):
        await capture_child_source(WorkspaceTools(root), tmp_path / "other")


async def test_source_mutation_during_copy_is_refused_before_admission(tmp_path, monkeypatch):
    root = await parent(tmp_path)
    tools = WorkspaceTools(root)
    original = tools._inventory
    calls = 0

    async def changing(prefix=""):
        nonlocal calls
        calls += 1
        if calls == 2:
            (root / "tracked.txt").write_text("raced bytes\n")
        return await original(prefix)

    monkeypatch.setattr(tools, "_inventory", changing)
    with pytest.raises(ValueError, match="changed"):
        await capture_child_source(tools, tmp_path / "snapshot")


async def test_sealed_snapshot_rejects_rehashed_file_or_git_drift(tmp_path):
    root = await parent(tmp_path)
    snapshot = await capture_child_source(WorkspaceTools(root), tmp_path / "snapshot")
    (Path(snapshot["path"]) / "tracked.txt").write_text("different\n")
    with pytest.raises(ValueError):
        await verify_child_source(snapshot)
