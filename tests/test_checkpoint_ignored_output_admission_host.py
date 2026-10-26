# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Ignored output churn must not impersonate a selected source mutation."""

import pytest
from test_checkpoint_source_identity_host import SourceStore, git, repository

from firefly_forge.agentic_checkpoint_invocation import workspace_cut


@pytest.mark.parametrize("change", ["nested_cache", "new_ignored_root", "private_runner_root"])
async def test_final_admission_retains_exact_source_during_ignored_output_churn(tmp_path, change):
    root = repository(tmp_path)
    (root / ".git/info/exclude").write_text(".forge-sandbox-results/\n")
    if change == "nested_cache":
        (root / "node_modules/.cache/vite").mkdir(parents=True)
    expected = await workspace_cut(SourceStore(), root)
    changed = False

    def churn():
        nonlocal changed
        if changed:
            return
        changed = True
        name = {
            "nested_cache": "node_modules/.cache/vite/result.json",
            "new_ignored_root": "generated/cache/result.json",
            "private_runner_root": ".forge-sandbox-results/command/result.json",
        }[change]
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"exit_code":0}\n')
        assert git(root, "check-ignore", name) == name

    observed = await workspace_cut(SourceStore(observe=churn), root)
    assert changed
    assert observed == expected


@pytest.mark.parametrize(
    "change",
    [
        "selected_file",
        "new_selected",
        "deleted_selected",
        "ignore_policy",
        "index",
        "tracked_ignored",
        "selected_link",
        "directory_mode",
    ],
)
async def test_ignored_churn_never_masks_selected_source_or_identity_change(tmp_path, change):
    root = repository(tmp_path)
    directory = root / "source/nested"
    directory.mkdir(parents=True)
    selected = directory / "module.py"
    selected.write_text("VALUE = 1\n")
    ignored = root / "generated/force.txt"
    ignored.parent.mkdir()
    ignored.write_text("force-tracked source\n")
    git(root, "add", "-f", "generated/force.txt")
    changed = False

    def mutate():
        nonlocal changed
        if changed:
            return
        changed = True
        (root / "generated/cache").mkdir()
        if change == "selected_file":
            selected.write_text("VALUE = 2\n")
        elif change == "new_selected":
            (directory / "late.py").write_text("late selected source\n")
        elif change == "deleted_selected":
            selected.unlink()
        elif change == "ignore_policy":
            (root / ".gitignore").write_text("node_modules/\ngenerated/\nsource/\n")
        elif change == "index":
            git(root, "update-index", "--chmod=+x", "package.json")
        elif change == "tracked_ignored":
            ignored.write_text("changed tracked source\n")
        elif change == "selected_link":
            selected.unlink()
            selected.symlink_to(root / "package.json")
        else:
            directory.chmod(0o700)

    with pytest.raises(ValueError, match="changed|regular|link"):
        await workspace_cut(SourceStore(observe=mutate), root)
    assert changed


@pytest.mark.parametrize("ignored", [False, True])
async def test_final_namespace_refresh_refuses_tracked_deleted_source_reappearance(tmp_path, ignored):
    root = repository(tmp_path)
    name = "generated/force.txt" if ignored else "deleted.py"
    selected = root / name
    selected.parent.mkdir(exist_ok=True)
    selected.write_text("original source\n")
    git(root, "add", "-f", name)
    selected.unlink()
    changed = False

    def reappear():
        nonlocal changed
        if not changed:
            changed = True
            selected.write_text("reappeared source\n")

    with pytest.raises(ValueError, match="changed"):
        await workspace_cut(SourceStore(observe=reappear), root)


@pytest.mark.parametrize(
    "name",
    ["node_modules/.cache/result.json", "generated/cache/result.json", ".forge-sandbox-results/command/result.json"],
)
async def test_background_output_writer_cannot_change_the_selected_source_cut(tmp_path, name):
    import asyncio

    from firefly_forge.source_diff_admission import SourceDiffAdmission

    root = repository(tmp_path)
    (root / ".git/info/exclude").write_text(".forge-sandbox-results/\n")
    expected = await workspace_cut(SourceStore(), root)
    ready, written = asyncio.Event(), asyncio.Event()

    class ConcurrentStore:
        async def source_patch_scanner(self):
            async def verify():
                ready.set()
                await written.wait()

            return SourceDiffAdmission(credentials=(), matcher=None, packets=(), verify_current=verify)

    async def writer():
        await ready.wait()
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("controlled runner output\n")
        written.set()

    background = asyncio.create_task(writer())
    try:
        assert await workspace_cut(ConcurrentStore(), root) == expected
    finally:
        background.cancel()
        await asyncio.gather(background, return_exceptions=True)


async def test_refresh_never_admits_source_hidden_by_transient_git_exclude(tmp_path, monkeypatch):
    from firefly_forge import checkpoint_source_identity as source

    root = repository(tmp_path)
    policy = root / ".git/info/exclude"
    original = policy.read_bytes()
    changed = False

    def add_source():
        nonlocal changed
        if not changed:
            changed = True
            (root / "late-selected.py").write_text("late selected source\n")

    ignored = source._ignored_directories
    inventory = source._inventory_identity
    calls = 0

    async def change_policy(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            policy.write_bytes(original + b"\nlate-selected.py\n")
        return await ignored(*args, **kwargs)

    async def restore_policy(*args, **kwargs):
        result = await inventory(*args, **kwargs)
        policy.write_bytes(original)
        return result

    monkeypatch.setattr(source, "_ignored_directories", change_policy)
    monkeypatch.setattr(source, "_inventory_identity", restore_policy)
    with pytest.raises(ValueError, match="changed"):
        await workspace_cut(SourceStore(observe=add_source), root)
