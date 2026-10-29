# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Seal a public parent tree into an independent, immutable child clone origin."""

from __future__ import annotations

import asyncio
import hashlib
import os
import stat
from pathlib import Path
from typing import Any

from firefly_forge.journal import digest
from firefly_forge.process import checked
from firefly_forge.workspace_tools import WorkspaceTools

Data = dict[str, Any]


async def _files(tools: WorkspaceTools) -> Data:
    rows, view = await tools._inventory()
    result: Data = {}
    for name, stamp in rows:
        hashed = hashlib.sha256()
        with tools._source_file(name, stamp) as source:
            executable = bool(os.fstat(source.fileno()).st_mode & stat.S_IXUSR)
            while chunk := source.read(65536):
                hashed.update(chunk)
                await asyncio.sleep(0)
        result[name] = {"sha256": hashed.hexdigest(), "executable": executable}
    if (await tools._inventory())[1] != view:
        raise ValueError("Parent source changed while capturing child ownership")
    return result


async def capture_child_source(tools: WorkspaceTools, destination: Path) -> Data:
    if destination.exists() or destination.is_symlink():
        raise ValueError("Child source requires a fresh destination")
    if destination.parent.is_symlink() or not destination.parent.is_dir():
        raise ValueError("Child source requires an owned private parent directory")
    resolved = destination.parent.resolve() / destination.name
    if resolved.is_relative_to(tools.repository_root) or tools.repository_root.is_relative_to(resolved):
        raise ValueError("Child source must be isolated from the parent workspace")
    rows, view = await tools._inventory()
    destination.mkdir(mode=0o700)
    child = WorkspaceTools(destination)
    files: Data = {}
    for name, stamp in rows:
        hashed = hashlib.sha256()
        parts = child._parts(name)
        parent = child._parent_fd(parts, create=True)
        try:
            with tools._source_file(name, stamp) as source:
                executable = bool(os.fstat(source.fileno()).st_mode & stat.S_IXUSR)
                fd = os.open(
                    parts[-1],
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o755 if executable else 0o644,
                    dir_fd=parent,
                )
                with os.fdopen(fd, "wb") as target:
                    while chunk := source.read(65536):
                        target.write(chunk)
                        hashed.update(chunk)
                        await asyncio.sleep(0)
                    os.fsync(target.fileno())
                files[name] = {"sha256": hashed.hexdigest(), "executable": executable}
        finally:
            os.close(parent)
    if (await tools._inventory())[1] != view or await _files(tools) != files:
        raise ValueError("Parent source changed before child admission")
    git = ["git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false"]
    await checked(git + ["init", "--initial-branch=main"], destination)
    await checked(git + ["add", "--all", "--force", "--", "."], destination)
    await checked(
        git
        + [
            "-c",
            "user.name=Forge",
            "-c",
            "user.email=forge@localhost",
            "commit",
            "--allow-empty",
            "-m",
            "Seal delegated source baseline",
        ],
        destination,
    )
    value = {
        "version": 1,
        "path": str(resolved),
        "revision": await checked(git + ["rev-parse", "HEAD"], destination),
        "tree": await checked(git + ["rev-parse", "HEAD^{tree}"], destination),
        "files": files,
    }
    value["digest"] = digest(value)
    await verify_child_source(value)
    return value


async def verify_child_source(value: Data) -> None:
    if (
        set(value) != {"version", "path", "revision", "tree", "files", "digest"}
        or type(value["version"]) is not int
        or value["version"] != 1
        or digest({key: item for key, item in value.items() if key != "digest"}) != value["digest"]
    ):
        raise ValueError("Child source seal does not verify")
    root = Path(value["path"])
    tools = WorkspaceTools(root)
    if await _files(tools) != value["files"]:
        raise ValueError("Child source content or mode changed")
    for reference, expected in (("HEAD", value["revision"]), ("HEAD^{tree}", value["tree"])):
        if await checked(["git", "rev-parse", reference], root) != expected:
            raise ValueError("Child source Git identity changed")
    if await checked(["git", "status", "--porcelain", "--untracked-files=all"], root):
        raise ValueError("Sealed child source has changed")
