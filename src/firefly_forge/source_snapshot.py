# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Explicit diff evidence and streamed identities for factory-owned Git indexes."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import math
import os
import re
import signal
import stat
import time
import uuid
import zipfile
from collections.abc import Awaitable, Callable
from contextlib import suppress
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from firefly_forge.git_diff_models import Digest, GitDiffBudget, GitDiffRefV1
from firefly_forge.git_workspace import git_environment, trusted_git
from firefly_forge.source_diff_admission import SourceDiffAdmission


class GitIndexIdentityV1(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    version: Literal[1] = 1
    device: int
    inode: int
    uid: int
    mode: int
    byte_count: int
    mtime_ns: int
    ctime_ns: int
    sha256: Digest


class SourceDiffSnapshotV1(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    version: Literal[1] = 1
    artifact: GitDiffRefV1
    preview: str = Field(max_length=1048576)
    preview_bytes: int = Field(ge=0)
    preview_truncated: bool

    @model_validator(mode="after")
    def preview_scope(self) -> SourceDiffSnapshotV1:
        if self.preview_bytes > self.artifact.byte_count or self.preview_truncated is not (
            self.preview_bytes < self.artifact.byte_count
        ):
            raise ValueError("Source diff preview differs from the retained artifact scope")
        return self


def intersect_diff_budgets(project: GitDiffBudget, runner: GitDiffBudget | None = None) -> GitDiffBudget:
    if not isinstance(project, GitDiffBudget) or (runner is not None and not isinstance(runner, GitDiffBudget)):
        raise ValueError("Diff storage requires explicit typed project and runner budgets")
    if runner is None:
        return project
    limits = [v for v in (project.max_bytes, runner.max_bytes) if v is not None]
    return GitDiffBudget(
        max_bytes=min(limits) if limits else None,
        min_free_bytes=max(project.min_free_bytes, runner.min_free_bytes),
        timeout_seconds=min(project.timeout_seconds, runner.timeout_seconds),
        chunk_bytes=min(project.chunk_bytes, runner.chunk_bytes),
        index_page_files=min(project.index_page_files, runner.index_page_files),
        preview_bytes=min(project.preview_bytes, runner.preview_bytes),
    )


def diff_budget_for_run(run: dict[str, Any], runner: GitDiffBudget | None = None) -> GitDiffBudget:
    policy = run.get("git_delivery_policy") or {}
    raw = policy.get("diff_budget")
    return intersect_diff_budgets(GitDiffBudget.model_validate(raw) if raw is not None else GitDiffBudget(), runner)


def snapshot_run_fields(files: list[str], source: SourceDiffSnapshotV1) -> dict[str, Any]:
    return {
        "changed_files": files,
        "changed_file_count": source.artifact.file_count,
        "changed_files_preview_truncated": len(files) < source.artifact.file_count,
        "diff": "[Bounded source diff preview; complete evidence is available through the diff artifact]\n"
        + source.preview,
        "diff_artifact": source.artifact.model_dump(mode="json"),
        "diff_preview_bytes": source.preview_bytes,
        "diff_preview_truncated": source.preview_truncated,
    }


async def stream_git_bytes(
    workspace: Path, argv: list[str], consume: Callable[[bytes], Awaitable[None]], *, timeout_seconds: int
) -> None:
    """Keep source blobs outside subprocess capture and drain only this owned child."""
    child = None
    diagnostics = None

    async def drain(stream: asyncio.StreamReader) -> None:
        total = 0
        while raw := await stream.read(4096):
            total += len(raw)
            if total > 65536:
                raise ValueError("Git source diagnostics exceeded the bounded window")

    try:
        async with asyncio.timeout(timeout_seconds):
            child = await asyncio.create_subprocess_exec(
                *trusted_git(argv),
                cwd=workspace,
                env={"PATH": os.defpath, "HOME": os.devnull, **git_environment()},
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
                limit=65536,
            )
            assert child.stdout is not None and child.stderr is not None
            diagnostics = asyncio.create_task(drain(child.stderr))
            while raw := await child.stdout.read(65536):
                if diagnostics.done():
                    diagnostics.result()
                await consume(raw)
            status = await child.wait()
            await diagnostics
            if status:
                raise ValueError("Git could not stream the exact source evidence")
    finally:
        if child is not None:
            with suppress(ProcessLookupError, PermissionError):
                os.killpg(child.pid, signal.SIGKILL)
            if child.returncode is None:
                with suppress(ProcessLookupError, PermissionError):
                    child.kill()
            await asyncio.wait_for(child.wait(), 2)
        if diagnostics is not None and not diagnostics.done():
            diagnostics.cancel()
            with suppress(asyncio.CancelledError):
                await diagnostics


async def stream_changed_paths(
    workspace: Path, base: str, consume: Callable[[str], Awaitable[None]], *, timeout_seconds: int
) -> None:
    tail = b""

    async def receive(raw: bytes) -> None:
        nonlocal tail
        values = (tail + raw).split(b"\0")
        tail = values.pop()
        if len(tail) > 65536:
            raise ValueError("Source path exceeds the bounded Git name window")
        for value in values:
            try:
                path = value.decode("utf-8", errors="strict")
            except UnicodeDecodeError:
                raise ValueError("Source paths require exact UTF-8 text") from None
            if not path or path.startswith("/") or any(p in {"", ".", ".."} for p in path.split("/")):
                raise ValueError("Source paths require exact relative Git names")
            await consume(path)

    await stream_git_bytes(
        workspace,
        ["git", "diff", "--cached", "--name-only", "--no-renames", "--no-ext-diff", "--no-textconv", "-z", base, "--"],
        receive,
        timeout_seconds=timeout_seconds,
    )
    if tail:
        raise ValueError("Git source inventory ended without an exact name boundary")


async def source_blob(workspace: Path, tree: str | None, path: str, *, timeout_seconds: int) -> tuple[str, str] | None:
    raw = bytearray()

    async def collect(chunk: bytes) -> None:
        if len(raw) + len(chunk) > 65536:
            raise ValueError("Git source entry exceeds the bounded metadata window")
        raw.extend(chunk)

    argv = (
        ["git", "--literal-pathspecs", "ls-tree", "-z", tree, "--", path]
        if tree is not None
        else ["git", "--literal-pathspecs", "ls-files", "--stage", "-z", "--", path]
    )
    await stream_git_bytes(
        workspace,
        argv,
        collect,
        timeout_seconds=timeout_seconds,
    )
    if not raw:
        return None
    header, separator, name = bytes(raw).partition(b"\t")
    if not separator or name != path.encode() + b"\0":
        raise ValueError("Git source entry differs from its exact requested path")
    values = header.decode("ascii").split(" ")
    blob = values[2] if tree is not None and len(values) == 3 else values[1] if len(values) == 3 else ""
    kind = values[1] if tree is not None and len(values) == 3 else values[2] if len(values) == 3 else ""
    if (
        len(values) != 3
        or values[0] not in {"100644", "100755"}
        or kind != ("blob" if tree is not None else "0")
        or not re.fullmatch(r"[a-f0-9]{40}|[a-f0-9]{64}", blob)
    ):
        raise ValueError("Changed source must contain regular Git blobs without symbolic links or submodules")
    return values[0], blob


async def scan_changed_source(
    workspace: Path,
    base: str,
    tree: str | None,
    scanner: SourceDiffAdmission,
    *,
    budget: GitDiffBudget,
    validate_path: Callable[[str], None],
) -> tuple[list[str], int]:
    preview: list[str] = []
    count = 0

    async def path_document(path: str) -> None:
        nonlocal count
        validate_path(path)
        await scanner(path.encode(), False)
        scanner.finish_document()
        # Raw blobs cover binary patches and quoted names that serialized Git
        # output cannot expose to a plaintext credential matcher.
        for revision in (base, tree):
            entry = await source_blob(workspace, revision, path, timeout_seconds=budget.timeout_seconds)
            if entry is not None:

                async def receive(chunk: bytes) -> None:
                    await scanner(chunk, False)

                await stream_git_bytes(
                    workspace,
                    ["git", "cat-file", "blob", entry[1]],
                    receive,
                    timeout_seconds=budget.timeout_seconds,
                )
                scanner.finish_document()
        count += 1
        if len(preview) < 100:
            preview.append(path)

    async with asyncio.timeout(budget.timeout_seconds):
        await stream_changed_paths(workspace, base, path_document, timeout_seconds=budget.timeout_seconds)
    return preview, count


async def archive_source_diff(
    workspace: Path,
    tree: str,
    artifactroot: Path,
    source: SourceDiffSnapshotV1,
    target: Path,
    *,
    budget: GitDiffBudget,
    validate_path: Callable[[str], None],
    assert_current: Callable[[], Awaitable[None]],
) -> None:
    from firefly_forge.git_diff_artifacts import read_diff_index_page, read_diff_page

    parent = _owned_directory(target.parent)
    temporary = "." + target.name + "." + uuid.uuid4().hex + ".partial"
    total = 0
    deadline = time.monotonic() + budget.timeout_seconds

    def tick(size: int = 0) -> None:
        nonlocal total
        total += size
        if budget.max_bytes is not None and total > budget.max_bytes:
            raise ValueError("The configured changed-source archive budget was exceeded")
        space = os.fstatvfs(parent)
        if space.f_bavail * space.f_frsize < budget.min_free_bytes:
            raise ValueError("The private archive storage free-space floor was reached")
        if time.monotonic() >= deadline:
            raise TimeoutError("The changed-source archive deadline expired")

    fd = None
    try:
        tick()
        fd = os.open(temporary, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
        async with asyncio.timeout(budget.timeout_seconds):
            await assert_current()
            with os.fdopen(fd, "w+b") as output:
                fd = None
                with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
                    with archive.open("forge-changes.patch", "w", force_zip64=True) as patch:
                        offset = 0
                        while offset < source.artifact.byte_count:
                            page = read_diff_page(artifactroot, source.artifact, offset, 65536)
                            raw = base64.b64decode(page.content_base64, validate=True)
                            tick(len(raw))
                            patch.write(raw)
                            offset += len(raw)
                    seen = 0
                    for ordinal in range(source.artifact.index_page_count):
                        index = read_diff_index_page(artifactroot, source.artifact, ordinal)
                        await assert_current()
                        for entry in index.files:
                            tick()
                            validate_path(entry.path)
                            if entry.path == "forge-changes.patch":
                                raise ValueError("Changed source collides with the reserved archive patch name")
                            seen += 1
                            blob = await source_blob(
                                workspace, tree, entry.path, timeout_seconds=budget.timeout_seconds
                            )
                            if blob is None:
                                continue
                            info = zipfile.ZipInfo(entry.path)
                            info.compress_type = zipfile.ZIP_DEFLATED
                            info.external_attr = (stat.S_IFREG | (0o755 if blob[0] == "100755" else 0o644)) << 16
                            with archive.open(info, "w", force_zip64=True) as destination:

                                async def receive(raw: bytes) -> None:
                                    tick(len(raw))
                                    destination.write(raw)

                                await stream_git_bytes(
                                    workspace,
                                    ["git", "cat-file", "blob", blob[1]],
                                    receive,
                                    timeout_seconds=budget.timeout_seconds,
                                )
                    if seen != source.artifact.file_count:
                        raise ValueError("The archive inventory differs from the complete diff evidence")
                output.flush()
                os.fsync(output.fileno())
            await assert_current()
            tick()
            # Never replace an existing or foreign artifact during a retry.
            os.link(temporary, target.name, src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)
            os.fsync(parent)
    finally:
        if fd is not None:
            os.close(fd)
        with suppress(FileNotFoundError):
            os.unlink(temporary, dir_fd=parent)
        os.close(parent)


def _stamp(value: os.stat_result) -> tuple[int, ...]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_uid,
        value.st_mode,
        value.st_nlink,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _owned_directory(path: Path) -> int:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    value = os.fstat(fd)
    if value.st_uid != os.geteuid() or value.st_mode & 0o022:
        os.close(fd)
        raise ValueError("Source index requires an owned Git directory")
    return fd


def git_index_identity(workspace: Path, *, timeout_seconds: float = 120) -> GitIndexIdentityV1:
    if type(timeout_seconds) not in {int, float} or not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError("Source index hashing requires a finite positive time budget")
    deadline = time.monotonic() + timeout_seconds
    root = git = index = None
    try:
        root = _owned_directory(workspace)
        git = os.open(".git", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root)
        directory = os.fstat(git)
        if directory.st_uid != os.geteuid() or directory.st_mode & 0o022:
            raise ValueError("Source index requires an owned Git directory")
        index = os.open("index", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=git)
        before = os.fstat(index)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_uid != os.geteuid()
            or before.st_mode & 0o022
        ):
            raise ValueError("Source index requires an owned unshared regular file")
        digest = hashlib.sha256()
        remaining = before.st_size
        while remaining:
            if time.monotonic() >= deadline:
                raise ValueError("Source index hashing exceeded its finite time budget")
            chunk = os.read(index, min(65536, remaining))
            if not chunk:
                raise ValueError("Source index changed during its streamed read")
            digest.update(chunk)
            remaining -= len(chunk)
        after = os.fstat(index)
        current = os.stat("index", dir_fd=git, follow_symlinks=False)
        if os.read(index, 1) or _stamp(before) != _stamp(after) or _stamp(after) != _stamp(current):
            raise ValueError("Source index changed during its streamed read")
        if time.monotonic() >= deadline:
            raise ValueError("Source index hashing exceeded its finite time budget")
        return GitIndexIdentityV1(
            device=before.st_dev,
            inode=before.st_ino,
            uid=before.st_uid,
            mode=before.st_mode,
            byte_count=before.st_size,
            mtime_ns=before.st_mtime_ns,
            ctime_ns=before.st_ctime_ns,
            sha256=digest.hexdigest(),
        )
    except OSError:
        raise ValueError("Source index is unavailable or is not owned regular Git metadata") from None
    finally:
        for fd in (index, git, root):
            if fd is not None:
                os.close(fd)


def diff_artifact_root(workspaces: Path, run_id: str) -> Path:
    if not isinstance(run_id, str) or not re.fullmatch(r"[a-f0-9]{32}", run_id):
        raise ValueError("Source artifact namespace requires an exact native run identity")
    root = child = None
    try:
        root = _owned_directory(workspaces)
        for name in (".diff-artifacts", run_id):
            with suppress(FileExistsError):
                os.mkdir(name, mode=0o700, dir_fd=root)
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root)
            value = os.fstat(child)
            if value.st_uid != os.geteuid() or value.st_mode & 0o777 != 0o700:
                raise ValueError("Source artifact namespace must be private and owned")
            os.close(root)
            root, child = child, None
        return workspaces / ".diff-artifacts" / run_id
    except (OSError, ValueError):
        raise ValueError("Source artifact namespace must be a private owned directory") from None
    finally:
        for fd in (child, root):
            if fd is not None:
                os.close(fd)
