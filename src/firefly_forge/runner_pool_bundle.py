# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Explicit source/Git archive framing; metadata is retained separately and never imported."""

from __future__ import annotations

import hashlib
import io
import os
import shutil
import tarfile
import tempfile
from pathlib import Path

from firefly_forge.runner_bundle_models import ProjectBundleIdentity
from firefly_forge.runner_pool_artifacts import source_archive_digest, source_tree_digest
from firefly_forge.sandbox import _directory_fd, _regular_data, pack_workspace, unpack_workspace


def pack_project_bundle(
    source: Path, metadata: Path | None, limit: int, *, registry: dict[str, str] | None = None
) -> tuple[bytes, ProjectBundleIdentity]:
    workspace, _ = pack_workspace(source, limit, registry=registry)
    git = pack_workspace(metadata, limit, metadata=True, registry=registry)[0] if metadata else b""
    return frame_project_archives(workspace, git, limit)


def frame_project_archives(workspace: bytes, git: bytes, limit: int) -> tuple[bytes, ProjectBundleIdentity]:
    source_sha = hashlib.sha256(workspace).hexdigest()
    git_sha = hashlib.sha256(git).hexdigest() if git else None
    if len(workspace) + len(git) > limit:
        raise ValueError("Combined source and Git metadata framing exceeds its byte limit")
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w", format=tarfile.USTAR_FORMAT) as stream:
        for name, data in (("source.tar", workspace), ("git.tar", git)):
            if not data:
                continue
            item = tarfile.TarInfo(name)
            item.mode, item.size = 0o600, len(data)
            stream.addfile(item, io.BytesIO(data))
    data = output.getvalue()
    if len(data) > limit:
        raise ValueError("Project bundle framing exceeds its total byte limit")
    identity = ProjectBundleIdentity(
        archive_digest=hashlib.sha256(data).hexdigest(),
        archive_bytes=len(data),
        source_digest=source_archive_digest(workspace),
        source_archive_digest=source_sha,
        source_archive_bytes=len(workspace),
        git_digest=source_archive_digest(git) if git else None,
        git_archive_digest=git_sha,
        git_archive_bytes=len(git),
    )
    return data, identity


def unpack_project_bundle(
    data: bytes,
    destination: Path,
    limit: int,
    identity: ProjectBundleIdentity,
    *,
    registry: dict[str, str] | None = None,
) -> tuple[Path, Path | None]:
    if len(data) != identity.archive_bytes or hashlib.sha256(data).hexdigest() != identity.archive_digest:
        raise ValueError("Project bundle transport byte count or digest changed")
    if (
        len(data) > limit
        or identity.source_archive_bytes + identity.git_archive_bytes > limit
        or bool(identity.git_archive_bytes) != bool(identity.git_archive_digest)
        or bool(identity.git_archive_bytes) != bool(identity.git_digest)
    ):
        raise ValueError("Project bundle exceeds its byte limit or metadata identity")
    expected = {"source.tar", "git.tar"} if identity.git_archive_bytes else {"source.tar"}
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as stream:
        seen = set()
        for member in stream:
            if (
                member.name not in expected
                or member.name in seen
                or not member.isfile()
                or getattr(member, "sparse", None)
                or member.pax_headers
            ):
                raise ValueError("Project bundle contains unsupported or duplicate framing entries")
            seen.add(member.name)
        if seen != expected:
            raise ValueError("Project bundle framing entries are missing")
    if destination.exists() or destination.is_symlink():
        raise ValueError("Project bundle requires a fresh private destination")
    temporary = Path(tempfile.mkdtemp(prefix=".forge-bundle-", dir=destination.parent))
    try:
        framing = temporary / "framing"
        unpack_workspace(data, framing, limit, registry=registry)
        if {path.name for path in framing.iterdir()} != expected:
            raise ValueError("Project bundle contains unsupported framing entries")
        root = _directory_fd(framing)
        try:
            source, _ = _regular_data(root, "source.tar", identity.source_archive_bytes)
            git = _regular_data(root, "git.tar", identity.git_archive_bytes)[0] if identity.git_archive_bytes else b""
        finally:
            os.close(root)
        if (
            len(source) != identity.source_archive_bytes
            or hashlib.sha256(source).hexdigest() != identity.source_archive_digest
            or (
                git
                and (
                    len(git) != identity.git_archive_bytes
                    or hashlib.sha256(git).hexdigest() != identity.git_archive_digest
                )
            )
        ):
            raise ValueError("Project bundle component digest changed")
        if source_archive_digest(source) != identity.source_digest or (
            git and source_archive_digest(git) != identity.git_digest
        ):
            raise ValueError("Project bundle canonical source or metadata digest changed")
        unpack_workspace(source, temporary / "source", limit, registry=registry)
        if git:
            unpack_workspace(git, temporary / "git", limit, registry=registry)
            actual, _ = pack_workspace(temporary / "git", limit, metadata=True, registry=registry)
            if source_archive_digest(actual) != identity.git_digest:
                raise ValueError("Git metadata contains unsupported private paths")
        if source_tree_digest(temporary / "source", limit, registry=registry) != identity.source_digest:
            raise ValueError("Source bundle contains unsupported private paths")
        shutil.rmtree(framing)
        temporary.rename(destination)
        return destination / "source", destination / "git" if git else None
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
