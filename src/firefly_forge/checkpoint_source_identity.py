# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Stream checkpoint source identity independently of runner transport archives."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sqlite3
import stat
import tempfile
import time
from collections.abc import Awaitable, Callable
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, cast

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


def _revalidate(
    root: int, records: BinaryIO, deadline: float, *, namespace: bool = False, strict_directories: bool = False
) -> bool:
    changed = False
    records.seek(0)
    for record in records:
        if time.monotonic() >= deadline:
            raise TimeoutError("Checkpoint source inspection exceeded its finite time budget")
        name, expected = json.loads(record)
        parts = tuple(name.split("/")) if name else ()
        parent = None
        try:
            parent = _parent_fd(root, parts) if parts else os.dup(root)
            actual = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False) if parts else os.fstat(parent)
            if expected is None:
                raise ValueError("Checkpoint deleted source changed during final admission")
            observed = _stamp(actual)
            if stat.S_ISDIR(expected[3]):
                if observed[:4] != expected[:4]:
                    raise ValueError("Checkpoint source directory identity changed during final admission")
                if strict_directories and observed != expected:
                    raise ValueError("Checkpoint Git metadata changed during final admission")
                changed = changed or (namespace and observed != expected)
            elif observed != expected:
                raise ValueError("Checkpoint source changed during final admission")
        except FileNotFoundError:
            if expected is not None:
                raise ValueError("Checkpoint source changed during final admission") from None
        except OSError:
            raise ValueError("Checkpoint source changed during final admission") from None
        finally:
            if parent is not None:
                os.close(parent)
    return changed


async def _git_metadata_witnesses(directory: int, prefix: tuple[str, ...], records: BinaryIO) -> None:
    _remember(records, "/".join(prefix), os.fstat(directory))
    with os.scandir(directory) as entries:
        for entry in entries:
            info = entry.stat(follow_symlinks=False)
            if info.st_uid != os.geteuid():
                raise ValueError("Checkpoint Git metadata requires current ownership")
            if stat.S_ISDIR(info.st_mode):
                child = _directory_fd(entry.name, parent=directory)
                try:
                    await _git_metadata_witnesses(child, (*prefix, entry.name), records)
                finally:
                    os.close(child)
            elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                _remember(records, "/".join((*prefix, entry.name)), info)
            else:
                raise ValueError("Checkpoint Git metadata requires unaliased regular files and directories")
            await asyncio.sleep(0)


async def _ignored_directories(workspace: Path, ignored: sqlite3.Connection, timeout_seconds: int) -> None:
    ignored.execute("DELETE FROM roots")
    tail = b""

    async def receive(chunk: bytes) -> None:
        nonlocal tail
        values = (tail + chunk).split(b"\0")
        tail = values.pop()
        if len(tail) > 65536:
            raise ValueError("Checkpoint ignored directory exceeds the Git name window")
        for value in values:
            if not value.endswith(b"/"):
                continue
            try:
                name = value[:-1].decode("utf-8", errors="strict")
            except UnicodeDecodeError:
                raise ValueError("Checkpoint ignored directories require exact UTF-8 paths") from None
            if any(_private_component(part) for part in name.split("/")):
                continue
            _parts(name)
            ignored.execute("INSERT OR IGNORE INTO roots VALUES (?)", (name,))

    await stream_git_bytes(
        workspace,
        ["git", "ls-files", "--others", "--ignored", "--exclude-standard", "--directory", "-z", "--"],
        receive,
        timeout_seconds=timeout_seconds,
    )
    if tail:
        raise ValueError("Checkpoint ignored directories ended without an exact path boundary")
    ignored.commit()


async def _directory_witnesses(
    directory: int, prefix: tuple[str, ...], records: BinaryIO, ignored: sqlite3.Connection
) -> None:
    _remember(records, "/".join(prefix), os.fstat(directory))
    with os.scandir(directory) as entries:
        for entry in entries:
            relative = "/".join((*prefix, entry.name))
            if (
                _private_component(entry.name)
                or ignored.execute("SELECT 1 FROM roots WHERE path = ?", (relative,)).fetchone()
            ):
                continue
            if entry.is_dir(follow_symlinks=False):
                child = _directory_fd(entry.name, parent=directory)
                try:
                    await _directory_witnesses(child, (*prefix, entry.name), records, ignored)
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


@dataclass(frozen=True)
class SelectedSourceFence:
    workspace: Path
    root_identity: tuple[int, ...]
    streams: tuple[BinaryIO, BinaryIO, BinaryIO]
    timeout_seconds: int

    def revalidate(self, *, payload_only: bool = False) -> None:
        root = _directory_fd(self.workspace)
        try:
            if _descriptor_path(root) != self.workspace or tuple(_stamp(os.fstat(root))[:4]) != self.root_identity:
                raise ValueError("Selected source root changed after capture")
            deadline = time.monotonic() + self.timeout_seconds
            for stream in self.streams[:2] if payload_only else self.streams:
                if stream.closed:
                    raise ValueError("Selected source witness is closed")
                _revalidate(root, stream, deadline, strict_directories=True)
        finally:
            os.close(root)

    def close(self) -> None:
        for stream in self.streams:
            stream.close()


@dataclass(frozen=True)
class CapturedSourceIdentity:
    source_digest: str
    physical_digest: str
    fence: SelectedSourceFence


async def source_identity(
    workspace: Path,
    scanner: SourceDiffAdmission,
    *,
    finalize: Callable[[], Awaitable[None]],
    timeout_seconds: int = 300,
) -> str:
    return (await _source_capture(workspace, scanner, finalize=finalize, timeout_seconds=timeout_seconds))[0]


async def source_payload_identity(
    workspace: Path,
    scanner: SourceDiffAdmission,
    *,
    finalize: Callable[[], Awaitable[None]],
    timeout_seconds: int = 300,
) -> CapturedSourceIdentity:
    full, physical, fence = await _source_capture(
        workspace, scanner, finalize=finalize, timeout_seconds=timeout_seconds, physical=True
    )
    assert physical is not None and fence is not None
    return CapturedSourceIdentity(full, physical, fence)


async def _source_capture(
    workspace: Path,
    scanner: SourceDiffAdmission,
    *,
    finalize: Callable[[], Awaitable[None]],
    timeout_seconds: int = 300,
    physical: bool = False,
) -> tuple[str, str | None, SelectedSourceFence | None]:
    """Hash complete selected source using bounded chunks and temporary metadata only."""
    root = _directory_fd(workspace)
    deadline = time.monotonic() + timeout_seconds
    try:
        if os.fstat(root).st_uid != os.geteuid():
            raise ValueError("Checkpoint source requires current workspace ownership")
        canonical = _descriptor_path(root)
        with (
            tempfile.TemporaryFile(mode="w+b") as records,
            tempfile.TemporaryFile(mode="w+b") as directories,
            tempfile.TemporaryFile(mode="w+b") as metadata,
            tempfile.TemporaryDirectory(prefix="forge-source-ignored-") as ignored_root,
            closing(sqlite3.connect(str(Path(ignored_root) / "roots.sqlite3"))) as ignored,
        ):
            ignored.execute("PRAGMA cache_size = -1024")
            ignored.execute("CREATE TABLE roots (path TEXT PRIMARY KEY) WITHOUT ROWID")
            async with asyncio.timeout(timeout_seconds):
                git_directory = _directory_fd(".git", parent=root)
                try:
                    await _git_metadata_witnesses(git_directory, (".git",), metadata)
                finally:
                    os.close(git_directory)
                first = await _capture(
                    canonical, root, scanner, records, timeout_seconds, physical=ignored if physical else None
                )
                records.seek(0)
                records.truncate()
                await _ignored_directories(canonical, ignored, timeout_seconds)
                await _directory_witnesses(root, (), directories, ignored)
                second = await _capture(
                    canonical, root, scanner, records, timeout_seconds, physical=ignored if physical else None
                )
                if first != second:
                    raise ValueError("Checkpoint source selection or contents changed during capture")
                await scanner(b"", True)
                while True:
                    await finalize()
                    _revalidate(root, metadata, deadline, strict_directories=True)
                    _revalidate(root, records, deadline)
                    if not _revalidate(root, directories, deadline, namespace=True):
                        named, opened = os.stat(workspace, follow_symlinks=False), os.fstat(root)
                        if _stamp(named)[:4] != _stamp(opened)[:4]:
                            raise ValueError("Checkpoint source root changed during capture")
                        fence = None
                        if physical:
                            streams: list[BinaryIO] = []
                            try:
                                for stream in (records, directories, metadata):
                                    stream.flush()
                                    streams.append(cast(BinaryIO, os.fdopen(os.dup(stream.fileno()), "rb")))
                                fence = SelectedSourceFence(
                                    canonical,
                                    tuple(_stamp(os.fstat(root))[:4]),
                                    (streams[0], streams[1], streams[2]),
                                    timeout_seconds,
                                )
                            except BaseException:
                                for owned_stream in streams:
                                    owned_stream.close()
                                raise
                        return "git-source-v1:" + second[0], second[2], fence
                    # Only canonical Git can distinguish new ignored output from selected source.
                    directories.seek(0)
                    directories.truncate()
                    await _ignored_directories(canonical, ignored, timeout_seconds)
                    await _directory_witnesses(root, (), directories, ignored)
                    if await _inventory_identity(canonical, timeout_seconds) != second[1]:
                        raise ValueError("Checkpoint source selection changed during final admission")
                    await scanner.revalidate()
    finally:
        os.close(root)


async def _capture(
    workspace: Path,
    root: int,
    scanner: SourceDiffAdmission,
    records: BinaryIO,
    timeout_seconds: int,
    *,
    physical: sqlite3.Connection | None = None,
) -> tuple[str, str, str | None]:
    result = hashlib.sha256(b"forge-checkpoint-git-source-v1\0")
    inventory = hashlib.sha256()
    _remember(records, "", os.fstat(root))
    if physical is not None:
        physical.execute(
            "CREATE TABLE IF NOT EXISTS physical (path TEXT PRIMARY KEY, data BLOB NOT NULL) WITHOUT ROWID"
        )
        physical.execute("DELETE FROM physical")

    def retain(path: str, value: list[object]) -> None:
        if physical is not None:
            encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
            physical.execute("INSERT OR REPLACE INTO physical VALUES (?, ?)", (path, encoded))

    async def consume(entry: SourceEntry) -> None:
        parts = _parts(entry.path)
        raw = json.dumps([entry.path, entry.mode, entry.object_id], ensure_ascii=False, separators=(",", ":")).encode()
        await scanner(entry.path.encode(), False)
        scanner.finish_document()
        result.update(len(raw).to_bytes(8, "big") + raw)
        inventory.update(len(raw).to_bytes(8, "big") + raw)
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
            retain(
                entry.path, [entry.path, "file", stat.S_IMODE(after.st_mode), after.st_uid, size, content.hexdigest()]
            )
        except FileNotFoundError:
            if entry.mode is None:
                raise ValueError("Checkpoint source changed during inventory") from None
            records.write(json.dumps([entry.path, None], separators=(",", ":")).encode() + b"\n")
            result.update(b"deleted\0")
            retain(entry.path, [entry.path, "deleted"])
        except OSError:
            raise ValueError("Checkpoint source requires regular files and directories without links") from None
        finally:
            for fd in (descriptor, parent):
                if fd is not None:
                    os.close(fd)

    await stream_source_entries(workspace, consume, timeout_seconds=timeout_seconds)
    physical_digest = None
    if physical is not None:
        records.seek(0)
        for encoded in records:
            path, stamp = json.loads(encoded)
            if stamp is not None and stat.S_ISDIR(stamp[3]):
                retain(path + "/", [path, "directory", stat.S_IMODE(stamp[3]), stamp[2]])
        selected = hashlib.sha256(b"forge-checkpoint-selected-physical-v1\0")
        deadline = time.monotonic() + timeout_seconds
        for ordinal, (encoded,) in enumerate(
            physical.execute("SELECT data FROM physical ORDER BY path COLLATE BINARY")
        ):
            if time.monotonic() >= deadline:
                raise TimeoutError("Selected physical source hashing exceeded its inspection window")
            selected.update(len(encoded).to_bytes(8, "big") + encoded)
            if ordinal % 128 == 0:
                await asyncio.sleep(0)
        physical_digest = selected.hexdigest()
    return result.hexdigest(), inventory.hexdigest(), physical_digest


async def _inventory_identity(workspace: Path, timeout_seconds: int) -> str:
    result = hashlib.sha256()

    async def consume(entry: SourceEntry) -> None:
        raw = json.dumps([entry.path, entry.mode, entry.object_id], ensure_ascii=False, separators=(",", ":")).encode()
        result.update(len(raw).to_bytes(8, "big") + raw)

    await stream_source_entries(workspace, consume, timeout_seconds=timeout_seconds)
    return result.hexdigest()
