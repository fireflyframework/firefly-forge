# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Explicit native reconciliation of legacy factory dependency-ignore preparation."""

from __future__ import annotations

import hashlib
import os
import re
import stat
import subprocess
import uuid
from pathlib import Path
from typing import Any

from firefly_forge.git_workspace import git_environment, metadata_digest, trusted_git
from firefly_forge.verification_models import verification_source

Data = dict[str, Any]
DEPENDENCIES = frozenset({".venv", "node_modules", "vendor"})
IGNORE_POLICY = b"\n.venv\nnode_modules\nvendor\n"


def _git(workspace: Path, *args: str) -> bytes:
    result = subprocess.run(
        trusted_git(["git", *args]),
        cwd=workspace,
        env={"PATH": os.defpath, "HOME": os.devnull, **git_environment()},
        capture_output=True,
        timeout=10,
        check=False,
    )
    if result.returncode or len(result.stdout) > 4 * 1024 * 1024:
        raise ValueError("The bounded native Git reconciliation read failed")
    return result.stdout


def _regular(path: Path, maximum: int) -> bytes:
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as stream:
        value = os.fstat(stream.fileno())
        if not stat.S_ISREG(value.st_mode) or value.st_nlink != 1 or value.st_size > maximum:
            raise ValueError("Reconciliation requires bounded owned regular Git metadata")
        data = stream.read(maximum + 1)
    if len(data) > maximum:
        raise ValueError("Reconciliation Git metadata exceeds its read bound")
    return data


def dependency_plan(workspace: Path, base: str, metadata: str, *, component: str) -> Data:
    """Inspect the proposed environment delta without staging or following dependencies."""
    from firefly_forge.engine import private_path

    scope = Path(component)
    if (
        scope.is_absolute()
        or ".." in scope.parts
        or not re.fullmatch(r"[a-f0-9]{40}|[a-f0-9]{64}", base)
        or metadata_digest(workspace) != metadata
        or _git(workspace, "rev-parse", "HEAD").strip().decode() != base
    ):
        raise ValueError("The retained source scope, base or trusted Git metadata changed")
    index = _regular(workspace / ".git/index", 1024 * 1024)
    exclude = _regular(workspace / ".git/info/exclude", 65536)
    if len(exclude) + len(IGNORE_POLICY) > 65536:
        raise ValueError("The legacy ignore transition exceeds its finite metadata bound")
    patterns = exclude.decode().splitlines()
    baseline = set()
    for record in _git(workspace, "ls-tree", "-r", "-z", "--full-tree", base).split(b"\0"):
        if record:
            _, raw_name = record.split(b"\t", 1)
            path = raw_name.decode()
            if private_path(path):
                raise ValueError("Tracked private source cannot be hidden by environment reconciliation")
            baseline.add(path)
    aliases, identities = [], {}
    for record in _git(workspace, "ls-files", "--stage", "-z").split(b"\0"):
        if not record:
            continue
        entry, raw_path = record.split(b"\t", 1)
        mode, _, stage = entry.split(b" ")
        path = raw_path.decode()
        relative = Path(path)
        if stage != b"0" or relative.is_absolute() or ".." in relative.parts:
            raise ValueError("The native index contains unresolved or foreign source")
        if not private_path(path):
            continue
        parent = workspace
        for part in relative.parts[:-1]:
            parent /= part
            if not stat.S_ISDIR(parent.lstat().st_mode):
                raise ValueError("Dependency reconciliation cannot follow a redirected source directory")
        value = (workspace / relative).lstat()
        name = relative.name
        if (
            path in baseline
            or name not in DEPENDENCIES
            or private_path(str(relative.parent))
            or not relative.is_relative_to(scope)
            or mode != b"120000"
            or not stat.S_ISLNK(value.st_mode)
            or name + "/" not in patterns
            or name in patterns
        ):
            raise ValueError("Only a legacy staged dependency symlink can be reconciled")
        aliases.append(path)
        identities[path] = {
            "device": value.st_dev,
            "inode": value.st_ino,
            "mode": value.st_mode,
            "size": value.st_size,
            "mtime_ns": value.st_mtime_ns,
        }
        if len(aliases) > 100:
            raise ValueError("Legacy dependency reconciliation exceeds its finite alias bound")
    if not aliases:
        raise ValueError("The retained workspace has no supported legacy preparation defect")
    if (
        metadata_digest(workspace) != metadata
        or _regular(workspace / ".git/index", 1024 * 1024) != index
        or _git(workspace, "rev-parse", "HEAD").strip().decode() != base
    ):
        raise ValueError("The native source admission changed during reconciliation review")
    return {
        "version": 1,
        "kind": "dependency_ignore_policy_v1",
        "base_revision": base,
        "component": component,
        "aliases": sorted(aliases),
        "alias_identities": identities,
        "metadata_before": metadata,
        "index_before": hashlib.sha256(index).hexdigest(),
        "exclude_before": hashlib.sha256(exclude).hexdigest(),
        "source_before": verification_source(workspace),
    }


def _sync(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def apply_dependency_plan(workspace: Path, plan: Data) -> Data:
    """Fixed native effect; its caller must retain a durable intent before entry."""
    current = dependency_plan(workspace, plan["base_revision"], plan["metadata_before"], component=plan["component"])
    if current != plan:
        raise ValueError("The reviewed dependency reconciliation cut changed")
    info = workspace / ".git/info"
    exclude = info / "exclude"
    original = _regular(exclude, 65536)
    mode = stat.S_IMODE(exclude.lstat().st_mode)
    temporary = info / ("forge-exclude-" + uuid.uuid4().hex + ".tmp")
    # A partial effect must be observed by the caller, never rolled back or replayed here.
    _git(workspace, "update-index", "--force-remove", "--", *plan["aliases"])
    _sync(workspace / ".git/index")
    _sync(workspace / ".git")
    descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, mode)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(original + IGNORE_POLICY)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, exclude)
    _sync(info)
    if _git(workspace, "ls-files", "-z", "--", *plan["aliases"]):
        raise ValueError("A reconciled alias remains staged")
    for name, expected in plan["alias_identities"].items():
        value = (workspace / name).lstat()
        if expected != {
            "device": value.st_dev,
            "inode": value.st_ino,
            "mode": value.st_mode,
            "size": value.st_size,
            "mtime_ns": value.st_mtime_ns,
        }:
            raise ValueError("A reconciled alias changed")
    if (
        verification_source(workspace) != plan["source_before"]
        or _git(workspace, "rev-parse", "HEAD").strip().decode() != plan["base_revision"]
    ):
        raise ValueError("Public source or history changed during the native environment effect")
    return {
        "version": 1,
        "metadata_before": plan["metadata_before"],
        "metadata_after": metadata_digest(workspace),
        "index_after": hashlib.sha256(_regular(workspace / ".git/index", 1024 * 1024)).hexdigest(),
        "source_digest": plan["source_before"],
        "aliases": plan["aliases"],
    }
