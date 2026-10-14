# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Scoped Git metadata is synthesized from sanitized bytes, without host configuration/history."""

import importlib
import io
import os
import sys
import tarfile
from pathlib import Path

import pytest

from firefly_forge.runner_backends import _bounded_process
from firefly_forge.sandbox import pack_workspace, unpack_workspace


async def test_fixed_git_process_environment_cannot_inherit_host_configuration(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_PARAMETERS", "host-private-git-configuration")
    code, output = await _bounded_process(
        [sys.executable, "-c", "import os;print(os.environ.get('GIT_CONFIG_PARAMETERS','absent'))"],
        tmp_path,
        3,
        1024,
        environment={"PATH": os.defpath},
    )
    assert code == 0 and output == b"absent\n"


async def test_generated_git_snapshot_has_exact_source_modes_and_no_host_hooks_filters_or_history(
    tmp_path, monkeypatch
):
    try:
        module = importlib.import_module("firefly_forge.runner_git_snapshot")
    except ModuleNotFoundError:
        pytest.fail("Confined generated Git metadata is not implemented")
    source = tmp_path / "source"
    source.mkdir()
    (source / "main.txt").write_text("original source\n")
    (source / ".gitattributes").write_text("*.txt filter=unsafe\n")
    (source / "script.sh").write_text("#!/bin/sh\nexit 0\n")
    (source / "script.sh").chmod(0o755)
    (source / ".env").write_text("PRIVATE=excluded\n")
    original_git = source / ".git"
    original_git.mkdir()
    (original_git / "host-private-history").write_text("host private repository material")
    global_config = tmp_path / "host-config"
    marker = tmp_path / "host-hook-ran"
    global_config.write_text('[filter "unsafe"]\n clean = touch ' + str(marker) + "\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))
    raw, _ = pack_workspace(source, 1_048_576)
    builder = module.RunnerGitSnapshot(Path("/usr/bin/git"))
    metadata = await builder.build(raw, tmp_path, 1_048_576)
    received = tmp_path / "received"
    unpack_workspace(raw, received, 1_048_576)
    unpack_workspace(metadata, received / ".git", 1_048_576)
    environment = {"PATH": os.defpath, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null"}
    code, status = await _bounded_process(
        ["/usr/bin/git", "status", "--porcelain"], received, 5, 4096, environment=environment
    )
    assert code == 0 and status == b""
    (received / "main.txt").write_text("returned source\n")
    code, diff = await _bounded_process(
        ["/usr/bin/git", "diff", "--no-ext-diff", "--", "main.txt"], received, 5, 4096, environment=environment
    )
    assert code == 0 and b"-original source" in diff and b"+returned source" in diff
    assert not marker.exists() and not (received / ".git" / "host-private-history").exists()
    assert not (received / ".env").exists()
    with tarfile.open(fileobj=io.BytesIO(metadata), mode="r:") as stream:
        names = {member.name for member in stream}
    assert not any(name.startswith("hooks/") or name.endswith("alternates") for name in names)
    assert "host private" not in (received / ".git" / "config").read_text()


async def test_git_snapshot_cannot_reintroduce_private_files_from_an_untrusted_archive(tmp_path):
    module = importlib.import_module("firefly_forge.runner_git_snapshot")
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w", format=tarfile.USTAR_FORMAT) as stream:
        item = tarfile.TarInfo(".env")
        item.size = 4
        stream.addfile(item, io.BytesIO(b"evil"))
    with pytest.raises(ValueError, match="sanitized"):
        await module.RunnerGitSnapshot(Path("/usr/bin/git")).build(output.getvalue(), tmp_path, 1_048_576)
    assert not list(tmp_path.glob(".forge-git-snapshot-*"))


async def test_controller_validates_only_reconstructed_minimal_git_closure(tmp_path):
    module = importlib.import_module("firefly_forge.runner_git_snapshot")
    source = tmp_path / "source"
    source.mkdir()
    (source / "main.txt").write_text("approved source\n")
    raw, _ = pack_workspace(source, 1_048_576)
    builder = module.RunnerGitSnapshot(Path("/usr/bin/git"))
    frozen = await builder.build(raw, tmp_path, 1_048_576)
    rebuilt = await builder.validate(raw, frozen, tmp_path, 1_048_576)
    assert rebuilt == frozen
    poisoned = io.BytesIO()
    with (
        tarfile.open(fileobj=io.BytesIO(frozen), mode="r:") as original,
        tarfile.open(fileobj=poisoned, mode="w", format=tarfile.USTAR_FORMAT) as target,
    ):
        for member in original:
            payload = original.extractfile(member) if member.isfile() else None
            data = payload.read() if payload else None
            if member.name == "config":
                data = b'[core]\n hooksPath = /outside/hooks\n[remote "origin"]\n url = https://private-token@example.invalid/repo\n'
                member.size = len(data)
            target.addfile(member, io.BytesIO(data) if data is not None else None)
    with pytest.raises(ValueError, match="minimum Git"):
        await builder.validate(raw, poisoned.getvalue(), tmp_path, 1_048_576)
    assert not list(tmp_path.glob(".forge-git-snapshot-*"))
