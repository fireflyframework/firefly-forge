# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Index CAS remains complete without a project-sized metadata read buffer."""

import hashlib
import importlib
import importlib.util
import os

import pytest


def snapshot_module():
    name = "firefly_forge.source_snapshot"
    assert importlib.util.find_spec(name) is not None, "Streamed source snapshot metadata is missing"
    return importlib.import_module(name)


def index_workspace(tmp_path, data=b"public index fixture"):
    root = tmp_path / "workspace"
    (root / ".git").mkdir(parents=True)
    (root / ".git/index").write_bytes(data)
    return root


def test_index_larger_than_old_limit_has_complete_content_digest(tmp_path):
    data = b"public-index-record\0" * 100000
    root = index_workspace(tmp_path, data)
    identity = snapshot_module().git_index_identity(root)
    assert identity.sha256 == hashlib.sha256(data).hexdigest()
    assert identity.byte_count == 2_000_000
    assert str(root) not in repr(identity)


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo"])
def test_index_special_files_or_shared_links_are_not_admitted(tmp_path, kind):
    root = index_workspace(tmp_path)
    index = root / ".git/index"
    index.unlink()
    other = tmp_path / "other"
    other.write_text("public fixture")
    if kind == "symlink":
        index.symlink_to(other)
    elif kind == "hardlink":
        os.link(other, index)
    else:
        os.mkfifo(index)
    with pytest.raises(ValueError, match="index"):
        snapshot_module().git_index_identity(root)


@pytest.mark.parametrize("swap", [False, True])
def test_index_rewrite_or_replacement_during_read_cannot_produce_a_cas_identity(tmp_path, monkeypatch, swap):
    root = index_workspace(tmp_path, b"public fixture" * 20000)
    module = snapshot_module()
    read = os.read
    changed = False

    def mutation(fd, size):
        nonlocal changed
        value = read(fd, size)
        if value and not changed:
            changed = True
            index = root / ".git/index"
            if swap:
                index.rename(root / ".git/old-index")
            index.write_bytes(b"changed public fixture")
        return value

    monkeypatch.setattr(module.os, "read", mutation)
    with pytest.raises(ValueError, match="index"):
        module.git_index_identity(root)


def test_artifact_root_is_private_per_run_and_cannot_follow_a_symlink(tmp_path):
    module = snapshot_module()
    root = module.diff_artifact_root(tmp_path, "1" * 32)
    assert root == tmp_path / ".diff-artifacts" / ("1" * 32)
    assert root.stat().st_mode & 0o777 == 0o700
    root.rmdir()
    target = tmp_path / "other"
    target.mkdir()
    root.symlink_to(target)
    with pytest.raises(ValueError, match="artifact"):
        module.diff_artifact_root(tmp_path, "1" * 32)


@pytest.mark.parametrize("value", [True, 0, -1, float("inf"), float("nan")])
def test_index_hashing_requires_an_explicit_finite_positive_time_budget(tmp_path, value):
    root = index_workspace(tmp_path)
    with pytest.raises(ValueError, match="budget"):
        snapshot_module().git_index_identity(root, timeout_seconds=value)


def test_only_explicit_typed_diff_budgets_intersect_and_default_remains_elastic():
    from firefly_forge.git_diff_models import GitDiffBudget

    module = snapshot_module()
    assert module.diff_budget_for_run({}).max_bytes is None
    assert module.diff_budget_for_run({"runner": {"max_output_bytes": 1, "workspace_max_bytes": 1}}).max_bytes is None
    project = GitDiffBudget(max_bytes=4000, min_free_bytes=400, timeout_seconds=80, preview_bytes=3000)
    runner = GitDiffBudget(max_bytes=2000, min_free_bytes=800, timeout_seconds=40, preview_bytes=1000)
    merged = module.intersect_diff_budgets(project, runner)
    assert (merged.max_bytes, merged.min_free_bytes, merged.timeout_seconds, merged.preview_bytes) == (
        2000,
        800,
        40,
        1000,
    )
    assert module.diff_budget_for_run({"git_delivery_policy": {"diff_budget": project.model_dump()}}, runner) == merged
    with pytest.raises(ValueError, match="typed"):
        module.intersect_diff_budgets(project, {"max_bytes": 1})
