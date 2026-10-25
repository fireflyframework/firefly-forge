# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Stream checkpoint source identity independently of runner transport archives."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import stat
import tempfile
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from firefly_forge.sandbox import _directory_fd, _parent_fd, _regular_data
from firefly_forge.source_diff_admission import SourceDiffAdmission
from firefly_forge.source_snapshot import stream_git_bytes
from firefly_forge.workspace_tools import _descriptor_path, _private_name


def _stamp(value: os.stat_result) -> list[int]:
    return [
        getattr(value, "st_" + key) for key in ("dev", "ino", "uid", "mode", "nlink", "size", "mtime_ns", "ctime_ns")
    ]


def _parts(name: str) -> tuple[str, ...]:
    parts = tuple(name.split("/"))
    if not name or any(part in {"", ".", ".."} for part in parts) or "\x00" in name:
        raise ValueError("Checkpoint source requires exact relative Git paths")
    if any(_private_component(part) for part in parts):
        raise ValueError("Checkpoint source cannot include selected private paths")
    return parts


def _private_component(name: str) -> bool:
    return _private_name(name) or name.casefold() in {".aws", ".azure", ".docker", ".forge-sandbox-results"}


def _remember(records: BinaryIO, path: str, info: os.stat_result) -> None:
    records.write(json.dumps([path, _stamp(info)], separators=(",", ":")).encode() + b"\n")


def _parents(root: int, parts: tuple[str, ...], records: BinaryIO) -> int:
    current = os.dup(root)
    try:
        _remember(records, "", os.fstat(current))
        for ordinal, name in enumerate(parts[:-1], 1):
            child = _directory_fd(name, parent=current)
            os.close(current)
            current = child
            if os.fstat(current).st_uid != os.geteuid():
                raise ValueError("Checkpoint source directories require current ownership")
            _remember(records, "/".join(parts[:ordinal]), os.fstat(current))
        return current
    except BaseException:
        os.close(current)
        raise


def _revalidate(root: int, records: BinaryIO, deadline: float) -> None:
    records.seek(0)
    for record in records:
        if time.monotonic() >= deadline:
            raise TimeoutError("Checkpoint source inspection exceeded its finite time budget")
        name, expected = json.loads(record)
        parts = tuple(name.split("/")) if name else ()
        parent = _parent_fd(root, parts) if parts else os.dup(root)
        try:
            actual = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False) if parts else os.fstat(parent)
            if _stamp(actual) != expected:
                raise ValueError("Checkpoint source changed during final admission")
        except OSError:
            raise ValueError("Checkpoint source changed during final admission") from None
        finally:
            os.close(parent)


async def _directory_witnesses(directory: int, prefix: tuple[str, ...], records: BinaryIO) -> None:
    _remember(records, "/".join(prefix), os.fstat(directory))
    with os.scandir(directory) as entries:
        for entry in entries:
            if _private_component(entry.name):
                continue
            if entry.is_dir(follow_symlinks=False):
                child = _directory_fd(entry.name, parent=directory)
                try:
                    await _directory_witnesses(child, (*prefix, entry.name), records)
                finally:
                    os.close(child)
            await asyncio.sleep(0)


def git_head_identity(workspace: Path) -> str:
    directory = _directory_fd(workspace / ".git")
    try:
        head, _ = _regular_data(directory, "HEAD", 1024)
        result = hashlib.sha256(head)
        if head.startswith(b"ref: "):
            name = head[5:].rstrip(b"\n").decode("ascii")
            parts = tuple(name.split("/"))
            if not name.startswith("refs/") or any(part in {"", ".", ".."} for part in parts):
                raise ValueError("Checkpoint source HEAD reference is invalid")
            parent = None
            try:
                parent = _parent_fd(directory, parts)
                os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                result.update(b"packed-ref")
            else:
                data, _ = _regular_data(parent, parts[-1], 1024)
                result.update(data)
            finally:
                if parent is not None:
                    os.close(parent)
        return result.hexdigest()
    finally:
        os.close(directory)


@dataclass(frozen=True)
class SourceEntry:
    path: str
    mode: str | None
    object_id: str | None


async def stream_source_entries(
    workspace: Path, consume: Callable[[SourceEntry], Awaitable[None]], *, timeout_seconds: int = 300
) -> None:
    """Enumerate tracked source, including ignored paths, then nonignored new files."""
    for tracked in (True, False):
        tail = b""

        async def receive(chunk: bytes, *, tracked: bool = tracked) -> None:
            nonlocal tail
            values = (tail + chunk).split(b"\0")
            tail = values.pop()
            if len(tail) > 65536:
                raise ValueError("Checkpoint source path exceeds the Git name window")
            for value in values:
                mode = object_id = None
                if tracked:
                    prefix, tab, value = value.partition(b"\t")
                    fields = prefix.split(b" ")
                    if not tab or len(fields) != 3 or fields[0] not in {b"100644", b"100755"} or fields[2] != b"0":
                        raise ValueError("Checkpoint source index requires regular files without links or conflicts")
                    mode, object_id = fields[0].decode(), fields[1].decode("ascii")
                try:
                    name = value.decode("utf-8", errors="strict")
                except UnicodeDecodeError:
                    raise ValueError("Checkpoint source requires exact UTF-8 Git paths") from None
                _parts(name)
                await consume(SourceEntry(name, mode, object_id))

        args = ["git", "ls-files", "--stage"] if tracked else ["git", "ls-files", "--others", "--exclude-standard"]
        await stream_git_bytes(workspace, [*args, "-z", "--"], receive, timeout_seconds=timeout_seconds)
        if tail:
            raise ValueError("Checkpoint source inventory ended without an exact path boundary")


async def source_identity(
    workspace: Path,
    scanner: SourceDiffAdmission,
    *,
    finalize: Callable[[], Awaitable[None]],
    timeout_seconds: int = 300,
) -> str:
    """Hash complete selected source using bounded chunks and temporary metadata only."""
    root = _directory_fd(workspace)
    deadline = time.monotonic() + timeout_seconds
    try:
        if os.fstat(root).st_uid != os.geteuid():
            raise ValueError("Checkpoint source requires current workspace ownership")
        canonical = _descriptor_path(root)
        with tempfile.TemporaryFile(mode="w+b") as records:
            async with asyncio.timeout(timeout_seconds):
                first = await _capture(canonical, root, scanner, records, timeout_seconds)
                records.seek(0)
                records.truncate()
                await _directory_witnesses(root, (), records)
                second = await _capture(canonical, root, scanner, records, timeout_seconds)
                if first != second:
                    raise ValueError("Checkpoint source selection or contents changed during capture")
                await scanner(b"", True)
                await finalize()
                _revalidate(root, records, deadline)
                if _stamp(os.stat(workspace, follow_symlinks=False)) != _stamp(os.fstat(root)):
                    raise ValueError("Checkpoint source root changed during capture")
                return "git-source-v1:" + second
    finally:
        os.close(root)


async def _capture(
    workspace: Path, root: int, scanner: SourceDiffAdmission, records: BinaryIO, timeout_seconds: int
) -> str:
    result = hashlib.sha256(b"forge-checkpoint-git-source-v1\0")
    _remember(records, "", os.fstat(root))

    async def consume(entry: SourceEntry) -> None:
        parts = _parts(entry.path)
        raw = json.dumps([entry.path, entry.mode, entry.object_id], ensure_ascii=False, separators=(",", ":")).encode()
        await scanner(entry.path.encode(), False)
        scanner.finish_document()
        result.update(len(raw).to_bytes(8, "big") + raw)
        parent = descriptor = None
        try:
            parent = _parents(root, parts, records)
            descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_uid != os.geteuid():
                raise ValueError("Checkpoint source requires owned unaliased regular files without links")
            if _descriptor_path(descriptor).relative_to(workspace) != Path(entry.path):
                raise ValueError("Checkpoint source path differs from its opened descriptor")
            content, size = hashlib.sha256(), 0
            while chunk := os.read(descriptor, 65536):
                size += len(chunk)
                content.update(chunk)
                await scanner(chunk, False)
            scanner.finish_document()
            after = os.fstat(descriptor)
            named = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
            if _stamp(before) != _stamp(after) or _stamp(before) != _stamp(named) or size != before.st_size:
                raise ValueError("Checkpoint source changed while reading a regular file")
            if _descriptor_path(descriptor).relative_to(workspace) != Path(entry.path):
                raise ValueError("Checkpoint source path changed during its read")
            _remember(records, entry.path, after)
            result.update(b"file\0" + str(stat.S_IMODE(after.st_mode)).encode() + b"\0")
            result.update(size.to_bytes(8, "big") + content.digest())
        except FileNotFoundError:
            if entry.mode is None:
                raise ValueError("Checkpoint source changed during inventory") from None
            result.update(b"deleted\0")
        except OSError:
            raise ValueError("Checkpoint source requires regular files and directories without links") from None
        finally:
            for fd in (descriptor, parent):
                if fd is not None:
                    os.close(fd)

    await stream_source_entries(workspace, consume, timeout_seconds=timeout_seconds)
    return result.hexdigest()
