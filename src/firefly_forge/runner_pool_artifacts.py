# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Owner-private, immutable invocation artifacts using hardened workspace transfer."""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import stat
import tarfile
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from pyfly.container import service

from firefly_forge.private_identity import private_identity
from firefly_forge.runner_pool_models import RunnerInvocation, RunnerPoolProfile, RunnerProjectInvocation, pool_digest
from firefly_forge.sandbox import _directory_fd, pack_workspace, unpack_workspace
from firefly_forge.secrets import contains_known_credential

if TYPE_CHECKING:
    from firefly_forge.runner_git_snapshot import RunnerGitSnapshot

ArtifactKind = Literal["source", "output", "result", "stdin"]


def validate_artifact_receipt(kind: str, receipt: Any, max_bytes: int) -> dict[str, Any]:
    if (
        kind not in {"output", "result"}
        or type(max_bytes) is not int
        or not 0 <= max_bytes <= 269_484_032
        or not isinstance(receipt, dict)
        or set(receipt) != {"kind", "bytes", "sha256"}
        or receipt["kind"] != kind
        or type(receipt["bytes"]) is not int
        or not 0 <= receipt["bytes"] <= max_bytes
        or not isinstance(receipt["sha256"], str)
        or re.fullmatch(r"[a-f0-9]{64}", receipt["sha256"]) is None
    ):
        raise ValueError("Worker artifact receipt differs from its frozen identity or byte budget")
    return dict(receipt)


def source_tree_digest(workspace: Path, limit: int, *, registry: dict[str, str] | None = None) -> str:
    # Transfer normalizes executable files/directories to 0755 and other files to
    # 0644. Identity binds those modes, paths and bytes, not producer uid/gid.
    archive, _ = pack_workspace(workspace, limit, registry=registry)
    return source_archive_digest(archive)


def source_archive_digest(archive: bytes) -> str:
    entries = []
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as stream:
        for item in stream:
            source = stream.extractfile(item) if item.isfile() else None
            entries.append(
                {
                    "path": item.name,
                    "mode": item.mode,
                    "directory": item.isdir(),
                    "bytes": item.size,
                    "sha256": hashlib.sha256(source.read()).hexdigest() if source else None,
                }
            )
    return pool_digest({"protocol": "forge-source-tree-v1", "entries": entries})


@service
class RunnerPoolArtifacts:
    def __init__(self, directory: Path) -> None:
        info = directory.stat(follow_symlinks=False)
        if (
            not directory.is_absolute()
            or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o700
        ):
            raise ValueError("Runner artifact root must be a private real owner-controlled directory")
        self.directory = directory
        self.credential_registry: Callable[[], dict[str, str]] = dict

    def _folder(self, invocation: RunnerInvocation) -> Path:
        root = _directory_fd(self.directory)
        child = None
        try:
            with suppress(FileExistsError):
                os.mkdir(invocation.execution_id, 0o700, dir_fd=root)
            child = _directory_fd(invocation.execution_id, parent=root)
            info = os.fstat(child)
            if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
                raise ValueError("Invocation artifact directory must remain private")
            return self.directory / invocation.execution_id
        finally:
            if child is not None:
                os.close(child)
            os.close(root)

    def retain(
        self, profile: RunnerPoolProfile, invocation: RunnerInvocation, kind: ArtifactKind, data: bytes
    ) -> dict[str, Any]:
        profile.validate_invocation(invocation)
        if kind not in {"source", "output", "result", "stdin"}:
            raise ValueError("Unsupported artifact kind")
        limit = self._limit(profile, invocation, kind)
        if kind == "stdin" and (
            len(data) != invocation.stdin_bytes
            or hashlib.sha256(data).hexdigest() != (invocation.stdin_digest or hashlib.sha256(b"").hexdigest())
        ):
            raise ValueError("Command stdin byte count or digest was changed")
        if len(data) > limit:
            raise ValueError("Runner artifact exceeds its byte limit")
        if contains_known_credential(data.decode("utf-8", errors="ignore"), self.credential_registry()):
            raise ValueError("Runner artifact cannot contain configured credentials")
        path = self._folder(invocation) / (kind + ".bin")
        try:
            previous = private_identity(path, lambda: data, size=len(data))
        except ValueError:
            raise RuntimeError("Retained runner artifact is immutable or requires reconciliation") from None
        if previous != data:
            raise RuntimeError("Retained runner artifact is immutable")
        return {"kind": kind, "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}

    def prepare(self, profile: RunnerPoolProfile, invocation: RunnerInvocation, archive: bytes) -> Path:
        profile.validate_invocation(invocation)
        if len(archive) != invocation.source_bytes or hashlib.sha256(archive).hexdigest() != invocation.archive_digest:
            raise ValueError("Source archive byte count or digest was changed")
        self.retain(profile, invocation, "source", archive)
        root = self._folder(invocation)
        if isinstance(invocation, RunnerProjectInvocation):
            from firefly_forge.runner_pool_bundle import unpack_project_bundle

            destination = root / "project-bundle"
            if not destination.exists():
                unpack_project_bundle(
                    archive,
                    destination,
                    profile.max_source_bytes,
                    invocation.bundle,
                    registry=self.credential_registry(),
                )
            source = destination / "source"
        else:
            source = root / "workspace"
            if not source.exists():
                unpack_workspace(archive, source, profile.max_source_bytes, registry=self.credential_registry())
        digest = source_tree_digest(source, profile.max_source_bytes, registry=self.credential_registry())
        if digest != invocation.source_digest:
            raise ValueError("Sanitized source digest differs from the immutable invocation")
        cwd = source.joinpath(invocation.cwd)
        if cwd.is_symlink() or not cwd.is_dir():
            raise ValueError("Invocation directory is not a confined source directory")
        return source

    async def minimum_git(
        self, profile: RunnerPoolProfile, invocation: RunnerInvocation, builder: RunnerGitSnapshot
    ) -> bytes:
        if not isinstance(invocation, RunnerProjectInvocation):
            raise ValueError("Minimum Git requires an explicit project bundle invocation")
        archive = self.read(
            profile,
            invocation,
            "source",
            {"kind": "source", "bytes": invocation.source_bytes, "sha256": invocation.archive_digest},
        )
        source = self.prepare(profile, invocation, archive)
        metadata, _ = pack_workspace(
            source.parent / "git", profile.max_source_bytes, metadata=True, registry=self.credential_registry()
        )
        if source_archive_digest(metadata) != invocation.bundle.git_digest:
            raise ValueError("Retained metadata differs from the frozen project bundle")
        raw, _ = pack_workspace(source, profile.max_source_bytes, registry=self.credential_registry())
        return await builder.validate(raw, metadata, self.directory, profile.max_source_bytes)

    def _limit(self, profile: RunnerPoolProfile, invocation: RunnerInvocation, kind: ArtifactKind) -> int:
        return (
            invocation.max_output_bytes
            if kind == "output"
            else 2_097_152
            if kind == "stdin"
            else profile.max_source_bytes + 1_048_576
        )

    def read(
        self, profile: RunnerPoolProfile, invocation: RunnerInvocation, kind: ArtifactKind, receipt: dict[str, Any]
    ) -> bytes:
        profile.validate_invocation(invocation)
        if kind not in {"source", "output", "result", "stdin"} or receipt.get("kind") != kind:
            raise ValueError("Invalid artifact receipt kind")
        size = receipt.get("bytes")
        if type(size) is not int or not 0 <= size <= self._limit(profile, invocation, kind):
            raise ValueError("Artifact receipt exceeds its byte limit")

        def absent() -> bytes:
            raise FileNotFoundError("Retained runner artifact is missing")

        value = private_identity(self._folder(invocation) / (kind + ".bin"), absent, size=size)
        if hashlib.sha256(value).hexdigest() != receipt.get("sha256"):
            raise ValueError("Retained artifact digest was changed")
        return value

    def input_archive(self, profile: RunnerPoolProfile, invocation: RunnerInvocation) -> bytes:
        source = self.read(
            profile,
            invocation,
            "source",
            {"kind": "source", "sha256": invocation.archive_digest, "bytes": invocation.source_bytes},
        )
        workspace = self.prepare(profile, invocation, source)
        archive, _ = pack_workspace(workspace, profile.max_source_bytes, registry=self.credential_registry())
        stdin = self.read(
            profile,
            invocation,
            "stdin",
            {
                "kind": "stdin",
                "bytes": invocation.stdin_bytes,
                "sha256": invocation.stdin_digest or hashlib.sha256(b"").hexdigest(),
            },
        )
        command = json.dumps(
            {
                "argv": list(invocation.argv),
                "cwd": invocation.cwd,
                "timeout": invocation.timeout,
                "execution_id": invocation.execution_id,
            }
        ).encode()
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode="w", format=tarfile.USTAR_FORMAT) as stream:
            with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as incoming:
                for item in incoming:
                    item.name = "source" if item.name == "." else "source/" + item.name
                    stream.addfile(item, incoming.extractfile(item) if item.isfile() else None)
            for name, data in (("stdin.bin", stdin), ("invocation.json", command)):
                item = tarfile.TarInfo(name)
                item.size, item.mode = len(data), 0o444
                stream.addfile(item, io.BytesIO(data))
        data = output.getvalue()
        if len(data) > profile.max_source_bytes + 5_242_880:
            raise ValueError("Runner carrier archive exceeds its byte limit")
        return data
