# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Delivery cleanliness uses the same bounded generated-result exclusions as staging."""

import hashlib
import os

import pytest
from test_git_delivery_local import delivery_case as delivery_case
from test_git_delivery_local import git

from firefly_forge.git_workspace import metadata_digest

CACHE = "frontend/.cache/vite/vitest/" + "a" * 40 + "/results.json"


def checkpoint(case):
    return {"revision": git(case.root, "rev-parse", "HEAD"), "source_tree": git(case.root, "rev-parse", "HEAD^{tree}")}


def result_cache(case):
    path = case.root / CACHE
    path.parent.mkdir(parents=True)
    path.write_text('{"success":true}\n')
    return path


async def test_generated_vitest_result_does_not_dirty_checkpoint_or_mutate_source(delivery_case):
    case = delivery_case()
    receipt = checkpoint(case)
    path = result_cache(case)
    status = git(case.root, "status", "--porcelain", "--untracked-files=all")
    before = path.read_bytes(), hashlib.sha256((case.root / ".git/index").read_bytes()).hexdigest()
    assert CACHE in status

    await case.service._assert_checkpoint_source(case.state["run"], receipt)

    assert path.read_bytes() == before[0]
    assert hashlib.sha256((case.root / ".git/index").read_bytes()).hexdigest() == before[1]
    assert git(case.root, "status", "--porcelain", "--untracked-files=all") == status
    assert metadata_digest(case.root) == case.state["run"]["git_metadata_digest"]


@pytest.mark.parametrize(
    "name",
    [
        "feature.py",
        "frontend/.cache/source.ts",
        "frontend/.cache/vite/vitest/short/results.json",
        "frontend/.cache/vite/vitest/" + "a" * 40 + "/source.ts",
        "line\nbreak.py",
    ],
)
async def test_generated_result_does_not_hide_genuine_untracked_source(delivery_case, name):
    case = delivery_case()
    receipt = checkpoint(case)
    result_cache(case)
    source = case.root / name
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("public source\n")
    with pytest.raises(ValueError, match="Checkpoint source"):
        await case.service._assert_checkpoint_source(case.state["run"], receipt)
    assert source.read_text() == "public source\n"
    assert not git(case.root, "diff", "--cached", "--name-only")


@pytest.mark.parametrize("staged", [False, True])
async def test_generated_result_does_not_hide_tracked_modifications(delivery_case, staged):
    case = delivery_case()
    receipt = checkpoint(case)
    result_cache(case)
    (case.root / "product.py").write_text("value = 2\n")
    if staged:
        git(case.root, "add", "product.py")
    index = git(case.root, "ls-files", "--stage"), git(case.root, "diff", "--cached", "--binary")
    with pytest.raises(ValueError, match="Checkpoint source"):
        await case.service._assert_checkpoint_source(case.state["run"], receipt)
    assert (case.root / "product.py").read_text() == "value = 2\n"
    assert (git(case.root, "ls-files", "--stage"), git(case.root, "diff", "--cached", "--binary")) == index


@pytest.mark.parametrize("change", ["modify", "delete"])
async def test_tracked_baseline_result_cache_remains_protected_source(delivery_case, change):
    case = delivery_case()
    path = result_cache(case)
    git(case.root, "add", CACHE)
    git(case.root, "commit", "-qm", "Keep a deliberate tracked baseline fixture")
    case.state["run"].update(
        base_revision=git(case.root, "rev-parse", "HEAD"), git_metadata_digest=metadata_digest(case.root)
    )
    receipt = checkpoint(case)
    if change == "modify":
        path.write_text('{"deliberate_source":true}\n')
    else:
        path.unlink()
    with pytest.raises(ValueError, match="Checkpoint source"):
        await case.service._assert_checkpoint_source(case.state["run"], receipt)


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "oversized", "staged"])
async def test_only_bounded_untracked_unaliased_results_can_be_excluded(delivery_case, kind):
    case = delivery_case()
    receipt = checkpoint(case)
    path = case.root / CACHE
    path.parent.mkdir(parents=True)
    if kind == "symlink":
        path.symlink_to(case.root / "product.py")
    elif kind == "hardlink":
        os.link(case.root / "product.py", path)
    else:
        path.write_bytes(b"x" * (131073 if kind == "oversized" else 1))
        if kind == "staged":
            git(case.root, "add", CACHE)
    with pytest.raises((ValueError, OSError)):
        await case.service._assert_checkpoint_source(case.state["run"], receipt)
    assert path.exists()


async def test_cache_staged_during_cleanliness_read_is_not_hidden_by_earlier_exclusion(delivery_case, monkeypatch):
    case = delivery_case()
    receipt = checkpoint(case)
    path = result_cache(case)
    checked = case.module.checked

    async def staged_before_status(argv, *args, **kwargs):
        if argv[:2] == ["git", "status"]:
            git(case.root, "add", CACHE)
        return await checked(argv, *args, **kwargs)

    monkeypatch.setattr(case.module, "checked", staged_before_status)
    with pytest.raises(ValueError):
        await case.service._assert_checkpoint_source(case.state["run"], receipt)
    assert path.read_text() == '{"success":true}\n'
    assert CACHE in git(case.root, "diff", "--cached", "--name-only")
