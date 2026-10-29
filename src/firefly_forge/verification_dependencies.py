# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Stream explicit runtime dependency identities under the existing catalog budget."""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextvars
import hashlib
import os
import stat
import tempfile
import threading
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from firefly_forge.source_catalog import (
    _IGNORED as _SOURCE_IGNORED,
)
from firefly_forge.source_catalog import (
    SourceCatalogBudget,
    _directory,
    _json,
    _private,
    _records,
    _Sort,
    _stamp,
    _Work,
)
from firefly_forge.verification_source_preparation import _offloop

_IGNORED = frozenset({"__pycache__"})


@dataclass(frozen=True)
class PreparedMetadataCut:
    metadata_cut: str
    _measure: Callable[[], Awaitable[str]]

    async def assert_current(self) -> None:
        if await self._measure() != self.metadata_cut:
            raise ValueError("Verification input metadata changed after capture")


@dataclass(frozen=True)
class PreparedDependencyCapture(PreparedMetadataCut):
    digest: str


def _cut_record(value: dict[str, Any]) -> dict[str, Any]:
    entry = {key: item for key, item in value["entry"].items() if key != "sha256"}
    identity = value["identity"][:5] if entry["kind"] == "directory" else value["identity"][:-1]
    return {"entry": entry, "identity": identity, "flags": value["identity"][-1]}


async def _capture(
    roots: list[Path],
    temporary_root: Path,
    budget: SourceCatalogBudget,
    authority: Callable[[], Awaitable[None]],
    cancelled: threading.Event,
    *,
    hash_payload: bool,
    public: bool = False,
    validate_path: Callable[[Path, bool], None] | None = None,
) -> tuple[str, str]:
    temporary_root.mkdir(mode=0o700, parents=True, exist_ok=True)
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
                if validate_path is not None:
                    validate_path(path, False)
                name = str(path)
                if name in walked:
                    return
                walked.add(name)
                before = os.stat(leaf, dir_fd=parent, follow_symlinks=False)
                if getattr(before, "st_flags", 0) & getattr(stat, "SF_DATALESS", 0):
                    raise ValueError("Verification dependency bytes are unavailable locally; hydration is not admitted")
                if before.st_uid not in ({os.getuid()} if public else {0, os.getuid()}):
                    raise ValueError("Verification dependencies require their declared native owner")
                if stat.S_ISLNK(before.st_mode):
                    if public:
                        raise ValueError("Public source permits owned regular files and real directories only")
                    target = os.readlink(leaf, dir_fd=parent)
                    resolved = path.resolve(strict=True)
                    sorter.add(
                        {
                            "entry": {"path": name, "kind": "link", "mode": before.st_mode, "target": target},
                            "identity": [*_stamp(before), getattr(before, "st_flags", 0)],
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
                                "identity": [*_stamp(before), getattr(before, "st_flags", 0)],
                            }
                        )
                        with os.scandir(descriptor) as children:
                            for child in children:
                                child_path = path / child.name
                                if public:
                                    relative = child_path.relative_to(roots[0].absolute()).as_posix()
                                    if _private(relative) or (
                                        child.is_dir(follow_symlinks=False) and child.name in _SOURCE_IGNORED
                                    ):
                                        continue
                                elif child.name in _IGNORED:
                                    continue
                                await walk(descriptor, child.name, child_path)
                        if _stamp(os.fstat(descriptor)) != _stamp(before):
                            raise ValueError("Dependency directory membership changed during capture")
                    finally:
                        os.close(descriptor)
                elif stat.S_ISREG(before.st_mode):
                    if public and before.st_nlink != 1:
                        raise ValueError("Public source files must not be hard linked")
                    if not hash_payload:
                        work.source_bytes += before.st_size
                        if budget.max_source_bytes is not None and work.source_bytes > budget.max_source_bytes:
                            raise ValueError("Configured dependency metadata budget exhausted")
                        sorter.add(
                            {
                                "entry": {"path": name, "kind": "file", "mode": before.st_mode},
                                "identity": [*_stamp(before), getattr(before, "st_flags", 0)],
                            }
                        )
                        if _stamp(os.stat(leaf, dir_fd=parent, follow_symlinks=False)) != _stamp(before):
                            raise ValueError("Verification input changed during metadata capture")
                        return
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
                                "identity": [*_stamp(before), getattr(before, "st_flags", 0)],
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
                    if validate_path is not None:
                        validate_path(path, True)
                    spelling = str(path)
                    if spelling in seen:
                        continue
                    seen.add(spelling)
                    parent_path = path.parent.resolve(strict=True)
                    parent = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
                    try:
                        for component in parent_path.parts[1:]:
                            info = os.stat(component, dir_fd=parent, follow_symlinks=False)
                            if getattr(info, "st_flags", 0) & getattr(stat, "SF_DATALESS", 0):
                                raise ValueError("Verification dependency parent is unavailable locally")
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
                metadata = hashlib.sha256()
                for value in _records(folder, run, work):
                    result.update(_json(value["entry"]) + b"\n")
                    metadata.update(_json(_cut_record(value)) + b"\n")
                await authority()
                return result.hexdigest(), metadata.hexdigest()
        finally:
            os.close(folder)


async def _metadata_cut(
    roots: list[Path],
    temporary_root: Path,
    budget: SourceCatalogBudget,
    *,
    public: bool = False,
    validate_path: Callable[[Path, bool], None] | None = None,
) -> str:
    cancelled = threading.Event()
    cancel_worker: concurrent.futures.Future[Callable[[], None]] = concurrent.futures.Future()

    def worker() -> str:
        async def execute() -> str:
            loop = asyncio.get_running_loop()
            task = asyncio.current_task()
            assert task is not None

            def cancel() -> None:
                if not loop.is_closed():
                    loop.call_soon_threadsafe(task.cancel)

            cancel_worker.set_result(cancel)

            async def current() -> None:
                if cancelled.is_set():
                    raise asyncio.CancelledError

            _, metadata = await _capture(
                roots,
                temporary_root,
                budget,
                current,
                cancelled,
                hash_payload=False,
                public=public,
                validate_path=validate_path,
            )
            return metadata

        return contextvars.Context().run(lambda: asyncio.run(execute()))

    task = asyncio.get_running_loop().run_in_executor(None, contextvars.Context().run, worker)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        cancelled.set()
        cancel_worker.add_done_callback(lambda ready: ready.result()())
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
        if not task.cancelled():
            task.exception()
        raise


async def prepare_dependency_capture(
    roots: list[Path],
    temporary_root: Path,
    budget: SourceCatalogBudget,
    current: Callable[[], Awaitable[None]],
    *,
    validate_path: Callable[[Path, bool], None] | None = None,
) -> PreparedDependencyCapture:
    declared = list(roots)

    async def operation(authority: Callable[[], Awaitable[None]], cancelled: threading.Event) -> tuple[str, str]:
        return await _capture(
            declared, temporary_root, budget, authority, cancelled, hash_payload=True, validate_path=validate_path
        )

    digest, metadata = await _offloop(operation, current)

    async def measure() -> str:
        return await _metadata_cut(declared, temporary_root, budget, validate_path=validate_path)

    captured = PreparedDependencyCapture(metadata_cut=metadata, _measure=measure, digest=digest)
    await captured.assert_current()
    return captured


async def prepare_public_source_cut(
    workspace: Path, temporary_root: Path, budget: SourceCatalogBudget
) -> PreparedMetadataCut:
    async def measure() -> str:
        return await _metadata_cut([workspace], temporary_root, budget, public=True)

    return PreparedMetadataCut(metadata_cut=await measure(), _measure=measure)


async def dependency_fingerprint(
    roots: list[Path], temporary_root: Path, budget: SourceCatalogBudget, current: Callable[[], Awaitable[None]]
) -> str:
    """Hash declared package roots without changing the retained digest format."""
    return (await prepare_dependency_capture(roots, temporary_root, budget, current)).digest
