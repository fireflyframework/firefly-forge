# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Immutable catalog-linked file bytes using the native paged object ledger."""

from __future__ import annotations

import codecs
import hashlib
import json
import os
import re
import stat
from collections.abc import AsyncGenerator
from contextlib import aclosing
from contextvars import Context
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from firefly_forge.object_storage import ArtifactObjectStorage, Authorize, ObjectChunk, ObjectScope
from firefly_forge.runner_object_artifacts import (
    ArtifactLedger,
    RunnerObjectArtifacts,
    RunnerObjectChunk,
    RunnerObjectManifest,
)
from firefly_forge.runner_pool_models import FactoryFence, pool_digest
from firefly_forge.secrets import KnownCredentialScan, credential_values
from firefly_forge.source_archive_scan_models import SourceCredentialScanReceiptV1
from firefly_forge.source_catalog import (
    SourceCatalogEntryV1,
    _assert_record,
    _directory,
    _expected,
    _private,
    _read,
    _stamp,
)
from firefly_forge.verification_source_preparation import PreparedVerificationSource


def catalog_fence(prepared: PreparedVerificationSource) -> None:
    """Keep the complete captured file witnesses across the final SQL awaits."""
    prepared.fence()
    source = _directory(prepared.workspace)
    root = _directory(prepared.catalog_root, private=True)
    folder = data = None
    try:
        folder = _directory(prepared.reference.artifact_id, root, private=True)
        raw = _read(folder, "manifest.json", 1048576)
        if hashlib.sha256(raw).hexdigest() != prepared.reference.manifest_sha256:
            raise ValueError("Source catalog changed before publication")
        manifest = json.loads(raw)
        name = manifest["data_run"]
        if not isinstance(name, str) or re.fullmatch(r"run-[a-f0-9]{32}", name) is None:
            raise ValueError("Source catalog page namespace changed")
        data = _directory(name, folder, private=True)
        width = manifest["budget"]["chunk_bytes"]
        for page in range(prepared.reference.page_count):
            encoded = _read(data, f"page-{page:016x}.jsonl", width)
            if hashlib.sha256(encoded).digest() != _expected(folder, manifest["proof"], width, page):
                raise ValueError("Source catalog page changed before publication")
            for line in encoded.splitlines():
                _assert_record(source, json.loads(line))
        prepared.fence()
    finally:
        for fd in (data, folder, root, source):
            if fd is not None:
                os.close(fd)


def public_entry(entry: SourceCatalogEntryV1) -> None:
    parts = entry.path.rstrip("/").split("/")
    if (
        not entry.path
        or entry.path.startswith("/")
        or "\\" in entry.path
        or any(part in {"", ".", ".."} or any(ord(c) < 32 for c in part) for part in parts)
        or _private(entry.path)
        or entry.kind == "file"
        and (not stat.S_ISREG(entry.mode) or entry.sha256 is None)
        or entry.kind == "directory"
        and (not stat.S_ISDIR(entry.mode) or entry.sha256 is not None)
    ):
        raise ValueError("Source content requires an exact public catalog entry")


class SourceArchiveContent:
    def __init__(self, tenant: UUID, objects: ArtifactObjectStorage, ledger: ArtifactLedger, scratch: Path) -> None:
        self.helper = RunnerObjectArtifacts(tenant, objects, ledger, scratch)

    def credential_fingerprint(self) -> str:
        # Request-specific bearer taints must not make another authorized reader stale.
        values = Context().run(credential_values, self.helper.credential_registry())
        return pool_digest({"configured_credentials": values})

    def identity(self, binding: dict[str, Any], entry: SourceCatalogEntryV1) -> dict[str, Any]:
        public_entry(entry)
        if entry.kind != "file":
            raise ValueError("Directories have no source content")
        scope = ObjectScope(tenant_id=self.helper.tenant_id, project_id=UUID(binding["project_id"]))
        receipt = {"kind": "source", "bytes": entry.byte_count, "sha256": entry.sha256}
        width = self.helper.objects.settings.chunk_bytes
        value = {
            "protocol": "forge-source-content-v1",
            "scope": scope.model_dump(mode="json"),
            "snapshot_id": binding["snapshot_id"],
            "source_digest": binding["source_digest"],
            "entry": entry.model_dump(mode="json"),
            "storage_binding": self.helper.objects.settings.binding,
            "chunk_bytes": width,
            "expected_chunks": (entry.byte_count + width - 1) // width,
            "receipt": receipt,
        }
        return {"key": uuid5(NAMESPACE_URL, pool_digest(value)).hex, **value}

    async def _source(self, workspace: Path, entry: SourceCatalogEntryV1, current: Authorize) -> AsyncGenerator[bytes]:
        parent = _directory(workspace)
        fd = None
        try:
            parts = entry.path.split("/")
            for part in parts[:-1]:
                child = _directory(part, parent)
                os.close(parent)
                parent = child
            fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            before = os.fstat(fd)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_uid != os.getuid()
                or before.st_nlink != 1
                or before.st_mode != entry.mode
                or before.st_size != entry.byte_count
            ):
                raise ValueError("Source file differs from its immutable catalog")
            while data := os.read(fd, self.helper.objects.settings.chunk_bytes):
                await current()
                yield data
            if _stamp(before) != _stamp(os.fstat(fd)) or _stamp(before) != _stamp(
                os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
            ):
                raise ValueError("Source changed during content capture")
        finally:
            if fd is not None:
                os.close(fd)
            os.close(parent)

    async def publish(
        self,
        binding: dict[str, Any],
        prepared: PreparedVerificationSource,
        entry: SourceCatalogEntryV1,
        fence: FactoryFence,
        *,
        authorize: Authorize,
    ) -> dict[str, Any]:
        identity = self.identity(binding, entry)
        authorize = self.helper._current(authorize)
        receipt = identity["receipt"]
        async with (
            aclosing(self._source(prepared.workspace, entry, authorize)) as source,
            self.helper._spooled(source, receipt, authorize) as spool,
        ):
            owner, prior = await self.helper.ledger.begin(identity, fence)
            if prior is None:

                async def progress(chunk: ObjectChunk, confirmed: bool) -> None:
                    await authorize()
                    await self.helper.ledger.progress(identity, owner, chunk, confirmed, fence)

                count, head = 0, "0" * 64
                async for chunk in self.helper.objects.write_chunks(
                    ObjectScope.model_validate(identity["scope"]),
                    self.helper._parts(spool.file, authorize),
                    authorize=authorize,
                    progress=progress,
                ):
                    count += 1
                    head = RunnerObjectChunk.capture(count, head, chunk).hash
                manifest = RunnerObjectManifest(
                    scope=ObjectScope.model_validate(identity["scope"]),
                    binding=identity["storage_binding"],
                    sha256=receipt["sha256"],
                    bytes=receipt["bytes"],
                    chunk_count=count,
                    chain_sha256=head,
                )
                await self.helper._manifest(identity, manifest.model_dump(mode="json"))
                await authorize()
                await self.helper.ledger.publish(identity, owner, manifest, fence)
            else:
                await self.helper._manifest(identity, prior)
            await authorize()
        return {
            "identity_sha256": pool_digest(identity),
            "receipt": receipt,
            "credential_fingerprint": self.credential_fingerprint(),
        }

    def _descriptor(self, identity: dict[str, Any], descriptor: dict[str, Any]) -> None:
        if (
            descriptor
            != {
                "identity_sha256": pool_digest(identity),
                "receipt": identity["receipt"],
                "credential_fingerprint": descriptor.get("credential_fingerprint"),
            }
            or re.fullmatch(r"[a-f0-9]{64}", str(descriptor.get("credential_fingerprint"))) is None
        ):
            raise ValueError("Source content differs from its catalog or immutable object binding")

    async def scan(
        self,
        binding: dict[str, Any],
        entry: SourceCatalogEntryV1,
        descriptor: dict[str, Any],
        *,
        authorize: Authorize,
    ) -> str:
        identity = self.identity(binding, entry)
        self._descriptor(identity, descriptor)
        authorize = self.helper._current(authorize)
        await authorize()
        manifest = await self.helper._manifest(identity, await self.helper.ledger.load(identity))
        scanner = KnownCredentialScan(credential_values(self.helper.credential_registry()))
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        digest, size = hashlib.sha256(), 0
        async for data in self.helper._data(identity, manifest, authorize):
            await authorize()
            digest.update(data)
            size += len(data)
            if scanner.feed(decoder.decode(data)):
                raise ValueError("Retained source contains configured credentials")
        if scanner.feed(decoder.decode(b"", final=True)) or scanner.feed(None):
            raise ValueError("Retained source contains configured credentials")
        if size != entry.byte_count or digest.hexdigest() != entry.sha256:
            raise ValueError("Retained source differs from its immutable catalog content")
        await authorize()
        return pool_digest(identity)

    async def window(
        self,
        binding: dict[str, Any],
        entry: SourceCatalogEntryV1,
        descriptor: dict[str, Any],
        offset: int,
        limit: int,
        *,
        authorize: Authorize,
        scan: SourceCredentialScanReceiptV1 | None = None,
    ) -> bytes:
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 1048576:
            raise ValueError("Source content requires a bounded nonnegative byte window")
        identity = self.identity(binding, entry)
        self._descriptor(identity, descriptor)
        fingerprint = self.credential_fingerprint()
        if descriptor["credential_fingerprint"] != fingerprint and (
            scan is None
            or scan.snapshot_id != binding["snapshot_id"]
            or scan.project_id != binding["project_id"]
            or scan.tenant_id != self.helper.tenant_id.hex
            or scan.source_digest != binding["source_digest"]
            or scan.storage_binding != identity["storage_binding"]
            or scan.credential_fingerprint != fingerprint
        ):
            raise ValueError("Configured credentials changed; an explicit retained-byte credential scan is required")

        if offset > entry.byte_count:
            raise ValueError("Source window is outside the recorded file")
        authorize = self.helper._current(authorize)
        await authorize()
        manifest = await self.helper._manifest(identity, await self.helper.ledger.load(identity))
        end = min(manifest.bytes, offset + limit)
        scanner = KnownCredentialScan(credential_values(self.helper.credential_registry()))
        guard = (scanner.overlap + 1) * 4
        scan_start, scan_end = max(0, offset - guard), min(manifest.bytes, end + guard)
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        result = bytearray()
        count, size, head = 0, 0, "0" * 64
        async for envelope in self.helper.ledger.chunks(identity):
            await authorize()
            count += 1
            envelope.validate_chain(count, head)
            chunk = envelope.chunk
            expected = min(identity["chunk_bytes"], manifest.bytes - size)
            if (
                chunk.scope != manifest.scope
                or chunk.binding != manifest.binding
                or count > manifest.chunk_count
                or expected <= 0
                or chunk.bytes != expected
            ):
                raise ValueError("Source chunk scope or extent changed")
            chunk_end = size + chunk.bytes
            if end > offset and chunk_end > scan_start and size < scan_end:
                data = await self.helper.objects.read_chunk(manifest.scope, chunk, authorize=authorize)
                guarded = data[max(0, scan_start - size) : min(len(data), scan_end - size)]
                if scanner.feed(decoder.decode(guarded)):
                    raise ValueError("Source window contains configured credentials")
                first, last = max(offset, size), min(end, chunk_end)
                if first < last:
                    result.extend(data[first - size : last - size])
            size = chunk_end
            head = envelope.hash
        if (
            count != manifest.chunk_count
            or size != manifest.bytes
            or head != manifest.chain_sha256
            or len(result) != end - offset
        ):
            raise ValueError("Source chunk ledger differs from its immutable file manifest")
        if scanner.feed(decoder.decode(b"", final=True)) or scanner.feed(None):
            raise ValueError("Source window contains configured credentials")
        await authorize()
        return bytes(result)
