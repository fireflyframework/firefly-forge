# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Git metadata fencing before trusted checkpoint and publication operations."""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path


def trusted_git(argv: list[str]) -> list[str]:
    if not argv or argv[0] != "git":
        return argv
    return [
        "git",
        "--no-replace-objects",
        "-c",
        "core.hooksPath=" + os.devnull,
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.attributesFile=" + os.devnull,
        "-c",
        "protocol.ext.allow=never",
        *argv[1:],
    ]


def git_environment(env: dict[str, str] | None = None) -> dict[str, str]:
    return {**(env or {}), "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}


def metadata_digest(workspace: Path) -> str:
    return _metadata_digest(workspace)


def metadata_digest_before_exclude(workspace: Path, original_exclude: bytes) -> str:
    """Project only the known exclude transition; preserve every other byte and mode fence."""
    if type(original_exclude) is not bytes or len(original_exclude) > 65536:
        raise ValueError("Original Git exclude projection requires bounded immutable bytes")
    return _metadata_digest(workspace, original_exclude=original_exclude)


def _metadata_digest(workspace: Path, *, original_exclude: bytes | None = None) -> str:
    metadata = workspace / ".git"
    if workspace.is_symlink() or metadata.is_symlink() or not metadata.is_dir():
        raise ValueError("Git metadata must be an owned directory")
    inspected = 0
    for name in ("HEAD", "index", "packed-refs", "objects", "refs", "logs"):
        path = metadata / name
        entries = [path] if path.exists() or path.is_symlink() else []
        if path.is_dir() and not path.is_symlink():
            for root, directories, names in os.walk(path, followlinks=False):
                entries.extend(Path(root) / entry for entry in [*directories, *names])
                if len(entries) > 100_000:
                    raise ValueError("Git metadata exceeds the inspection bound")
        for entry in entries:
            info = entry.lstat()
            inspected += 1
            if inspected > 100_000:
                raise ValueError("Git metadata exceeds the inspection bound")
            if not stat.S_ISDIR(info.st_mode) and (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1):
                raise ValueError("Git metadata cannot redirect through symlinks, hardlinks or special files")
    files: list[Path] = []
    for name in (
        "config",
        "config.worktree",
        "packed-refs",
        "hooks",
        "info",
        "objects/info",
        "refs/replace",
        "shallow",
        "commondir",
        "worktrees",
    ):
        path = metadata / name
        if path.is_symlink():
            raise ValueError("Git metadata cannot redirect through symlinks")
        if path.is_dir():
            for root, directories, names in os.walk(path, followlinks=False):
                if any((Path(root) / directory).is_symlink() for directory in directories):
                    raise ValueError("Git metadata cannot redirect through symlinks")
                files.extend(Path(root) / item for item in names)
                if len(files) > 300:
                    raise ValueError("Git metadata exceeds the inspection bound")
        elif path.exists():
            files.append(path)
    result, size = hashlib.sha256(), 0
    projected = False
    for path in sorted(files):
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError("Git metadata must contain owned regular files")
            content = stream.read(1_000_001)
        size += len(content)
        if size > 1_000_000:
            raise ValueError("Git metadata exceeds the inspection bound")
        if original_exclude is not None and path.relative_to(metadata).as_posix() == "info/exclude":
            content = original_exclude
            projected = True
        result.update(path.relative_to(metadata).as_posix().encode() + b"\x00")
        result.update(str(stat.S_IMODE(info.st_mode)).encode() + b"\x00" + content + b"\x00")
    if original_exclude is not None and not projected:
        raise ValueError("Original Git exclude projection requires the existing protected file")
    return result.hexdigest()
