# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Real local Git source selection, without providers or installed dependencies."""

import os
import subprocess
import tracemalloc

import pytest

from firefly_forge.agentic_checkpoint_invocation import workspace_cut
from firefly_forge.source_diff_admission import SourceDiffAdmission


def git(root, *args):
    return subprocess.check_output(["git", *args], cwd=root, stderr=subprocess.STDOUT).decode().strip()


def repository(tmp_path):
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.name", "Controlled Source")
    git(tmp_path, "config", "user.email", "source@example.invalid")
    (tmp_path / ".gitignore").write_text("node_modules/\ngenerated/\n")
    (tmp_path / "package.json").write_text('{"name":"controlled-source","private":true}\n')
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-qm", "Seed source")
    return tmp_path


class SourceStore:
    def __init__(self, *, secret=None, observe=None):
        self.secret, self.observe = secret, observe
        self.checks = 0

    def credential_registry(self):
        return {"TOKEN": self.secret} if self.secret else {}

    async def source_patch_scanner(self, **kwargs):
        async def verify():
            self.checks += 1
            if self.observe:
                self.observe()

        return SourceDiffAdmission(
            credentials=tuple(self.credential_registry().values()), matcher=None, packets=(), verify_current=verify
        )


async def test_checkpoint_source_survives_real_npm_style_ignored_dependency_links(tmp_path):
    root = repository(tmp_path)
    (root / "package-lock.json").write_text('{"lockfileVersion":3,"packages":{}}\n')
    clean = await workspace_cut(SourceStore(), root)
    package = root / "node_modules" / "typescript" / "bin"
    package.mkdir(parents=True)
    (package / "tsc").write_text("#!/usr/bin/env node\n")
    bins = root / "node_modules" / ".bin"
    bins.mkdir()
    (bins / "tsc").symlink_to("../typescript/bin/tsc")
    assert git(root, "check-ignore", "node_modules/.bin/tsc") == "node_modules/.bin/tsc"
    assert await workspace_cut(SourceStore(), root) == clean
    (root / "package-lock.json").write_text('{"lockfileVersion":3,"packages":{"x":{}}}\n')
    assert (await workspace_cut(SourceStore(), root))["source"] != clean["source"]


async def test_checkpoint_source_streams_large_valid_source_and_keeps_tracked_ignored_assets(tmp_path):
    root = repository(tmp_path)
    asset = root / "generated" / "branding.bin"
    asset.parent.mkdir()
    with asset.open("wb") as output:
        for _ in range(300):
            output.write(b"public-branding\x00" * 4096)
    git(root, "add", "-f", "generated/branding.bin")
    store = SourceStore()
    assert asset.stat().st_size > 16_777_216
    tracemalloc.start()
    try:
        before = await workspace_cut(store, root)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 4 * 1024 * 1024
    with asset.open("ab") as output:
        output.write(b"real-asset-change")
    assert (await workspace_cut(store, root))["source"] != before["source"]
    assert store.checks >= 2


@pytest.mark.parametrize("tracked", [False, True])
async def test_checkpoint_source_refuses_selected_source_symlinks_even_when_tracked_ignored(tmp_path, tracked):
    root = repository(tmp_path)
    link = root / ("generated/source-link" if tracked else "source-link")
    link.parent.mkdir(exist_ok=True)
    link.symlink_to(root / "package.json")
    if tracked:
        git(root, "add", "-f", str(link.relative_to(root)))
    with pytest.raises(ValueError, match="regular|link"):
        await workspace_cut(SourceStore(), root)


@pytest.mark.parametrize("name", [".env", "credentials.json"])
async def test_checkpoint_source_rejects_tracked_private_paths(tmp_path, name):
    root = repository(tmp_path)
    (root / name).write_text("not-a-real-credential")
    git(root, "add", "-f", name)
    with pytest.raises(ValueError, match="private"):
        await workspace_cut(SourceStore(), root)


async def test_checkpoint_source_rejects_configured_credential_and_source_hardlink(tmp_path):
    root = repository(tmp_path)
    (root / "source.txt").write_text("fictional-source-test-token")
    with pytest.raises(ValueError, match="credential"):
        await workspace_cut(SourceStore(secret="fictional-source-test-token"), root)
    (root / "source.txt").write_text("public source")
    os.link(root / "source.txt", root / "alias.txt")
    with pytest.raises(ValueError, match="link|regular"):
        await workspace_cut(SourceStore(), root)


async def test_checkpoint_source_excludes_only_git_ignored_untracked_outputs(tmp_path):
    root = repository(tmp_path)
    before = await workspace_cut(SourceStore(), root)
    (root / "generated").mkdir()
    (root / "generated/output.txt").write_text("ignored build output")
    assert await workspace_cut(SourceStore(), root) == before
    (root / "new-source.txt").write_text("untracked source is included")
    assert (await workspace_cut(SourceStore(), root))["source"] != before["source"]


async def test_checkpoint_source_final_admission_rejects_source_change(tmp_path):
    root = repository(tmp_path)
    store = SourceStore(observe=lambda: (root / "package.json").write_text("changed during final admission"))
    with pytest.raises(ValueError, match="changed"):
        await workspace_cut(store, root)


async def test_checkpoint_source_requires_git_and_leaves_transport_archive_strict(tmp_path):
    from firefly_forge.sandbox import pack_workspace

    with pytest.raises((ValueError, RuntimeError)):
        await workspace_cut(SourceStore(), tmp_path)
    root = repository(tmp_path)
    (root / "node_modules").mkdir()
    (root / "node_modules/bin").symlink_to("../package.json")
    with pytest.raises(ValueError, match="regular"):
        pack_workspace(root, 65536)


@pytest.mark.parametrize("change", ["new_file", "index", "head", "ignore_rules"])
async def test_checkpoint_source_final_admission_fences_inventory_and_git_metadata(tmp_path, change):
    root = repository(tmp_path)

    def alter():
        if change == "new_file":
            (root / "late-source.txt").write_text("late source")
        elif change == "index":
            git(root, "update-index", "--chmod=+x", "package.json")
        elif change == "head":
            git(root, "commit", "--allow-empty", "-qm", "Different revision")
        else:
            (root / ".gitignore").write_text("*.json\n")

    with pytest.raises(ValueError, match="changed"):
        await workspace_cut(SourceStore(observe=alter), root)


async def test_checkpoint_source_tracks_deletion_and_untracked_names_with_tabs(tmp_path):
    root = repository(tmp_path)
    before = await workspace_cut(SourceStore(), root)
    (root / "package.json").unlink()
    deleted = await workspace_cut(SourceStore(), root)
    assert deleted["source"] != before["source"]
    (root / "100644 literal 0\tnew-source.txt").write_text("literal source")
    assert (await workspace_cut(SourceStore(), root))["source"] != deleted["source"]


async def test_checkpoint_source_inventory_does_not_truncate_after_two_thousand_paths(tmp_path):
    from firefly_forge.checkpoint_source_identity import stream_source_entries

    root = repository(tmp_path)
    for ordinal in range(2003):
        (root / f"source-{ordinal:04d}.txt").touch()
    observed = []

    async def collect(entry):
        observed.append(entry.path)

    await stream_source_entries(root, collect)
    assert len(observed) == 2005
    assert "source-2002.txt" in observed


async def test_checkpoint_source_includes_public_environment_template(tmp_path):
    root = repository(tmp_path)
    before = await workspace_cut(SourceStore(), root)
    (root / ".env.example").write_text("PUBLIC_API_URL=https://example.invalid\n")
    git(root, "add", ".env.example")
    assert (await workspace_cut(SourceStore(), root))["source"] != before["source"]


async def test_checkpoint_source_final_wait_cannot_add_source_in_an_empty_nested_directory(tmp_path):
    root = repository(tmp_path)
    empty = root / "unused" / "nested"
    empty.mkdir(parents=True)
    store = SourceStore(observe=lambda: (empty / "late-source.txt").write_text("late selected source"))
    with pytest.raises(ValueError, match="changed"):
        await workspace_cut(store, root)
