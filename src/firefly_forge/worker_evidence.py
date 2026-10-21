# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Private immutable raster previews bound to a native worker question and WAL receipt."""

from __future__ import annotations

import hashlib
import os
import re
import stat
from contextlib import suppress
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pyfly.container import service

from firefly_forge.attachment_decode import IMAGE_TYPES, MAX_BYTES, decode_attachment
from firefly_forge.journal import Journal, digest
from firefly_forge.private_identity import private_identity
from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied
from firefly_forge.run_evidence import RunEvidence
from firefly_forge.sandbox import _directory_fd, _parent_fd, _regular_data
from firefly_forge.secrets import contains_known_credential
from firefly_forge.store import Store, identifier

Data = dict[str, Any]


class WorkerImage(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    id: str = Field(pattern=r"^[a-f0-9]{64}$")
    filename: str = Field(min_length=1, max_length=180)
    mime_type: Literal["image/png", "image/jpeg", "image/webp"]
    bytes: int = Field(ge=1, le=MAX_BYTES)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class WorkerEvidenceManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    version: Literal[1]
    @field_validator("version", mode="before")
    @classmethod
    def exact_version(cls, value: Any) -> Any:
        if type(value) is not int or value != 1:
            raise ValueError("Worker evidence requires native version 1")
        return value

    run_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    feedback_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$")
    request_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$")
    images: list[WorkerImage] = Field(min_length=1, max_length=6)


@service
class WorkerEvidence:
    def __init__(self, store: Store, journal: Journal) -> None:
        self.store, self.journal = store, journal
        self.directory = store.path.parent / "worker-evidence"

    def _folder(self, run_id: str) -> Path:
        identifier(run_id)
        self.directory.mkdir(mode=0o700, exist_ok=True)
        root = _directory_fd(self.directory)
        child = None
        try:
            for descriptor in (root,):
                info = os.fstat(descriptor)
                if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
                    raise ValueError("Worker evidence storage must remain owner-private")
            with suppress(FileExistsError):
                os.mkdir(run_id, mode=0o700, dir_fd=root)
            child = _directory_fd(run_id, parent=root)
            info = os.fstat(child)
            if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
                raise ValueError("Worker evidence storage must remain owner-private")
            return self.directory / run_id
        finally:
            if child is not None:
                os.close(child)
            os.close(root)

    async def freeze(self, run: Data, feedback: Any, workspace: Path, component: Path) -> Data | None:
        if feedback.request is None:
            return None
        paths = [path for path in feedback.evidence_paths if Path(path).suffix.lower() in IMAGE_TYPES]
        if not paths:
            return None
        if len(paths) > 6 or len(set(paths)) != len(paths):
            raise ValueError("A worker question supports at most six distinct raster previews")
        images = []
        root = _directory_fd(workspace)
        try:
            for value in paths:
                path = Path(value)
                if path.is_absolute() or ".." in path.parts or not path.parts or len(path.name) > 180:
                    raise ValueError("Worker evidence must be a bounded public workspace file")
                if not (workspace / path).resolve().is_relative_to(component.resolve()):
                    raise ValueError("Worker evidence must remain inside the selected component")
                parent = _parent_fd(root, path.parts)
                try:
                    raw, _ = _regular_data(parent, path.name, MAX_BYTES)
                finally:
                    os.close(parent)
                self.store.reject_credentials([path.name, raw.decode("utf8", errors="ignore")])
                registry = self.store.credential_registry()
                if contains_known_credential(raw.decode("utf8", errors="ignore"), registry):
                    raise ValueError("Worker evidence cannot contain configured credentials")
                decoded = await decode_attachment(path.name, raw)
                content = decoded.get("image")
                if (
                    decoded.get("kind") != "image"
                    or not isinstance(content, bytes)
                    or not 0 < len(content) <= MAX_BYTES
                ):
                    raise ValueError("Worker preview requires a supported bounded raster image")
                sha = hashlib.sha256(content).hexdigest()
                image_id = digest({"run": run["id"], "feedback": feedback.id, "path": value, "sha256": sha})
                image = WorkerImage(
                    id=image_id, filename=path.name, mime_type=decoded["mime_type"], bytes=len(content), sha256=sha
                )
                folder = self._folder(run["id"])
                if len(list(folder.iterdir())) >= 48 and not (folder / (image_id + ".bin")).exists():
                    raise ValueError("Retained worker evidence reached its per-run byte and count budget")
                retained = private_identity(
                    folder / (image_id + ".bin"), lambda content=content: content, size=len(content)
                )
                if retained != content:
                    raise ValueError("Worker evidence is immutable")
                self.store.reject_credentials(image.model_dump())
                images.append(image)
        finally:
            os.close(root)
        return WorkerEvidenceManifest(
            version=1, run_id=run["id"], feedback_id=feedback.id, request_id=feedback.request.id, images=images
        ).model_dump()

    def read_blob(self, run_id: str, image: Data) -> bytes:
        record = WorkerImage.model_validate(image)
        root = _directory_fd(self.directory)
        child = None
        try:
            child = _directory_fd(identifier(run_id).hex, parent=root)
            for descriptor in (root, child):
                info = os.fstat(descriptor)
                if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
                    raise ValueError("Worker evidence storage must remain owner-private")
            raw, info = _regular_data(child, record.id + ".bin", MAX_BYTES)
            if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
                raise ValueError("Worker evidence bytes are not owner-private")
            if len(raw) != record.bytes or hashlib.sha256(raw).hexdigest() != record.sha256:
                raise ValueError("Worker evidence byte count or hash changed")
            return raw
        finally:
            if child is not None:
                os.close(child)
            os.close(root)

    async def _snapshot(self, run_id: str) -> tuple[Data, Any, list[Data]]:
        reader = CURRENT_PRINCIPAL.get()
        if reader is None:
            raise AccessDenied("Worker previews require a current authenticated project reader")
        run = await self.store.get_run(run_id)
        await RunEvidence(self.store)._authorize(run, frozenset({run["repository_id"]}), reader)
        entries = await self.journal.entries(run_id)
        if len(entries) > 2048 or not Journal.verify_chain(entries):
            raise ValueError("Worker evidence requires an intact bounded native journal")
        manifests = []
        for entry in entries:
            if entry["kind"] != "worker_evidence":
                continue
            manifest = WorkerEvidenceManifest.model_validate(entry["payload"])
            if manifest.run_id != run_id or len({image.id for image in manifest.images}) != len(manifest.images):
                raise ValueError("Worker evidence manifest does not match the native run")
            feedback = next(
                (
                    record
                    for record in entries
                    if record["kind"] == "worker_feedback"
                    and record["sequence"] < entry["sequence"]
                    and record["payload"].get("id") == manifest.feedback_id
                ),
                None,
            )
            if not feedback or (feedback["payload"].get("request") or {}).get("id") != manifest.request_id:
                raise ValueError("Worker evidence does not belong to the recorded question")
            manifests.append(manifest.model_dump())
        self.store.reject_credentials(manifests)
        return run, reader, manifests

    async def list(self, run_id: str, request_id: str) -> list[Data]:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", request_id):
            raise ValueError("Invalid worker request identity")
        run, reader, manifests = await self._snapshot(run_id)
        result = [
            image for manifest in manifests if manifest["request_id"] == request_id for image in manifest["images"]
        ]
        for image in result:
            self.read_blob(run_id, image)
        await RunEvidence(self.store)._authorize(run, frozenset({run["repository_id"]}), reader)
        return result

    async def image(self, run_id: str, image_id: str) -> tuple[Data, bytes]:
        if not re.fullmatch(r"[a-f0-9]{64}", image_id):
            raise ValueError("Invalid worker evidence identity")
        run, reader, manifests = await self._snapshot(run_id)
        images = [image for manifest in manifests for image in manifest["images"] if image["id"] == image_id]
        if len(images) != 1:
            raise KeyError("Worker evidence not found in this run")
        raw = self.read_blob(run_id, images[0])
        self.store.reject_credentials(raw.decode("utf8", errors="ignore"))
        await RunEvidence(self.store)._authorize(run, frozenset({run["repository_id"]}), reader)
        return images[0], raw
