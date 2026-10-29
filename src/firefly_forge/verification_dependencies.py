# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Stream explicit runtime dependency identities under the existing catalog budget."""

from __future__ import annotations

import asyncio
import hashlib
import os
import stat
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from firefly_forge.source_catalog import SourceCatalogBudget, _directory, _json, _records, _Sort, _stamp, _Work
from firefly_forge.verification_source_preparation import _offloop

_IGNORED = frozenset({"__pycache__"})


async def dependency_fingerprint(
    roots: list[Path], temporary_root: Path, budget: SourceCatalogBudget, current: Callable[[], Awaitable[None]]
) -> str:
    """Hash declared package roots, including symbolic links, without publishing their contents."""
    temporary_root.mkdir(mode=0o700, parents=True, exist_ok=True)

    async def operation(authority: Callable[[], Awaitable[None]], cancelled: Any) -> str:
        with tempfile.TemporaryDirectory(prefix="verification-dependencies-", dir=temporary_root) as directory:
            folder = _directory(Path(directory), private=True)
            try:
                work = _Work(folder, budget, lambda: cancelled.is_set())
                sorter = _Sort(folder, work)
                pending = list(roots)
                seen: set[str] = set()
                processed = 0

                async def check() -> None:
                    nonlocal processed
                    work.tick()
                    processed += 1
                    if processed % budget.sort_entries == 0:
                        await authority()
                    await asyncio.sleep(0)

                walked: set[str] = set()

                async def walk(parent: int, leaf: str, path: Path) -> None:
                    await check()
                    name = str(path)
                    if name in walked:
                        return
                    walked.add(name)
                    before = os.stat(leaf, dir_fd=parent, follow_symlinks=False)
                    if before.st_uid not in {0, os.getuid()}:
                        raise ValueError("Verification dependencies require their declared native owner")
                    if stat.S_ISLNK(before.st_mode):
                        target = os.readlink(leaf, dir_fd=parent)
                        resolved = path.resolve(strict=True)
                        sorter.add(
                            {
                                "entry": {"path": name, "kind": "link", "mode": before.st_mode, "target": target},
                                "identity": _stamp(before),
                            }
                        )
                        pending.append(resolved)
                    elif stat.S_ISDIR(before.st_mode):
                        descriptor = os.open(leaf, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                        try:
                            if _stamp(os.fstat(descriptor)) != _stamp(before):
                                raise ValueError("Dependency directory changed before its descriptor opened")
                            sorter.add(
                                {
                                    "entry": {"path": name, "kind": "directory", "mode": before.st_mode},
                                    "identity": _stamp(before),
                                }
                            )
                            with os.scandir(descriptor) as children:
                                for child in children:
                                    if child.name not in _IGNORED:
                                        await walk(descriptor, child.name, path / child.name)
                            if _stamp(os.fstat(descriptor)) != _stamp(before):
                                raise ValueError("Dependency directory membership changed during capture")
                        finally:
                            os.close(descriptor)
                    elif stat.S_ISREG(before.st_mode):
                        descriptor = os.open(leaf, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=parent)
                        try:
                            if _stamp(os.fstat(descriptor)) != _stamp(before):
                                raise ValueError("Dependency changed before its descriptor opened")
                            work.source_bytes += before.st_size
                            if budget.max_source_bytes is not None and work.source_bytes > budget.max_source_bytes:
                                raise ValueError("Configured dependency read budget exhausted")
                            hashed = hashlib.sha256()
                            remaining = before.st_size
                            while remaining:
                                await check()
                                part = os.read(descriptor, min(budget.chunk_bytes, remaining))
                                if not part:
                                    raise ValueError("Dependency ended before its captured length")
                                remaining -= len(part)
                                hashed.update(part)
                            if os.read(descriptor, 1) or _stamp(os.fstat(descriptor)) != _stamp(before):
                                raise ValueError("Dependency changed during its captured hash")
                            sorter.add(
                                {
                                    "entry": {
                                        "path": name,
                                        "kind": "file",
                                        "mode": before.st_mode,
                                        "sha256": hashed.hexdigest(),
                                    },
                                    "identity": _stamp(before),
                                }
                            )
                        finally:
                            os.close(descriptor)
                    else:
                        raise ValueError("Verification dependencies require ordinary files, links or directories")
                    if _stamp(os.stat(leaf, dir_fd=parent, follow_symlinks=False)) != _stamp(before):
                        raise ValueError("Verification dependency path changed during capture")

                async with asyncio.timeout(budget.timeout_seconds):
                    await authority()
                    while pending:
                        path = pending.pop().absolute()
                        spelling = str(path)
                        if spelling in seen:
                            continue
                        seen.add(spelling)
                        parent_path = path.parent.resolve(strict=True)
                        parent = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
                        try:
                            for component in parent_path.parts[1:]:
                                child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                                os.close(parent)
                                parent = child
                            parent_identity = _stamp(os.fstat(parent))[:5]
                            await walk(parent, path.name, parent_path / path.name)
                            if (
                                path.parent.resolve(strict=True) != parent_path
                                or _stamp(parent_path.lstat())[:5] != parent_identity
                            ):
                                raise ValueError("Dependency parent was replaced or redirected during capture")
                        finally:
                            os.close(parent)
                    run = sorter.finish()
                    result = hashlib.sha256()
                    for value in _records(folder, run, work):
                        result.update(_json(value["entry"]) + b"\n")
                    await authority()
                    return result.hexdigest()
            finally:
                os.close(folder)

    return await _offloop(operation, current)
