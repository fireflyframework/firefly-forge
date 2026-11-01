# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Runner artifact publication: native ECM bytes, PostgreSQL CAS and private scratch."""

from __future__ import annotations

import codecs
import hashlib
import os
import stat
from collections.abc import AsyncIterable, AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Literal, Protocol
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from pydantic import BaseModel, ConfigDict, Field

from firefly_forge.object_storage import ArtifactObjectStorage, Authorize, ObjectChunk, ObjectScope
from firefly_forge.runner_pool_models import FactoryFence, RunnerInvocation, RunnerPoolProfile, pool_digest
from firefly_forge.secrets import KnownCredentialScan, credential_values
from firefly_forge.source_catalog import _directory


class ArtifactLedger(Protocol):
    async def begin(self, identity: dict[str, Any], fence: FactoryFence) -> tuple[str, dict[str, Any] | None]: ...
    async def progress(
        self, identity: dict[str, Any], owner: str, chunk: ObjectChunk, confirmed: bool, fence: FactoryFence
    ) -> None: ...
    async def publish(
        self, identity: dict[str, Any], owner: str, manifest: RunnerObjectManifest, fence: FactoryFence
    ) -> None: ...
    async def load(self, identity: dict[str, Any]) -> dict[str, Any]: ...
    async def pending(self, identity: dict[str, Any], fence: FactoryFence) -> dict[str, Any]: ...
    async def publish_readback(
        self, identity: dict[str, Any], observation: dict[str, Any], manifest: RunnerObjectManifest, fence: FactoryFence
    ) -> None: ...
    def chunks(
        self, identity: dict[str, Any], observation: dict[str, Any] | None = None
    ) -> AsyncIterator[RunnerObjectChunk]: ...


class RunnerObjectChunk(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    protocol: Literal["forge-runner-object-chunk-v1"] = "forge-runner-object-chunk-v1"
    sequence: int = Field(gt=0, strict=True)
    previous_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    chunk: ObjectChunk
    hash: str = Field(pattern=r"^[a-f0-9]{64}$")

    @classmethod
    def capture(cls, sequence: int, previous_hash: str, chunk: ObjectChunk) -> RunnerObjectChunk:
        value = {
            "protocol": "forge-runner-object-chunk-v1",
            "sequence": sequence,
            "previous_hash": previous_hash,
            "chunk": chunk.model_dump(mode="json"),
        }
        return cls.model_validate({**value, "hash": pool_digest(value)})

    def validate_chain(self, sequence: int, previous_hash: str) -> None:
        if (
            self.sequence != sequence
            or self.previous_hash != previous_hash
            or self.hash != pool_digest(self.model_dump(mode="json", exclude={"hash"}))
        ):
            raise ValueError("Artifact chunk sequence or digest chain changed")


class RunnerObjectManifest(BaseModel):
    """Constant-size root; chunk references are independently persisted rows."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    protocol: Literal["forge-runner-object-ledger-v1"] = "forge-runner-object-ledger-v1"
    scope: ObjectScope
    binding: str = Field(pattern=r"^[a-f0-9]{64}$")
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    bytes: int = Field(ge=0, strict=True)
    chunk_count: int = Field(ge=0, strict=True)
    chain_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class RunnerObjectReference(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    protocol: Literal["forge-runner-object-reference-v1"] = "forge-runner-object-reference-v1"
    execution_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    invocation_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    kind: Literal["source", "stdin", "output", "result"]
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    bytes: int = Field(ge=0, strict=True)
    receipt_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")

    def receipt(self) -> dict[str, Any]:
        value = {"kind": self.kind, "bytes": self.bytes, "sha256": self.sha256}
        if pool_digest(value) != self.receipt_sha256:
            raise ValueError("Object reference receipt digest changed")
        return value


class RunnerArtifactReadLease(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    owner_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    epoch: int = Field(gt=0, strict=True)
    fence: FactoryFence


@dataclass
class _Spool:
    parent: int
    name: str
    file: BinaryIO


class RunnerObjectArtifacts:
    def __init__(self, tenant_id: UUID, objects: ArtifactObjectStorage, ledger: ArtifactLedger, scratch: Path) -> None:
        self.tenant_id, self.objects, self.ledger, self.scratch = tenant_id, objects, ledger, scratch
        self.credential_registry: Callable[[], dict[str, str]] = dict

    def _current(self, authorize: Authorize) -> Authorize:
        credentials = credential_values(self.credential_registry())

        async def current() -> None:
            await authorize()
            if credential_values(self.credential_registry()) != credentials:
                raise ValueError("Artifact credential registry changed; explicit reconciliation is required")

        return current

    @staticmethod
    def reference(invocation: RunnerInvocation, kind: str, receipt: dict[str, Any]) -> RunnerObjectReference:
        return RunnerObjectReference.model_validate(
            {
                "execution_id": invocation.execution_id,
                "invocation_digest": pool_digest(invocation.model_dump(mode="json")),
                "kind": kind,
                "bytes": receipt["bytes"],
                "sha256": receipt["sha256"],
                "receipt_sha256": pool_digest(receipt),
            }
        )

    async def read_reference(
        self,
        profile: RunnerPoolProfile,
        invocation: RunnerInvocation,
        reference: RunnerObjectReference,
        *,
        authorize: Authorize,
    ) -> AsyncIterator[bytes]:
        self.reference_identity(profile, invocation, reference)
        async for part in self.read(profile, invocation, reference.kind, reference.receipt(), authorize=authorize):
            yield part

    def reference_identity(
        self, profile: RunnerPoolProfile, invocation: RunnerInvocation, reference: RunnerObjectReference
    ) -> dict[str, Any]:
        if reference.execution_id != invocation.execution_id or reference.invocation_digest != pool_digest(
            invocation.model_dump(mode="json")
        ):
            raise ValueError("Object reference belongs to another immutable invocation")
        return self.identity(profile, invocation, reference.kind, reference.receipt())

    def identity(
        self, profile: RunnerPoolProfile, invocation: RunnerInvocation, kind: str, receipt: dict[str, Any]
    ) -> dict[str, Any]:
        profile.validate_invocation(invocation)
        if invocation.project_id is None:
            raise ValueError("Object artifacts require an explicit project; retained unscoped invocations stay local")
        limit = (
            invocation.max_output_bytes
            if kind == "output"
            else 2_097_152
            if kind == "stdin"
            else profile.max_source_bytes + 1_048_576
        )
        if (
            kind not in {"source", "stdin", "output", "result"}
            or set(receipt) != {"kind", "bytes", "sha256"}
            or receipt.get("kind") != kind
            or type(receipt.get("bytes")) is not int
            or not 0 <= receipt["bytes"] <= limit
            or not isinstance(receipt.get("sha256"), str)
        ):
            raise ValueError("Artifact receipt differs from its frozen invocation budget")
        scope = ObjectScope(tenant_id=self.tenant_id, project_id=invocation.project_id)
        # Reuse the strict native descriptor validation rather than accepting an
        # arbitrary digest that would later become an object key.
        self.reference(invocation, kind, receipt)
        expected = (
            (invocation.source_bytes, invocation.archive_digest)
            if kind == "source"
            else (invocation.stdin_bytes, invocation.stdin_digest or hashlib.sha256(b"").hexdigest())
            if kind == "stdin"
            else None
        )
        if expected is not None and (receipt["bytes"], receipt["sha256"]) != expected:
            raise ValueError("Artifact source/stdin differs from the frozen invocation")
        key = f"{scope.tenant_id.hex}:{scope.project_id.hex}:{invocation.execution_id}:{kind}"
        return {
            "key": uuid5(NAMESPACE_URL, "forge-object-artifact-v1:" + key).hex,
            "protocol": "forge-object-artifact-v1",
            "scope": scope.model_dump(mode="json"),
            "execution_id": invocation.execution_id,
            "invocation_digest": pool_digest(invocation.model_dump(mode="json")),
            "profile_revision": profile.revision,
            "storage_binding": self.objects.settings.binding,
            "chunk_bytes": self.objects.settings.chunk_bytes,
            "expected_chunks": (receipt["bytes"] + self.objects.settings.chunk_bytes - 1)
            // self.objects.settings.chunk_bytes,
            "receipt": dict(receipt),
        }

    @asynccontextmanager
    async def _spooled(
        self, source: AsyncIterable[bytes], expected: dict[str, Any], authorize: Authorize
    ) -> AsyncIterator[_Spool]:
        parent = _directory(self.scratch, private=True)
        name = ".forge-object-stage-" + uuid4().hex
        descriptor = None
        output = None
        try:
            root = os.fstat(parent)
            if root.st_uid != os.getuid() or stat.S_IMODE(root.st_mode) != 0o700:
                raise ValueError("Artifact staging requires an infrastructure-owned private volume")
            descriptor = os.open(name, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
            output = os.fdopen(descriptor, "w+b")
            descriptor = None
            digest, count = hashlib.sha256(), 0
            scanner = KnownCredentialScan(credential_values(self.credential_registry()))
            decoder = codecs.getincrementaldecoder("utf-8")(errors="ignore")
            await authorize()
            async for data in source:
                if not isinstance(data, bytes):
                    raise TypeError("Artifact producer must stream bytes")
                count += len(data)
                if count > expected["bytes"]:
                    raise ValueError("Artifact stream exceeds its frozen identity")
                if scanner.feed(decoder.decode(data)):
                    raise ValueError("Artifact stream cannot contain configured credentials")
                digest.update(data)
                output.write(data)
            if scanner.feed(decoder.decode(b"", final=True)) or scanner.feed(None):
                raise ValueError("Artifact stream cannot contain configured credentials")
            if count != expected["bytes"] or digest.hexdigest() != expected["sha256"]:
                raise ValueError("Artifact stream differs from its frozen identity")
            output.flush()
            os.fsync(output.fileno())
            output.seek(0)
            await authorize()
            yield _Spool(parent, name, output)
        finally:
            if output is not None:
                identity = os.fstat(output.fileno())
                try:
                    current = os.stat(name, dir_fd=parent, follow_symlinks=False)
                    if (current.st_dev, current.st_ino) == (identity.st_dev, identity.st_ino):
                        os.unlink(name, dir_fd=parent)
                except FileNotFoundError:
                    pass
                output.close()
            if descriptor is not None:
                os.close(descriptor)
            os.close(parent)

    async def _parts(self, file: BinaryIO, authorize: Authorize) -> AsyncIterator[bytes]:
        while data := file.read(self.objects.settings.chunk_bytes):
            await authorize()
            yield data

    async def upload(
        self,
        profile: RunnerPoolProfile,
        invocation: RunnerInvocation,
        kind: str,
        receipt: dict[str, Any],
        source: AsyncIterable[bytes],
        fence: FactoryFence,
        *,
        authorize: Authorize,
    ) -> dict[str, Any]:
        identity = self.identity(profile, invocation, kind, receipt)
        authorize = self._current(authorize)
        # Credential and aggregate validation finish before any object upload.
        # The spool belongs to an ephemeral controller/runner volume, not to the
        # durable store or an API host's shared project checkout.
        async with self._spooled(source, receipt, authorize) as spool:
            owner, previous = await self.ledger.begin(identity, fence)
            if previous is not None:
                await self._manifest(identity, previous)
                await authorize()
                return dict(receipt)

            async def progress(chunk: ObjectChunk, confirmed: bool) -> None:
                await authorize()
                await self.ledger.progress(identity, owner, chunk, confirmed, fence)

            count, head = 0, "0" * 64
            async for chunk in self.objects.write_chunks(
                ObjectScope.model_validate(identity["scope"]),
                self._parts(spool.file, authorize),
                authorize=authorize,
                progress=progress,
            ):
                count += 1
                head = RunnerObjectChunk.capture(count, head, chunk).hash
            manifest = RunnerObjectManifest(
                scope=ObjectScope.model_validate(identity["scope"]),
                binding=identity["storage_binding"],
                bytes=receipt["bytes"],
                sha256=receipt["sha256"],
                chunk_count=count,
                chain_sha256=head,
            )
            await self._manifest(identity, manifest.model_dump(mode="json"))
            await authorize()
            await self.ledger.publish(identity, owner, manifest, fence)
            await authorize()
            return dict(receipt)

    async def _manifest(self, identity: dict[str, Any], value: dict[str, Any]) -> RunnerObjectManifest:
        manifest = RunnerObjectManifest.model_validate(value)
        if (
            manifest.scope.model_dump(mode="json") != identity["scope"]
            or manifest.binding != identity["storage_binding"]
            or manifest.sha256 != identity["receipt"]["sha256"]
            or manifest.bytes != identity["receipt"]["bytes"]
            or manifest.chunk_count != identity["expected_chunks"]
            or (manifest.chunk_count == 0 and manifest.chain_sha256 != "0" * 64)
        ):
            raise ValueError("Published manifest differs from its immutable artifact identity")
        return manifest

    async def _data(
        self,
        identity: dict[str, Any],
        manifest: RunnerObjectManifest,
        authorize: Authorize,
        observation: dict[str, Any] | None = None,
    ) -> AsyncIterator[bytes]:
        count, size, head = 0, 0, "0" * 64
        async for envelope in self.ledger.chunks(identity, observation):
            count += 1
            envelope.validate_chain(count, head)
            chunk = envelope.chunk
            expected_size = min(identity["chunk_bytes"], manifest.bytes - size)
            if (
                chunk.scope != manifest.scope
                or chunk.binding != manifest.binding
                or count > manifest.chunk_count
                or chunk.bytes != expected_size
                or expected_size <= 0
            ):
                raise ValueError("Artifact chunk differs from the published scope or stream shape")
            data = await self.objects.read_chunk(manifest.scope, chunk, authorize=authorize)
            size += len(data)
            head = envelope.hash
            yield data
        if count != manifest.chunk_count or size != manifest.bytes or head != manifest.chain_sha256:
            raise ValueError("Artifact chunk ledger differs from its scalar manifest root")

    async def reconcile(
        self,
        profile: RunnerPoolProfile,
        invocation: RunnerInvocation,
        kind: str,
        receipt: dict[str, Any],
        fence: FactoryFence,
        *,
        authorize: Authorize,
    ) -> dict[str, Any]:
        """Explicit readback can publish exact retained bytes, never upload again."""
        identity = self.identity(profile, invocation, kind, receipt)
        authorize = self._current(authorize)
        await authorize()
        observation = await self.ledger.pending(identity, fence)
        if observation["status"] == "published":
            await self._manifest(identity, observation["data"]["manifest"])
            await authorize()
            return dict(receipt)
        manifest = RunnerObjectManifest(
            scope=ObjectScope.model_validate(identity["scope"]),
            binding=identity["storage_binding"],
            sha256=receipt["sha256"],
            bytes=receipt["bytes"],
            chunk_count=observation["data"]["intent_count"],
            chain_sha256=observation["data"]["intent_head"],
        )
        await self._manifest(identity, manifest.model_dump(mode="json"))
        async with self._spooled(self._data(identity, manifest, authorize, observation), receipt, authorize):
            await authorize()
            await self.ledger.publish_readback(identity, observation, manifest, fence)
            await authorize()
        return dict(receipt)

    async def read(
        self,
        profile: RunnerPoolProfile,
        invocation: RunnerInvocation,
        kind: str,
        receipt: dict[str, Any],
        *,
        authorize: Authorize,
    ) -> AsyncIterator[bytes]:
        identity = self.identity(profile, invocation, kind, receipt)
        authorize = self._current(authorize)
        await authorize()
        manifest = await self._manifest(identity, await self.ledger.load(identity))
        async with self._spooled(self._data(identity, manifest, authorize), receipt, authorize) as spool:
            async for data in self._parts(spool.file, authorize):
                yield data

    async def restore(
        self,
        profile: RunnerPoolProfile,
        invocation: RunnerInvocation,
        kind: str,
        receipt: dict[str, Any],
        destination: Path,
        name: str,
        *,
        authorize: Authorize,
    ) -> Path:
        identity = self.identity(profile, invocation, kind, receipt)
        authorize = self._current(authorize)
        await authorize()
        manifest = await self._manifest(identity, await self.ledger.load(identity))
        if not destination.is_absolute() or not name or name in {".", ".."} or "/" in name or "\\" in name:
            raise ValueError("Artifact restoration requires a confined runner-volume file")
        parent = _directory(destination, private=True)
        try:
            root = os.fstat(parent)
            if root.st_uid != os.getuid() or stat.S_IMODE(root.st_mode) != 0o700:
                raise ValueError("Artifact restoration requires an owner-private runner volume")
            async with self._spooled(self._data(identity, manifest, authorize), receipt, authorize) as spool:
                await authorize()
                current = destination.stat(follow_symlinks=False)
                if (current.st_dev, current.st_ino) != (root.st_dev, root.st_ino):
                    raise ValueError("Runner volume changed before publication")
                os.link(spool.name, name, src_dir_fd=spool.parent, dst_dir_fd=parent, follow_symlinks=False)
                os.fsync(parent)
            return destination / name
        finally:
            os.close(parent)
