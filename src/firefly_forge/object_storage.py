# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Scoped, checksummed chunk staging through native PyFly document storage."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from collections.abc import AsyncIterable, AsyncIterator, Awaitable, Callable, Mapping
from contextlib import suppress
from pathlib import Path
from typing import Any, Literal, cast
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator
from pyfly.ecm.models import Document, DocumentVersion
from pyfly.ecm.ports import DocumentStoragePort

Authorize = Callable[[], Awaitable[None]]
ChunkProgress = Callable[["ObjectChunk", bool], Awaitable[None]]


class ObjectStorageSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    provider: Literal["disabled", "local", "s3", "minio", "azure", "gcs"] = "disabled"
    mode: Literal["development", "test", "production"] = "production"
    storage_id: UUID | None = None
    chunk_bytes: int = Field(default=8_388_608, ge=1, le=67_108_864, strict=True)
    local_directory: Path | None = None
    bucket: str = Field(default="", pattern=r"^[a-zA-Z0-9._-]*$")
    container: str = Field(default="", pattern=r"^[a-zA-Z0-9-]*$")
    region: str | None = None
    gcs_project: str | None = None
    key_prefix: str = Field(default="forge", pattern=r"^[a-zA-Z0-9_-]+$")
    endpoint_url: str | None = None
    account_url: str | None = None
    connection_string: SecretStr | None = None

    @model_validator(mode="after")
    def complete(self) -> ObjectStorageSettings:
        if self.provider == "disabled":
            return self
        if self.storage_id is None:
            raise ValueError("Object storage requires its stable deployment identity")
        if self.mode == "production" and self.chunk_bytes < 262_144:
            raise ValueError(
                "Production chunks must be at least 256 KiB to bound manifest metadata within runner budgets"
            )
        if self.provider == "local" and (
            self.mode == "production" or self.local_directory is None or not self.local_directory.is_absolute()
        ):
            raise ValueError("Local object storage requires an explicit development/test absolute directory")
        if self.provider in {"s3", "minio", "gcs"} and not self.bucket:
            raise ValueError("The object storage bucket is required")
        if self.provider == "minio" and not self.endpoint_url:
            raise ValueError("MinIO requires its explicit S3 endpoint")
        if self.endpoint_url:
            endpoint = urlsplit(self.endpoint_url)
            local_http = (
                self.mode != "production"
                and endpoint.scheme == "http"
                and endpoint.hostname in {"localhost", "127.0.0.1", "::1"}
            )
            if (
                not endpoint.hostname
                or endpoint.username
                or endpoint.password
                or endpoint.query
                or endpoint.fragment
                or (endpoint.scheme != "https" and not local_http)
            ):
                raise ValueError("Object storage requires TLS; HTTP is limited to local development/test endpoints")
        if self.provider == "azure" and (not self.container or not (self.account_url or self.connection_string)):
            raise ValueError("Azure storage requires its container and account or private connection string")
        if self.account_url:
            account = urlsplit(self.account_url)
            if (
                account.scheme != "https"
                or not account.hostname
                or account.username
                or account.password
                or account.query
            ):
                raise ValueError("Azure storage requires a credential-free TLS account URL")
        return self

    @classmethod
    def from_environment(cls, environment: Mapping[str, str]) -> ObjectStorageSettings:
        values: dict[str, Any] = {}
        for name in cls.model_fields:
            value = environment.get("FORGE_OBJECT_STORAGE_" + name.upper())
            if value is not None:
                values[name] = int(value) if name == "chunk_bytes" else value
        return cls.model_validate(values)

    @property
    def binding(self) -> str:
        # Credential rotation does not rename retained objects. A deployment must
        # change its identity when changing a connection-string-only account.
        value = self.model_dump(mode="json", exclude={"connection_string"})
        if self.connection_string:
            fields = dict(
                part.split("=", 1) for part in self.connection_string.get_secret_value().split(";") if "=" in part
            )
            value["azure_account_identity"] = {
                key: fields[key]
                for key in ("AccountName", "BlobEndpoint", "EndpointSuffix", "DefaultEndpointsProtocol")
                if key in fields
            }
        return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def credential_registry(self) -> dict[str, str]:
        if self.connection_string is None:
            return {}
        value = self.connection_string.get_secret_value()
        fields = dict(part.split("=", 1) for part in value.split(";") if "=" in part)
        return {
            "FORGE_OBJECT_STORAGE_CONNECTION_STRING": value,
            **{
                f"FORGE_OBJECT_STORAGE_SECRET_{key}": fields[key]
                for key in ("AccountKey", "SharedAccessSignature")
                if key in fields
            },
        }


def native_document_storage(
    settings: ObjectStorageSettings,
    *,
    s3_client: Any = None,
    azure_service: Any = None,
    azure_credential: Any = None,
    gcs_client: Any = None,
) -> DocumentStoragePort | None:
    if settings.provider == "disabled":
        return None
    if settings.provider == "gcs":
        try:
            from pyfly.ecm.adapters.gcs import GoogleCloudStorageAdapter  # type: ignore[import-not-found, unused-ignore]
        except ImportError:
            raise RuntimeError(
                "This PyFly release has no native GCS document adapter; configure a released native provider"
            ) from None
        return cast(
            DocumentStoragePort,
            GoogleCloudStorageAdapter(
                settings.bucket, project=settings.gcs_project, key_prefix=settings.key_prefix, client=gcs_client
            ),
        )
    if settings.provider == "local":
        from pyfly.ecm.adapters.local_filesystem import LocalFilesystemStorageAdapter

        assert settings.local_directory is not None
        with suppress(FileExistsError):
            settings.local_directory.mkdir(mode=0o700)
        local = settings.local_directory.lstat()
        if not stat.S_ISDIR(local.st_mode) or local.st_uid != os.getuid() or stat.S_IMODE(local.st_mode) != 0o700:
            raise ValueError("Local object storage requires an owned, unaliased private development/test directory")
        return LocalFilesystemStorageAdapter(settings.local_directory)
    if settings.provider in {"s3", "minio"}:
        from pyfly.ecm.adapters.aws_s3 import AwsS3StorageAdapter

        if s3_client is None:
            import boto3  # type: ignore[import-not-found, import-untyped, unused-ignore]
            from botocore.config import Config  # type: ignore[import-not-found, import-untyped, unused-ignore]

            s3_client = boto3.client(
                "s3",
                region_name=settings.region,
                endpoint_url=settings.endpoint_url,
                config=Config(
                    retries={"total_max_attempts": 1},
                    ignore_configured_endpoint_urls=True,
                    s3={"addressing_style": "path" if settings.provider == "minio" else "auto"},
                ),
            )
        return AwsS3StorageAdapter(
            settings.bucket, region=settings.region, key_prefix=settings.key_prefix, client=s3_client
        )
    from pyfly.ecm.adapters.azure_blob import AzureBlobStorageAdapter

    if azure_service is None:
        from azure.storage.blob import BlobServiceClient  # type: ignore[import-not-found, unused-ignore]

        if settings.connection_string:
            azure_service = BlobServiceClient.from_connection_string(
                settings.connection_string.get_secret_value(), retry_total=0
            )
        else:
            if azure_credential is None:
                from azure.identity import DefaultAzureCredential  # type: ignore[import-not-found, unused-ignore]

                azure_credential = DefaultAzureCredential(
                    exclude_interactive_browser_credential=True,
                    exclude_shared_token_cache_credential=True,
                    exclude_cli_credential=True,
                    exclude_powershell_credential=True,
                    exclude_developer_cli_credential=True,
                )
            azure_service = BlobServiceClient(settings.account_url, credential=azure_credential, retry_total=0)
    return AzureBlobStorageAdapter(
        settings.container,
        connection_string=settings.connection_string.get_secret_value() if settings.connection_string else None,
        account_url=settings.account_url,
        credential=azure_credential,
        key_prefix=settings.key_prefix,
        service=azure_service,
    )


class ObjectScope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    tenant_id: UUID
    project_id: UUID


class ObjectChunk(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    scope: ObjectScope
    binding: str = Field(pattern=r"^[a-f0-9]{64}$")
    object_id: UUID
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    bytes: int = Field(ge=0, le=67_108_864, strict=True)

    def document(self) -> Document:
        key = (
            f"tenants/{self.scope.tenant_id.hex}/projects/{self.scope.project_id.hex}/"
            f"{self.object_id.hex}/{self.sha256}"
        )
        return Document(
            id=key,
            versions=[DocumentVersion(version=1, content_hash=self.sha256, size_bytes=self.bytes, storage_uri="")],
        )


class ObjectManifest(BaseModel):
    """A bounded page utility; full runner manifests live in the paged ledger."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    protocol: Literal["forge-object-chunks-v1"] = "forge-object-chunks-v1"
    scope: ObjectScope
    binding: str = Field(pattern=r"^[a-f0-9]{64}$")
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    bytes: int = Field(ge=0, strict=True)
    chunks: tuple[ObjectChunk, ...] = Field(max_length=128)


class ObjectWriteUncertain(RuntimeError):
    def __init__(self, candidate: ObjectChunk) -> None:
        super().__init__("Object upload outcome requires explicit readback; no automatic retry or publication")
        self.candidate = candidate


class ArtifactObjectStorage:
    def __init__(self, settings: ObjectStorageSettings, adapter: DocumentStoragePort | None) -> None:
        self.settings, self.adapter = settings, adapter

    def _scope(self, scope: ObjectScope, binding: str, expected: ObjectScope) -> None:
        if scope != expected:
            raise ValueError("Object scope differs from the authorized tenant/project")
        if binding != self.settings.binding:
            raise ValueError("Object storage binding changed; use the retained deployment configuration")
        if self.adapter is None:
            raise RuntimeError("Object storage is not configured")

    async def _put(
        self, scope: ObjectScope, data: bytes, authorize: Authorize, progress: ChunkProgress | None
    ) -> ObjectChunk:
        if self.adapter is None:
            raise RuntimeError("Object storage is not configured")
        candidate = ObjectChunk(
            scope=scope,
            binding=self.settings.binding,
            object_id=uuid4(),
            sha256=hashlib.sha256(data).hexdigest(),
            bytes=len(data),
        )
        document = candidate.document()
        document.versions = []
        await authorize()
        if progress is not None:
            await progress(candidate, False)
        await authorize()
        try:
            receipt = await self.adapter.upload(document, data)
            if (
                receipt.version != 1
                or receipt.content_hash != candidate.sha256
                or receipt.size_bytes != candidate.bytes
            ):
                raise ValueError("Provider receipt differs from the requested bytes")
        except Exception:
            raise ObjectWriteUncertain(candidate) from None
        await authorize()
        try:
            stored = await self.adapter.download(candidate.document(), 1)
            if len(stored) != candidate.bytes or hashlib.sha256(stored).hexdigest() != candidate.sha256:
                raise ValueError("Provider acknowledgement did not preserve the requested bytes")
        except Exception:
            raise ObjectWriteUncertain(candidate) from None
        await authorize()
        if progress is not None:
            await progress(candidate, True)
        return candidate

    async def write_chunks(
        self,
        scope: ObjectScope,
        source: AsyncIterable[bytes],
        *,
        authorize: Authorize,
        progress: ChunkProgress | None = None,
    ) -> AsyncIterator[ObjectChunk]:
        """Yield confirmed chunks without retaining a full-file reference array."""
        if self.adapter is None:
            raise RuntimeError("Object storage is not configured")
        await authorize()
        buffer = bytearray()
        async for part in source:
            if not isinstance(part, bytes):
                raise TypeError("An artifact producer must stream bytes")
            offset = 0
            while offset < len(part):
                take = min(self.settings.chunk_bytes - len(buffer), len(part) - offset)
                buffer.extend(memoryview(part)[offset : offset + take])
                offset += take
                if len(buffer) == self.settings.chunk_bytes:
                    chunk = await self._put(scope, bytes(buffer), authorize, progress)
                    buffer.clear()
                    yield chunk
        if buffer:
            chunk = await self._put(scope, bytes(buffer), authorize, progress)
            buffer.clear()
            yield chunk
        await authorize()

    async def stage(
        self,
        scope: ObjectScope,
        source: AsyncIterable[bytes],
        *,
        authorize: Authorize,
        progress: ChunkProgress | None = None,
    ) -> ObjectManifest:
        """Stage at most one small page; runner uploads use write_chunks instead."""
        digest, total = hashlib.sha256(), 0

        async def counted() -> AsyncIterator[bytes]:
            nonlocal total
            async for part in source:
                total += len(part)
                if total > 128 * self.settings.chunk_bytes:
                    raise ValueError("Use the paged runner publication protocol for multi-page artifacts")
                digest.update(part)
                yield part

        chunks = [chunk async for chunk in self.write_chunks(scope, counted(), authorize=authorize, progress=progress)]
        return ObjectManifest(
            scope=scope, binding=self.settings.binding, sha256=digest.hexdigest(), bytes=total, chunks=tuple(chunks)
        )

    async def read_chunk(self, scope: ObjectScope, chunk: ObjectChunk, *, authorize: Authorize) -> bytes:
        self._scope(chunk.scope, chunk.binding, scope)
        if chunk.bytes > self.settings.chunk_bytes:
            raise ValueError("Object chunk exceeds its configured memory envelope")
        assert self.adapter is not None
        await authorize()
        data = await self.adapter.download(chunk.document(), 1)
        await authorize()
        if len(data) != chunk.bytes or hashlib.sha256(data).hexdigest() != chunk.sha256:
            raise ValueError("Stored object checksum differs from its manifest")
        return data

    async def read(self, scope: ObjectScope, manifest: ObjectManifest, *, authorize: Authorize) -> AsyncIterator[bytes]:
        self._scope(manifest.scope, manifest.binding, scope)
        digest, size = hashlib.sha256(), 0
        for chunk in manifest.chunks:
            data = await self.read_chunk(scope, chunk, authorize=authorize)
            digest.update(data)
            size += len(data)
            yield data
        if size != manifest.bytes or digest.hexdigest() != manifest.sha256:
            raise ValueError("Object manifest aggregate checksum or size differs")

    async def restore(
        self, scope: ObjectScope, manifest: ObjectManifest, directory: Path, name: str, *, authorize: Authorize
    ) -> Path:
        """Publish a verified file once into an infrastructure-owned private volume."""
        if not directory.is_absolute() or not name or name in {".", ".."} or "/" in name or "\\" in name:
            raise ValueError("Artifact restore requires a confined runner-volume file")
        parent = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        temporary = ".forge-restore-" + uuid4().hex
        descriptor = None
        try:
            identity = os.fstat(parent)
            if identity.st_uid != os.getuid() or stat.S_IMODE(identity.st_mode) != 0o700:
                raise ValueError("Artifact restore requires an owner-private runner volume")
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
            async for data in self.read(scope, manifest, authorize=authorize):
                view = memoryview(data)
                while view:
                    count = os.write(descriptor, view)
                    if count <= 0:
                        raise OSError("Artifact restore did not make progress")
                    view = view[count:]
            os.fsync(descriptor)
            await authorize()
            current = directory.stat(follow_symlinks=False)
            if (current.st_dev, current.st_ino) != (identity.st_dev, identity.st_ino):
                raise ValueError("Runner volume changed before artifact publication")
            os.link(temporary, name, src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)
            os.fsync(parent)
            return directory / name
        finally:
            if descriptor is not None:
                opened = os.fstat(descriptor)
                try:
                    observed = os.stat(temporary, dir_fd=parent, follow_symlinks=False)
                    if (opened.st_dev, opened.st_ino) == (observed.st_dev, observed.st_ino):
                        os.unlink(temporary, dir_fd=parent)
                except FileNotFoundError:
                    pass
                os.close(descriptor)
            os.close(parent)
