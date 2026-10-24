# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Paged public-source metadata with streaming legacy-compatible identity hashes."""

from __future__ import annotations

import asyncio
import hashlib
import heapq
import json
import os
import re
import stat
import time
from collections.abc import AsyncGenerator, Awaitable, Callable, Generator
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

Data = dict[str, Any]
_HEX = r"^[a-f0-9]{64}$"
_IGNORED = frozenset(
    {
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "build",
        "dist",
        ".cache",
    }
)


class _Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True)


class SourceCatalogBudget(_Contract):
    max_source_bytes: int | None = Field(default=None, ge=1)
    max_artifact_bytes: int | None = Field(default=None, ge=1)
    min_free_bytes: int = Field(default=1073741824, ge=0)
    timeout_seconds: int = Field(default=120, ge=1)
    chunk_bytes: int = Field(default=65536, ge=1024, le=1048576)
    sort_entries: int = Field(default=128, ge=2, le=4096)
    page_entries: int = Field(default=128, ge=1, le=4096)
    merge_fan_in: int = Field(default=16, ge=2, le=64)


class SourceCatalogRefV1(_Contract):
    version: Literal[1] = 1
    artifact_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    manifest_sha256: str = Field(pattern=_HEX)
    source_digest: str = Field(pattern=_HEX)
    manifest_digest: str = Field(pattern=_HEX)
    byte_count: int = Field(ge=0)
    file_count: int = Field(ge=0)
    entry_count: int = Field(ge=0)
    page_count: int = Field(ge=0)


class SourceCatalogEntryV1(_Contract):
    path: str
    kind: Literal["file", "directory"]
    mode: int
    byte_count: int = Field(ge=0)
    sha256: str | None


class SourceCatalogPageV1(_Contract):
    version: Literal[1] = 1
    artifact_id: str
    manifest_sha256: str
    page: int
    next_page: int | None
    entries: list[SourceCatalogEntryV1]


def _json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _stamp(value: os.stat_result) -> list[int]:
    return [
        value.st_dev,
        value.st_ino,
        value.st_uid,
        value.st_gid,
        value.st_mode,
        value.st_nlink,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    ]


def _directory(name: Path | str, parent: int | None = None, *, private: bool = False) -> int:
    fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
    value = os.fstat(fd)
    if value.st_uid != os.getuid() or (stat.S_IMODE(value.st_mode) != 0o700 if private else value.st_mode & 0o022):
        os.close(fd)
        raise ValueError("Catalog directories must be owned, unaliased and appropriately private")
    return fd


def _read(parent: int, name: str, width: int) -> bytes:
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    try:
        before = os.fstat(fd)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.getuid()
            or before.st_nlink != 1
            or stat.S_IMODE(before.st_mode) != 0o400
        ):
            raise ValueError("Catalog page must be immutable owner-controlled regular data")
        raw = os.pread(fd, width + 1, 0)
        if len(raw) > width or len(raw) != before.st_size or _stamp(before) != _stamp(os.fstat(fd)):
            raise ValueError("Catalog page changed or exceeded its processing window")
        return raw
    finally:
        os.close(fd)


class _Work:
    def __init__(
        self,
        root: int,
        budget: SourceCatalogBudget,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> None:
        self.root, self.budget = root, budget
        self.cancel_requested = cancel_requested
        self.deadline = time.monotonic() + budget.timeout_seconds
        self.used = 0
        self.source_bytes = 0

    def tick(self) -> None:
        if self.cancel_requested is not None and self.cancel_requested():
            raise asyncio.CancelledError
        space = os.fstatvfs(self.root)
        if space.f_bavail * space.f_frsize < self.budget.min_free_bytes:
            raise ValueError("The source catalog free-space floor was reached")
        if time.monotonic() >= self.deadline:
            raise TimeoutError("The source catalog deadline expired")

    def put(self, parent: int, name: str, raw: bytes) -> None:
        self.tick()
        if len(raw) > self.budget.chunk_bytes:
            raise ValueError("Catalog metadata exceeds the configured processing page")
        if self.budget.max_artifact_bytes is not None and self.used + len(raw) > self.budget.max_artifact_bytes:
            raise ValueError("The configured source catalog storage budget was exceeded")
        fd = os.open(
            name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=parent,
        )
        with os.fdopen(fd, "wb") as output:
            output.write(raw)
            output.flush()
            os.fchmod(output.fileno(), 0o400)
            os.fsync(output.fileno())
        self.used += len(raw)
        self.tick()


def _private(path: str) -> bool:
    # Keep the existing public-source selection, including example environment files.
    return any(
        part
        in {
            ".git",
            ".codex",
            ".claude",
            ".forge",
            "node_modules",
            ".venv",
            "vendor",
            ".env",
            ".npmrc",
            ".pypirc",
            ".ssh",
            "credentials.json",
            "id_rsa",
            "id_ed25519",
        }
        or part.endswith((".pem", ".key"))
        or (part.startswith(".env.") and part not in {".env.example", ".env.sample", ".env.template"})
        for part in path.split("/")
    )


async def _walk(parent: int, prefix: str, work: _Work, *, hash_payload: bool = True) -> AsyncGenerator[Data]:
    before = os.fstat(parent)
    with os.scandir(parent) as entries:
        for item in entries:
            work.tick()
            path = prefix + item.name
            if _private(path) or (item.is_dir(follow_symlinks=False) and item.name in _IGNORED):
                continue
            info = item.stat(follow_symlinks=False)
            if info.st_uid != os.getuid() or not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
                raise ValueError("Public source permits owned regular files and real directories only")
            if stat.S_ISDIR(info.st_mode):
                child = _directory(item.name, parent)
                try:
                    if _stamp(info) != _stamp(os.fstat(child)):
                        raise ValueError("Source directory changed before its descriptor opened")
                    yield {
                        "entry": {
                            "path": path + "/",
                            "kind": "directory",
                            "mode": info.st_mode,
                            "byte_count": 0,
                            "sha256": None,
                        },
                        "identity": _stamp(info),
                    }
                    children = _walk(child, path + "/", work, hash_payload=hash_payload)
                    try:
                        async for record in children:
                            yield record
                    finally:
                        await children.aclose()
                finally:
                    os.close(child)
            else:
                if info.st_nlink != 1:
                    raise ValueError("Public source files must not be hard linked")
                work.source_bytes += info.st_size
                if work.budget.max_source_bytes is not None and work.source_bytes > work.budget.max_source_bytes:
                    raise ValueError("The configured source-read budget was exceeded")
                fd = os.open(
                    item.name,
                    os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW,
                    dir_fd=parent,
                )
                try:
                    if _stamp(info) != _stamp(os.fstat(fd)):
                        raise ValueError("Source file changed before its descriptor opened")
                    digest = hashlib.sha256()
                    remaining = info.st_size if hash_payload else 0
                    while remaining:
                        work.tick()
                        raw = os.read(fd, min(work.budget.chunk_bytes, remaining))
                        if not raw:
                            raise ValueError("Source file changed during its streamed hash")
                        digest.update(raw)
                        remaining -= len(raw)
                        await asyncio.sleep(0)
                    if hash_payload and os.read(fd, 1):
                        raise ValueError("Source file grew during its bounded streamed hash")
                    if _stamp(info) != _stamp(os.fstat(fd)) or _stamp(info) != _stamp(
                        os.stat(item.name, dir_fd=parent, follow_symlinks=False)
                    ):
                        raise ValueError("Source file changed during its streamed hash")
                finally:
                    os.close(fd)
                yield {
                    "entry": {
                        "path": path,
                        "kind": "file",
                        "mode": info.st_mode,
                        "byte_count": info.st_size,
                        "sha256": digest.hexdigest() if hash_payload else None,
                    },
                    "identity": _stamp(info),
                }
            await asyncio.sleep(0)
    if _stamp(before) != _stamp(os.fstat(parent)):
        raise ValueError("Source directory membership changed during catalog capture")


@dataclass(frozen=True)
class _Run:
    name: str
    pages: int
    entries: int
    byte_count: int


class _Writer:
    def __init__(self, stage: int, work: _Work) -> None:
        self.stage, self.work = stage, work
        self.name = "run-" + uuid4().hex
        os.mkdir(self.name, 0o700, dir_fd=stage)
        self.fd = _directory(self.name, stage, private=True)
        self.buffer = bytearray()
        self.pages = self.entries = self.page_entries = self.bytes = 0

    def add(self, record: Data) -> None:
        raw = _json(record) + b"\n"
        if len(raw) > self.work.budget.chunk_bytes:
            raise ValueError("A source metadata record exceeds its configured processing page")
        if self.buffer and (
            len(self.buffer) + len(raw) > self.work.budget.chunk_bytes
            or self.page_entries >= self.work.budget.page_entries
        ):
            self.flush()
        self.buffer.extend(raw)
        self.entries += 1
        self.page_entries += 1

    def flush(self) -> None:
        if not self.buffer:
            return
        raw = bytes(self.buffer)
        self.work.put(self.fd, f"page-{self.pages:016x}.jsonl", raw)
        self.bytes += len(raw)
        self.pages += 1
        self.buffer.clear()
        self.page_entries = 0

    def finish(self) -> _Run:
        try:
            self.flush()
            os.fsync(self.fd)
            return _Run(self.name, self.pages, self.entries, self.bytes)
        finally:
            self.close()

    def close(self) -> None:
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1


def _records(stage: int, run: _Run, work: _Work) -> Generator[Data]:
    fd = _directory(run.name, stage, private=True)
    try:
        count = 0
        for page in range(run.pages):
            work.tick()
            for raw in _read(fd, f"page-{page:016x}.jsonl", work.budget.chunk_bytes).splitlines():
                value = json.loads(raw)
                if not isinstance(value, dict) or set(value) != {"entry", "identity"}:
                    raise ValueError("The sorted source metadata record changed")
                count += 1
                yield value
        if count != run.entries:
            raise ValueError("The sorted source run entry count changed")
    finally:
        os.close(fd)


def _discard(stage: int, run: _Run, work: _Work) -> None:
    fd = _directory(run.name, stage, private=True)
    try:
        for page in range(run.pages):
            os.unlink(f"page-{page:016x}.jsonl", dir_fd=fd)
    finally:
        os.close(fd)
    os.rmdir(run.name, dir_fd=stage)
    work.used -= run.byte_count


class _Sort:
    """Online fan-in compaction retains only bounded records and logarithmic run references."""

    def __init__(self, stage: int, work: _Work) -> None:
        self.stage, self.work = stage, work
        self.batch: list[Data] = []
        self.batch_bytes = 0
        self.levels: list[list[_Run]] = []

    def add(self, record: Data) -> None:
        self.work.tick()
        size = len(_json(record)) + 1
        if size > self.work.budget.chunk_bytes:
            raise ValueError("A source metadata record exceeds its configured processing page")
        if self.batch and (
            len(self.batch) >= self.work.budget.sort_entries or self.batch_bytes + size > self.work.budget.chunk_bytes
        ):
            self.flush()
        self.batch.append(record)
        self.batch_bytes += size

    def merge(self, runs: list[_Run]) -> _Run:
        writer = _Writer(self.stage, self.work)
        streams = [_records(self.stage, run, self.work) for run in runs]
        heap: list[tuple[str, int, Data]] = []
        try:
            for ordinal, stream in enumerate(streams):
                item = next(stream, None)
                if item is not None:
                    heapq.heappush(heap, (item["entry"]["path"], ordinal, item))
            while heap:
                self.work.tick()
                _, ordinal, value = heapq.heappop(heap)
                writer.add(value)
                item = next(streams[ordinal], None)
                if item is not None:
                    heapq.heappush(heap, (item["entry"]["path"], ordinal, item))
            result = writer.finish()
        except BaseException:
            writer.close()
            raise
        finally:
            for stream in streams:
                stream.close()
        for run in runs:
            _discard(self.stage, run, self.work)
        return result

    def accept(self, run: _Run, level: int = 0) -> None:
        while len(self.levels) <= level:
            self.levels.append([])
        self.levels[level].append(run)
        if len(self.levels[level]) == self.work.budget.merge_fan_in:
            merged = self.merge(self.levels[level])
            self.levels[level] = []
            self.accept(merged, level + 1)

    def flush(self) -> None:
        writer = _Writer(self.stage, self.work)
        try:
            for record in sorted(self.batch, key=lambda value: value["entry"]["path"]):
                writer.add(record)
            result = writer.finish()
        except BaseException:
            writer.close()
            raise
        self.batch.clear()
        self.batch_bytes = 0
        self.accept(result)

    def finish(self) -> _Run:
        if self.batch:
            self.flush()
        runs = [run for level in self.levels for run in level]
        if not runs:
            return _Writer(self.stage, self.work).finish()
        while len(runs) > 1:
            runs = [
                self.merge(runs[start : start + self.work.budget.merge_fan_in])
                for start in range(0, len(runs), self.work.budget.merge_fan_in)
            ]
        return runs[0]


class _Tree:
    def __init__(self, stage: int, work: _Work) -> None:
        self.stage, self.work = stage, work
        self.width = work.budget.chunk_bytes // 32
        self.buffers: list[bytearray] = []
        self.counts: list[int] = []
        self.last: list[bytes] = []

    def add(self, value: bytes, level: int = 0) -> None:
        while len(self.buffers) <= level:
            self.buffers.append(bytearray())
            self.counts.append(0)
            self.last.append(b"")
        self.buffers[level].extend(value)
        if len(self.buffers[level]) == self.width * 32:
            self.flush(level)

    def flush(self, level: int) -> None:
        raw = bytes(self.buffers[level])
        self.buffers[level].clear()
        self.work.put(self.stage, f"hash-{level:04x}-{self.counts[level]:016x}.bin", raw)
        hashed = hashlib.sha256(raw).digest()
        self.last[level] = hashed
        self.counts[level] += 1
        self.add(hashed, level + 1)

    def finish(self) -> Data:
        if not self.buffers:
            return {"levels": 0, "sha256": hashlib.sha256(b"").hexdigest()}
        level = 0
        while True:
            if self.buffers[level]:
                self.flush(level)
            if self.counts[level] == 1:
                return {"levels": level + 1, "sha256": self.last[level].hex()}
            level += 1


def _validate_record(value: Data) -> SourceCatalogEntryV1:
    if (
        set(value) != {"entry", "identity"}
        or not isinstance(value["identity"], list)
        or len(value["identity"]) != 9
        or any(type(v) is not int for v in value["identity"])
    ):
        raise ValueError("Source catalog identity metadata is malformed")
    entry = SourceCatalogEntryV1.model_validate(value["entry"])
    path = entry.path.removesuffix("/") if entry.kind == "directory" else entry.path
    if not path or path.startswith("/") or "\0" in path or any(p in {"", ".", ".."} for p in path.split("/")):
        raise ValueError("Source catalog paths must retain exact relative identities")
    if entry.kind == "file":
        if not stat.S_ISREG(entry.mode) or entry.sha256 is None or re.fullmatch(_HEX, entry.sha256) is None:
            raise ValueError("Source file hash or mode is invalid")
    elif not entry.path.endswith("/") or not stat.S_ISDIR(entry.mode) or entry.sha256 is not None or entry.byte_count:
        raise ValueError("Source directory metadata is invalid")
    return entry


def _assert_record(root: int, record: Data) -> None:
    _assert_identity(root, _validate_record(record), record["identity"])


def _assert_identity(root: int, entry: SourceCatalogEntryV1, identity: list[int]) -> None:
    parts = entry.path.rstrip("/").split("/")
    parent = os.dup(root)
    target = None
    try:
        for name in parts[:-1]:
            child = _directory(name, parent)
            os.close(parent)
            parent = child
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
        if entry.kind == "directory":
            flags |= os.O_DIRECTORY
        target = os.open(parts[-1], flags, dir_fd=parent)
        if (
            _stamp(os.fstat(target)) != identity
            or _stamp(os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)) != identity
        ):
            raise ValueError("Source metadata changed before catalog publication")
    finally:
        if target is not None:
            os.close(target)
        os.close(parent)


def _cleanup(parent: int, name: str, expected: tuple[int, int]) -> None:
    with suppress(FileNotFoundError):
        info = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if not stat.S_ISDIR(info.st_mode) or (info.st_dev, info.st_ino) != expected:
            return
        fd = _directory(name, parent, private=True)
        try:
            with os.scandir(fd) as entries:
                for item in entries:
                    if re.fullmatch(r"run-[a-f0-9]{32}", item.name):
                        child = _directory(item.name, fd, private=True)
                        try:
                            with os.scandir(child) as pages:
                                for page in pages:
                                    if re.fullmatch(r"page-[a-f0-9]{16}\.jsonl", page.name):
                                        os.unlink(page.name, dir_fd=child)
                            os.rmdir(item.name, dir_fd=fd)
                        finally:
                            os.close(child)
                    elif item.name == "manifest.json" or re.fullmatch(r"hash-[a-f0-9]{4}-[a-f0-9]{16}\.bin", item.name):
                        os.unlink(item.name, dir_fd=fd)
            os.rmdir(name, dir_fd=parent)
        finally:
            os.close(fd)


async def retain_source_catalog(
    workspace: Path,
    catalog_root: Path,
    *,
    budget: SourceCatalogBudget,
    assert_current: Callable[[], Awaitable[None]],
    cancel_requested: Callable[[], bool] | None = None,
) -> SourceCatalogRefV1:
    """Capture metadata under an exclusively owned workspace; the caller supplies authority."""
    if not isinstance(budget, SourceCatalogBudget) or not callable(assert_current):
        raise TypeError("A typed resource budget and current-authority callback are required")
    if (
        not workspace.is_absolute()
        or workspace.resolve() != workspace
        or not catalog_root.is_absolute()
        or catalog_root.resolve() != catalog_root
        or catalog_root.is_relative_to(workspace)
    ):
        raise ValueError("Source and catalog roots must be separate canonical absolute directories")
    source = _directory(workspace)
    root = folder = None
    stage = "." + uuid4().hex + ".partial"
    artifact_id = uuid4().hex
    identity = None
    published = False
    try:
        root = _directory(catalog_root, private=True)
        root_identity = os.fstat(root)
        work = _Work(root, budget, cancel_requested)
        work.tick()
        async with asyncio.timeout(budget.timeout_seconds):
            await assert_current()
            source_identity = _stamp(os.fstat(source))
            os.mkdir(stage, 0o700, dir_fd=root)
            folder = _directory(stage, root, private=True)
            info = os.fstat(folder)
            identity = (info.st_dev, info.st_ino)
            sorter = _Sort(folder, work)
            walker = _walk(source, "", work)
            try:
                async for record in walker:
                    sorter.add(record)
            finally:
                await walker.aclose()
            run = sorter.finish()
            files_hash = hashlib.sha256(b"{")
            source_hash = hashlib.sha256(b'{"files":{')
            previous = None
            entries = file_count = total = 0
            for record in _records(folder, run, work):
                entry = _validate_record(record)
                if previous is not None and entry.path <= previous:
                    raise ValueError("Source catalog ordering contains duplicate or changed paths")
                if entry.kind == "directory":
                    text = str(entry.mode)
                else:
                    assert entry.sha256 is not None
                    text = entry.sha256 + str(entry.mode)
                encoded = (b"," if entries else b"") + _json(entry.path) + b":" + _json(text)
                files_hash.update(encoded)
                source_hash.update(encoded)
                previous = entry.path
                entries += 1
                if entry.kind == "file":
                    file_count += 1
                    total += entry.byte_count
            files_hash.update(b"}")
            source_hash.update(b'},"protocol":"forge-verification-source-v1"}')
            tree = _Tree(folder, work)
            data = _directory(run.name, folder, private=True)
            try:
                for page in range(run.pages):
                    tree.add(hashlib.sha256(_read(data, f"page-{page:016x}.jsonl", budget.chunk_bytes)).digest())
            finally:
                os.close(data)
            proof = tree.finish()
            await assert_current()
            for record in _records(folder, run, work):
                _assert_record(source, record)
            if (
                _stamp(os.fstat(source)) != source_identity
                or _stamp(os.stat(workspace, follow_symlinks=False)) != source_identity
            ):
                raise ValueError("Source root or membership changed before catalog publication")
            now = os.stat(catalog_root, follow_symlinks=False)
            if (now.st_dev, now.st_ino) != (root_identity.st_dev, root_identity.st_ino):
                raise ValueError("The catalog root changed before publication")
            current_stage = os.stat(stage, dir_fd=root, follow_symlinks=False)
            if (current_stage.st_dev, current_stage.st_ino) != identity:
                raise ValueError("The owned catalog staging identity changed")
            work.tick()
            public: Data = {
                "version": 1,
                "artifact_id": artifact_id,
                "source_digest": source_hash.hexdigest(),
                "manifest_digest": files_hash.hexdigest(),
                "byte_count": total,
                "file_count": file_count,
                "entry_count": entries,
                "page_count": run.pages,
            }
            manifest = _json(
                {
                    **public,
                    "budget": budget.model_dump(),
                    "data_run": run.name,
                    "proof": proof,
                }
            )
            work.put(folder, "manifest.json", manifest)
            os.fsync(folder)
            work.tick()
            try:
                os.stat(artifact_id, dir_fd=root, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise ValueError("Catalog identity already exists")
            os.rename(stage, artifact_id, src_dir_fd=root, dst_dir_fd=root)
            os.fsync(root)
            published = True
            return SourceCatalogRefV1(**public, manifest_sha256=hashlib.sha256(manifest).hexdigest())
    finally:
        os.close(source)
        if folder is not None:
            os.close(folder)
        if root is not None:
            try:
                if not published and identity is not None:
                    _cleanup(root, stage, identity)
            finally:
                os.close(root)


def _expected(folder: int, proof: Data, width: int, page: int) -> bytes:
    fanout = width // 32
    wanted = bytes.fromhex(proof["sha256"])
    for level in reversed(range(proof["levels"])):
        index = page // fanout ** (level + 1)
        slot = (page // fanout**level) % fanout
        raw = _read(folder, f"hash-{level:04x}-{index:016x}.bin", width)
        if len(raw) % 32 or hashlib.sha256(raw).digest() != wanted:
            raise ValueError("The source catalog integrity index changed")
        wanted = raw[slot * 32 : (slot + 1) * 32]
        if len(wanted) != 32:
            raise ValueError("The source catalog integrity entry is missing")
    return wanted


def read_source_catalog_page(catalog_root: Path, reference: SourceCatalogRefV1, page: int) -> SourceCatalogPageV1:
    if (
        not isinstance(reference, SourceCatalogRefV1)
        or type(page) is not int
        or page < 0
        or (page >= reference.page_count and not (page == 0 and reference.entry_count == 0))
    ):
        raise ValueError("The requested catalog page is outside its retained scope")
    if not catalog_root.is_absolute() or catalog_root.resolve() != catalog_root:
        raise ValueError("Catalog reads require a canonical private root")
    root = _directory(catalog_root, private=True)
    try:
        folder = _directory(reference.artifact_id, root, private=True)
    finally:
        os.close(root)
    try:
        encoded = _read(folder, "manifest.json", 1048576)
        if hashlib.sha256(encoded).hexdigest() != reference.manifest_sha256:
            raise ValueError("The source catalog manifest integrity changed")
        manifest = json.loads(encoded)
        if any(manifest.get(key) != value for key, value in reference.model_dump(exclude={"manifest_sha256"}).items()):
            raise ValueError("The source catalog reference differs from its manifest")
        budget = SourceCatalogBudget.model_validate(manifest["budget"])
        name = manifest["data_run"]
        if not isinstance(name, str) or re.fullmatch(r"run-[a-f0-9]{32}", name) is None:
            raise ValueError("The source catalog page namespace is invalid")
        entries = []
        if reference.page_count:
            wanted = _expected(folder, manifest["proof"], budget.chunk_bytes, page)
            data = _directory(name, folder, private=True)
            try:
                raw = _read(data, f"page-{page:016x}.jsonl", budget.chunk_bytes)
            finally:
                os.close(data)
            if hashlib.sha256(raw).digest() != wanted:
                raise ValueError("The source catalog page integrity changed")
            entries = [_validate_record(json.loads(line)) for line in raw.splitlines()]
        return SourceCatalogPageV1(
            artifact_id=reference.artifact_id,
            manifest_sha256=reference.manifest_sha256,
            page=page,
            next_page=page + 1 if page + 1 < reference.page_count else None,
            entries=entries,
        )
    finally:
        os.close(folder)


async def revalidate_source_catalog(
    workspace: Path,
    catalog_root: Path,
    reference: SourceCatalogRefV1,
    *,
    assert_current: Callable[[], Awaitable[None]],
    cancel_requested: Callable[[], bool] | None = None,
) -> None:
    """Compare selected public membership and identities without rereading source payloads."""
    if workspace.resolve() != workspace or catalog_root.resolve() != catalog_root:
        raise ValueError("Catalog validation requires canonical source and storage roots")
    source = _directory(workspace)
    root = folder = stage_fd = None
    stage = "." + uuid4().hex + ".partial"
    stage_identity = None
    try:
        root = _directory(catalog_root, private=True)
        folder = _directory(reference.artifact_id, root, private=True)
        encoded = _read(folder, "manifest.json", 1048576)
        if hashlib.sha256(encoded).hexdigest() != reference.manifest_sha256:
            raise ValueError("The source catalog manifest integrity changed")
        manifest = json.loads(encoded)
        if any(manifest.get(key) != value for key, value in reference.model_dump(exclude={"manifest_sha256"}).items()):
            raise ValueError("The source catalog reference differs from its manifest")
        budget = SourceCatalogBudget.model_validate(manifest["budget"])
        work = _Work(root, budget, cancel_requested)
        name = manifest["data_run"]
        if not isinstance(name, str) or re.fullmatch(r"run-[a-f0-9]{32}", name) is None:
            raise ValueError("The source catalog page namespace is invalid")
        await assert_current()
        data = _directory(name, folder, private=True)
        try:
            for page in range(reference.page_count):
                work.tick()
                raw = _read(data, f"page-{page:016x}.jsonl", budget.chunk_bytes)
                if hashlib.sha256(raw).digest() != _expected(folder, manifest["proof"], budget.chunk_bytes, page):
                    raise ValueError("The source catalog page integrity changed")
                for line in raw.splitlines():
                    _validate_record(json.loads(line))
                await asyncio.sleep(0)
        finally:
            os.close(data)
        await assert_current()
        os.mkdir(stage, 0o700, dir_fd=root)
        stage_fd = _directory(stage, root, private=True)
        info = os.fstat(stage_fd)
        stage_identity = (info.st_dev, info.st_ino)
        sorter = _Sort(stage_fd, work)
        walker = _walk(source, "", work, hash_payload=False)
        try:
            async for record in walker:
                sorter.add(record)
        finally:
            await walker.aclose()
        current_run = sorter.finish()
        original = _records(folder, _Run(name, reference.page_count, reference.entry_count, 0), work)
        current = _records(stage_fd, current_run, work)
        count = 0
        try:
            while True:
                before, after = next(original, None), next(current, None)
                if before is None and after is None:
                    break
                if before is None or after is None:
                    raise ValueError("Selected public source membership changed")
                left, right = before["entry"], after["entry"]
                if {k: v for k, v in left.items() if k != "sha256"} != {
                    k: v for k, v in right.items() if k != "sha256"
                }:
                    raise ValueError("Selected public source metadata changed")
                # Directory timestamps can change when an excluded generated cache appears.
                # Explicit public membership comparison above replaces that timestamp authority.
                identity = before["identity"][:5] if left["kind"] == "directory" else before["identity"]
                actual = after["identity"][:5] if right["kind"] == "directory" else after["identity"]
                if identity != actual:
                    raise ValueError("Selected public source identity changed")
                _assert_identity(source, _validate_record(before), after["identity"])
                count += 1
        finally:
            original.close()
            current.close()
        if count != reference.entry_count:
            raise ValueError("The selected public source entry count changed")
        work.tick()
    finally:
        os.close(source)
        if stage_fd is not None:
            os.close(stage_fd)
        if folder is not None:
            os.close(folder)
        if root is not None:
            try:
                if stage_identity is not None:
                    _cleanup(root, stage, stage_identity)
            finally:
                os.close(root)
