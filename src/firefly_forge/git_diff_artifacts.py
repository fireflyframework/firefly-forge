# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Elastic private chunk storage and integrity-checked native Git diff pages."""

from __future__ import annotations

import asyncio
import base64
import codecs
import hashlib
import json
import os
import re
import signal
import stat
import struct
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from firefly_forge.git_diff_models import GitDiffBudget, GitDiffFileV1, GitDiffIndexPageV1, GitDiffPageV1, GitDiffRefV1
from firefly_forge.git_workspace import git_environment, trusted_git
from firefly_forge.sandbox import _directory_fd

Data = dict[str, Any]
ScanChunk = Callable[[bytes, bool], Awaitable[None]]
_META = struct.Struct(">QI32s")
_HEADER, _MARKER = b"diff --git ", b"\ndiff --git "


def _json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _root(path: Path) -> int:
    if not path.is_absolute() or path.resolve() != path:
        raise ValueError("Artifact roots must be canonical absolute directories")
    fd = _directory_fd(path)
    value = os.fstat(fd)
    if value.st_uid != os.getuid() or stat.S_IMODE(value.st_mode) != 0o700:
        os.close(fd)
        raise ValueError("Artifact roots must be private owner-controlled directories")
    return fd


def _identity(value: os.stat_result) -> tuple[int, ...]:
    return (value.st_dev, value.st_ino, value.st_uid, value.st_mode, value.st_nlink, value.st_size, value.st_mtime_ns)


def _read(parent: int, name: str, size: int | None = None) -> int:
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    value = os.fstat(fd)
    if (
        not stat.S_ISREG(value.st_mode)
        or value.st_uid != os.getuid()
        or value.st_nlink != 1
        or stat.S_IMODE(value.st_mode) != 0o400
        or (size is not None and value.st_size != size)
    ):
        os.close(fd)
        raise ValueError("The immutable artifact file identity changed")
    return fd


def _write(parent: int, name: str, raw: bytes) -> None:
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
    with os.fdopen(fd, "wb") as target:
        target.write(raw)
        target.flush()
        os.fsync(target.fileno())
        os.fchmod(target.fileno(), 0o400)


class _Hashes:
    """One bounded buffer per Merkle level; never a project-sized chunk list."""

    def __init__(self, folder: int, prefix: str, width: int, tick: Callable[[], None]) -> None:
        self.folder, self.prefix, self.width, self.tick = folder, prefix, width, tick
        self.fanout = width // 32
        self.buffers: list[bytearray] = []
        self.counts: list[int] = []
        self.last: list[bytes] = []

    def add(self, value: bytes, level: int = 0) -> None:
        if len(value) != 32:
            raise ValueError("A chunk index requires exact SHA-256 entries")
        while len(self.buffers) <= level:
            self.buffers.append(bytearray())
            self.counts.append(0)
            self.last.append(b"")
        self.buffers[level].extend(value)
        if len(self.buffers[level]) == self.fanout * 32:
            self._flush(level)

    def _flush(self, level: int) -> None:
        self.tick()
        raw = bytes(self.buffers[level])
        self.buffers[level].clear()
        _write(self.folder, f"{self.prefix}-{level:04d}-{self.counts[level]:016x}.bin", raw)
        value = hashlib.sha256(raw).digest()
        self.last[level] = value
        self.counts[level] += 1
        self.add(value, level + 1)

    def finish(self) -> Data:
        if not self.buffers:
            return {"levels": 0, "root": hashlib.sha256(b"").hexdigest()}
        level = 0
        while True:
            if self.buffers[level]:
                self._flush(level)
            if self.counts[level] == 1:
                return {"levels": level + 1, "root": self.last[level].hex()}
            level += 1


def _expected(folder: int, prefix: str, proof: Data, width: int, index: int, tick: Callable[[], None]) -> bytes:
    fanout = width // 32
    expected = bytes.fromhex(proof["root"])
    for level in reversed(range(proof["levels"])):
        tick()
        page = index // (fanout ** (level + 1))
        slot = (index // (fanout**level)) % fanout
        fd = _read(folder, f"{prefix}-{level:04d}-{page:016x}.bin")
        try:
            before = os.fstat(fd)
            raw = os.pread(fd, width + 1, 0)
            if (
                len(raw) > width
                or len(raw) % 32
                or hashlib.sha256(raw).digest() != expected
                or _identity(before) != _identity(os.fstat(fd))
            ):
                raise ValueError("The retained Merkle index integrity changed")
            expected = raw[slot * 32 : (slot + 1) * 32]
            if len(expected) != 32:
                raise ValueError("The requested index entry is unavailable")
        finally:
            os.close(fd)
    return expected


def _path(header: bytes) -> str:
    line = header.removeprefix(_HEADER)
    if line.startswith(b'"'):
        values: list[bytes] = []
        cursor = 0
        for _ in range(2):
            if line[cursor : cursor + 1] != b'"':
                raise ValueError("The Git path header is malformed")
            end = cursor + 1
            while end < len(line):
                if line[end] == 92:
                    end += 2
                    continue
                if line[end] == 34:
                    break
                end += 1
            if end >= len(line):
                raise ValueError("The Git path header is malformed")
            values.append(cast(bytes, codecs.escape_decode(line[cursor + 1 : end])[0]))
            cursor = end + 1
            if len(values) == 1:
                if line[cursor : cursor + 1] != b" ":
                    raise ValueError("The Git path header is malformed")
                cursor += 1
        if cursor != len(line):
            raise ValueError("The Git path header is malformed")
        old, new = values
    else:
        length = len(line) - 5
        if length < 0 or length % 2:
            raise ValueError("The Git path header is malformed")
        width = length // 2
        old, new = line[: width + 2], line[width + 3 :]
    if not old.startswith(b"a/") or not new.startswith(b"b/") or old[2:] != new[2:]:
        raise ValueError("The fixed no-rename path identity changed")
    name = new[2:].decode("utf-8")
    if not name or name.startswith("/") or "\0" in name or any(part in {"", ".", ".."} for part in name.split("/")):
        raise ValueError("The diff path leaves its relative source identity")
    return name


class _Files:
    def __init__(self, folder: int, budget: GitDiffBudget, tick: Callable[[], None]) -> None:
        self.folder, self.budget, self.tick = folder, budget, tick
        self.width = max(16384, budget.chunk_bytes)
        self.files: list[Data] = []
        self.page_count = 0
        self.file_count = 0
        self.metadata = bytearray()
        self.meta_count = 0
        self.hashes = _Hashes(folder, "files-hash", budget.chunk_bytes, tick)
        self.current: Data | None = None
        self.hasher: Any = None
        self.header = bytearray()
        self.offset = 0
        self.pending = b""

    def _meta_flush(self) -> None:
        if not self.metadata:
            return
        raw = bytes(self.metadata)
        self.metadata.clear()
        self.tick()
        _write(self.folder, f"files-meta-{self.meta_count:016x}.bin", raw)
        self.hashes.add(hashlib.sha256(raw).digest())
        self.meta_count += 1

    def _flush(self) -> None:
        if not self.files:
            return
        raw = _json(self.files)
        self.tick()
        _write(self.folder, f"files-{self.page_count:016x}.json", raw)
        record = _META.pack(self.files[0]["file_index"], len(self.files), hashlib.sha256(raw).digest())
        if len(self.metadata) + len(record) > self.width:
            self._meta_flush()
        self.metadata.extend(record)
        self.page_count += 1
        self.files = []

    def _finish_file(self, end: int) -> None:
        if self.current is None:
            return
        if "path" not in self.current:
            raise ValueError("The diff file header is incomplete")
        record = {**self.current, "byte_count": end - self.current["offset"], "sha256": self.hasher.hexdigest()}
        if len(_json([record])) > self.width:
            raise ValueError("A file descriptor exceeds its index transport page")
        if self.files and (
            len(self.files) >= self.budget.index_page_files or len(_json([*self.files, record])) > self.width
        ):
            self._flush()
        self.files.append(record)
        self.file_count += 1

    def _data(self, raw: bytes) -> None:
        if not raw:
            return
        if self.current is None:
            raise ValueError("The Git diff has data before its first file")
        self.hasher.update(raw)
        if "path" not in self.current:
            self.header.extend(raw.split(b"\n", 1)[0])
            if len(self.header) > 65536:
                raise ValueError("The Git path header exceeds its processing window")
            if b"\n" in raw:
                self.current["path"] = _path(bytes(self.header))
                self.header.clear()

    def feed(self, raw: bytes, final: bool = False) -> None:
        self.tick()
        data = self.pending + raw
        take = len(data) if final else max(0, len(data) - len(_MARKER))
        starts = []
        if self.offset == 0 and data.startswith(_HEADER):
            starts.append(0)
        at = 0
        while (at := data.find(_MARKER, at)) >= 0:
            if at + 1 <= take:
                starts.append(at + 1)
            at += len(_MARKER)
        cursor = 0
        for start in starts:
            self._data(data[cursor:start])
            self._finish_file(self.offset + start)
            self.current = {"file_index": self.file_count, "offset": self.offset + start}
            self.hasher = hashlib.sha256()
            self.header.clear()
            cursor = start
        self._data(data[cursor:take])
        self.offset += take
        self.pending = data[take:]
        if final:
            self._finish_file(self.offset)
            self.current = None
            self._flush()
            self._meta_flush()


async def _stop(child: asyncio.subprocess.Process | None) -> None:
    if child is None or child.returncode is not None:
        return
    with suppress(ProcessLookupError):
        os.killpg(child.pid, signal.SIGTERM)
    try:
        await asyncio.wait_for(child.wait(), 1)
    except TimeoutError:
        with suppress(ProcessLookupError):
            os.killpg(child.pid, signal.SIGKILL)
        await asyncio.wait_for(child.wait(), 1)


def _cleanup(root: int, name: str, identity: tuple[int, int]) -> None:
    with suppress(FileNotFoundError):
        info = os.stat(name, dir_fd=root, follow_symlinks=False)
        if not stat.S_ISDIR(info.st_mode) or (info.st_dev, info.st_ino) != identity:
            return
        folder = _directory_fd(name, parent=root)
        try:
            with os.scandir(folder) as entries:
                for entry in entries:
                    if entry.name == "manifest.json" or re.fullmatch(
                        r"(?:chunk-[a-f0-9]{16}\.bin|chunks-[0-9]{4}-[a-f0-9]{16}\.bin|files-[a-f0-9]{16}\.json|files-meta-[a-f0-9]{16}\.bin|files-hash-[0-9]{4}-[a-f0-9]{16}\.bin)",
                        entry.name,
                    ):
                        os.unlink(entry.name, dir_fd=folder)
            os.rmdir(name, dir_fd=root)
        finally:
            os.close(folder)


async def retain_git_diff(
    workspace: Path, base: str, artifactroot: Path, *, budget: GitDiffBudget, scan_chunk: ScanChunk, cached: bool = True
) -> GitDiffRefV1:
    """Retain exact Git output privately, publishing only after admission succeeds."""
    if (
        not isinstance(budget, GitDiffBudget)
        or type(cached) is not bool
        or not callable(scan_chunk)
        or not re.fullmatch(r"(?:[a-f0-9]{40}|[a-f0-9]{64})", base)
    ):
        raise ValueError("A typed budget, scanner and exact base revision are required")
    if workspace.resolve() != workspace or not workspace.is_dir() or artifactroot.is_relative_to(workspace):
        raise ValueError("The source and private artifact roots must be separate canonical directories")
    root = _root(artifactroot)
    root_id = os.fstat(root)
    deadline = time.monotonic() + budget.timeout_seconds
    artifact_id = uuid4().hex
    stage = f".{artifact_id}.partial"
    folder = -1
    child = None
    stderr_task = None
    published = False
    stage_id = None

    def tick() -> None:
        now = os.stat(artifactroot, follow_symlinks=False)
        space = os.fstatvfs(root)
        if (now.st_dev, now.st_ino) != (root_id.st_dev, root_id.st_ino):
            raise ValueError("The artifact root identity changed")
        if time.monotonic() >= deadline:
            raise TimeoutError("The diff retention deadline expired")
        if space.f_bavail * space.f_frsize < budget.min_free_bytes:
            raise ValueError("The private diff storage free-space floor was reached")

    async def drain(stream: asyncio.StreamReader) -> None:
        count = 0
        while raw := await stream.read(4096):
            count += len(raw)
            if count > 65536:
                raise ValueError("The Git diagnostic stream exceeded its bounded window")

    try:
        tick()
        os.mkdir(stage, mode=0o700, dir_fd=root)
        folder = _directory_fd(stage, parent=root)
        info = os.fstat(folder)
        stage_id = (info.st_dev, info.st_ino)
        chunks = _Hashes(folder, "chunks", budget.chunk_bytes, tick)
        files = _Files(folder, budget, tick)
        whole = hashlib.sha256()
        total = count = 0
        argv = [
            "git",
            "-c",
            "core.quotePath=true",
            "diff",
            "--binary",
            "--full-index",
            "--no-renames",
            "--no-ext-diff",
            "--no-textconv",
            "--no-color",
            "--src-prefix=a/",
            "--dst-prefix=b/",
        ]
        if cached:
            argv.append("--cached")
        argv.extend([base, "--"])
        async with asyncio.timeout(budget.timeout_seconds):
            child = await asyncio.create_subprocess_exec(
                *trusted_git(argv),
                cwd=workspace,
                env={"PATH": os.defpath, "HOME": os.devnull, **git_environment()},
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
                limit=budget.chunk_bytes,
            )
            assert child.stdout is not None and child.stderr is not None
            stderr_task = asyncio.create_task(drain(child.stderr))
            while True:
                try:
                    raw = await child.stdout.readexactly(budget.chunk_bytes)
                except asyncio.IncompleteReadError as error:
                    raw = error.partial
                if not raw:
                    break
                tick()
                total += len(raw)
                if budget.max_bytes is not None and total > budget.max_bytes:
                    raise ValueError("The configured total diff storage budget was exceeded")
                if stderr_task.done():
                    stderr_task.result()
                await scan_chunk(raw, False)
                tick()
                _write(folder, f"chunk-{count:016x}.bin", raw)
                chunks.add(hashlib.sha256(raw).digest())
                files.feed(raw)
                whole.update(raw)
                count += 1
            status = await child.wait()
            await stderr_task
            if status != 0:
                raise ValueError("Git could not produce the requested diff")
            files.feed(b"", True)
            chunk_proof = chunks.finish()
            file_proof = files.hashes.finish()
            await scan_chunk(b"", True)
            tick()
            public: Data = {
                "version": 1,
                "artifact_id": artifact_id,
                "sha256": whole.hexdigest(),
                "byte_count": total,
                "file_count": files.file_count,
                "index_page_count": files.page_count,
            }
            manifest = {
                **public,
                "budget": budget.model_dump(),
                "chunk_count": count,
                "chunk_proof": chunk_proof,
                "file_proof": file_proof,
                "base": base,
                "cached": cached,
            }
            encoded = _json(manifest)
            _write(folder, "manifest.json", encoded)
            os.fsync(folder)
            tick()
            try:
                os.stat(artifact_id, dir_fd=root, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise ValueError("The artifact identity already exists")
            os.rename(stage, artifact_id, src_dir_fd=root, dst_dir_fd=root)
            os.fsync(root)
            published = True
            return GitDiffRefV1(**public, manifest_sha256=hashlib.sha256(encoded).hexdigest())
    finally:
        await asyncio.shield(_stop(child))
        if stderr_task is not None and not stderr_task.done():
            stderr_task.cancel()
            with suppress(asyncio.CancelledError):
                await stderr_task
        if folder >= 0:
            os.close(folder)
        if not published and stage_id is not None:
            _cleanup(root, stage, stage_id)
        os.close(root)


retain_diff = retain_git_diff


def _open(artifactroot: Path, ref: GitDiffRefV1) -> tuple[int, Data, GitDiffBudget, Callable[[], None]]:
    if not isinstance(ref, GitDiffRefV1):
        raise ValueError("An exact typed artifact reference is required")
    root = _root(artifactroot)
    try:
        folder = _directory_fd(ref.artifact_id, parent=root)
    finally:
        os.close(root)
    try:
        info = os.fstat(folder)
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise ValueError("The artifact directory identity changed")
        fd = _read(folder, "manifest.json")
        try:
            before = os.fstat(fd)
            raw = os.pread(fd, 65537, 0)
            if (
                len(raw) > 65536
                or hashlib.sha256(raw).hexdigest() != ref.manifest_sha256
                or _identity(before) != _identity(os.fstat(fd))
            ):
                raise ValueError("The artifact manifest integrity changed")
        finally:
            os.close(fd)
        manifest = json.loads(raw)
        if any(manifest.get(key) != value for key, value in ref.model_dump(exclude={"manifest_sha256"}).items()):
            raise ValueError("The artifact reference does not match its manifest")
        budget = GitDiffBudget.model_validate(manifest["budget"])
        deadline = time.monotonic() + budget.timeout_seconds

        def tick() -> None:
            if time.monotonic() >= deadline:
                raise TimeoutError("The artifact read deadline expired")

        return folder, manifest, budget, tick
    except BaseException:
        os.close(folder)
        raise


def _content(
    folder: int, manifest: Data, budget: GitDiffBudget, tick: Callable[[], None], offset: int, count: int
) -> bytes:
    result = []
    end = offset + count
    while offset < end:
        tick()
        index = offset // budget.chunk_bytes
        start = index * budget.chunk_bytes
        expected = _expected(folder, "chunks", manifest["chunk_proof"], budget.chunk_bytes, index, tick)
        size = min(budget.chunk_bytes, manifest["byte_count"] - start)
        fd = _read(folder, f"chunk-{index:016x}.bin", size)
        try:
            before = os.fstat(fd)
            raw = os.pread(fd, size + 1, 0)
            if (
                len(raw) != size
                or hashlib.sha256(raw).digest() != expected
                or _identity(before) != _identity(os.fstat(fd))
            ):
                raise ValueError("The retained diff content integrity changed")
        finally:
            os.close(fd)
        part = raw[offset - start : min(size, end - start)]
        if not part:
            raise ValueError("The requested content is unavailable")
        result.append(part)
        offset += len(part)
    return b"".join(result)


def _file_meta(
    folder: int, manifest: Data, budget: GitDiffBudget, tick: Callable[[], None], page: int
) -> tuple[int, int, bytes]:
    per_page = max(16384, budget.chunk_bytes) // _META.size
    index = page // per_page
    expected = _expected(folder, "files-hash", manifest["file_proof"], budget.chunk_bytes, index, tick)
    fd = _read(folder, f"files-meta-{index:016x}.bin")
    try:
        before = os.fstat(fd)
        raw = os.pread(fd, max(16384, budget.chunk_bytes) + 1, 0)
        if (
            len(raw) > max(16384, budget.chunk_bytes)
            or len(raw) % _META.size
            or hashlib.sha256(raw).digest() != expected
            or _identity(before) != _identity(os.fstat(fd))
        ):
            raise ValueError("The file index metadata integrity changed")
    finally:
        os.close(fd)
    offset = (page % per_page) * _META.size
    if offset + _META.size > len(raw):
        raise ValueError("The requested file index is unavailable")
    return _META.unpack(raw[offset : offset + _META.size])


def _index(
    folder: int, manifest: Data, budget: GitDiffBudget, tick: Callable[[], None], page: int
) -> list[GitDiffFileV1]:
    first, count, expected = _file_meta(folder, manifest, budget, tick, page)
    fd = _read(folder, f"files-{page:016x}.json")
    try:
        before = os.fstat(fd)
        raw = os.pread(fd, max(16384, budget.chunk_bytes) + 1, 0)
        if (
            len(raw) > max(16384, budget.chunk_bytes)
            or hashlib.sha256(raw).digest() != expected
            or _identity(before) != _identity(os.fstat(fd))
        ):
            raise ValueError("The retained file index integrity changed")
    finally:
        os.close(fd)
    values = json.loads(raw)
    if not isinstance(values, list) or len(values) != count:
        raise ValueError("The file index record count changed")
    records = [GitDiffFileV1.model_validate(value) for value in values]
    if any(
        record.file_index != first + ordinal or record.offset + record.byte_count > manifest["byte_count"]
        for ordinal, record in enumerate(records)
    ):
        raise ValueError("The file index ordering changed")
    return records


def read_diff_index_page(artifactroot: Path, ref: GitDiffRefV1, page: int) -> GitDiffIndexPageV1:
    if type(page) is not int or not 0 <= page < ref.index_page_count:
        raise ValueError("The requested file index page is outside the artifact")
    folder, manifest, budget, tick = _open(artifactroot, ref)
    try:
        files = _index(folder, manifest, budget, tick, page)
        return GitDiffIndexPageV1(
            artifact_id=ref.artifact_id,
            manifest_sha256=ref.manifest_sha256,
            page=page,
            next_page=page + 1 if page + 1 < ref.index_page_count else None,
            files=files,
        )
    finally:
        os.close(folder)


def _page(
    folder: int,
    manifest: Data,
    budget: GitDiffBudget,
    tick: Callable[[], None],
    ref: GitDiffRefV1,
    offset: int,
    limit: int,
    file: GitDiffFileV1 | None = None,
) -> GitDiffPageV1:
    total = ref.byte_count if file is None else file.byte_count
    start = 0 if file is None else file.offset
    if type(offset) is not int or not 0 <= offset <= total or type(limit) is not int or not 1 <= limit <= 1048576:
        raise ValueError("The requested content window is outside its bounded extent")
    raw = _content(folder, manifest, budget, tick, start + offset, min(limit, total - offset))
    complete = True
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError as error:
        complete = False
        if file is not None:
            if offset and error.start == 0 and raw and 0x80 <= raw[0] <= 0xBF:
                raise ValueError("The requested UTF-8 offset is not aligned") from error
            if error.reason == "unexpected end of data" and error.end == len(raw):
                raw = raw[: error.start]
                if not raw and offset < total:
                    raise ValueError("The requested window cannot contain the next UTF-8 character") from error
                complete = True
    previous = _content(folder, manifest, budget, tick, start + offset - 1, 1) if offset else b"\n"
    next_offset = offset + len(raw)
    return GitDiffPageV1(
        artifact_id=ref.artifact_id,
        manifest_sha256=ref.manifest_sha256,
        file_index=None if file is None else file.file_index,
        offset=offset,
        byte_count=len(raw),
        total_bytes=total,
        next_offset=next_offset if next_offset < total else None,
        content_base64=base64.b64encode(raw).decode("ascii"),
        content_sha256=hashlib.sha256(raw).hexdigest(),
        starts_mid_line=bool(offset and previous != b"\n"),
        ends_mid_line=bool(next_offset < total and raw and not raw.endswith(b"\n")),
        utf8_complete=complete,
    )


def read_diff_page(artifactroot: Path, ref: GitDiffRefV1, offset: int, limit: int) -> GitDiffPageV1:
    folder, manifest, budget, tick = _open(artifactroot, ref)
    try:
        return _page(folder, manifest, budget, tick, ref, offset, limit)
    finally:
        os.close(folder)


def read_diff_file_page(
    artifactroot: Path, ref: GitDiffRefV1, file_index: int, offset: int, limit: int
) -> GitDiffPageV1:
    if type(file_index) is not int or not 0 <= file_index < ref.file_count:
        raise ValueError("The requested file is outside the artifact")
    folder, manifest, budget, tick = _open(artifactroot, ref)
    try:
        low, high = 0, ref.index_page_count
        while low < high:
            page = (low + high) // 2
            first, count, _ = _file_meta(folder, manifest, budget, tick, page)
            if file_index < first:
                high = page
            elif file_index >= first + count:
                low = page + 1
            else:
                file = _index(folder, manifest, budget, tick, page)[file_index - first]
                return _page(folder, manifest, budget, tick, ref, offset, limit, file)
        raise ValueError("The requested file descriptor is unavailable")
    finally:
        os.close(folder)
