# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Native ECM contracts with private local files and controlled provider clients."""

import hashlib
import io
import sys
from dataclasses import replace
from uuid import UUID

import pytest
from pyfly.ecm.adapters.aws_s3 import AwsS3StorageAdapter
from pyfly.ecm.adapters.azure_blob import AzureBlobStorageAdapter
from pyfly.ecm.adapters.local_filesystem import LocalFilesystemStorageAdapter

from firefly_forge.object_storage import (
    ArtifactObjectStorage,
    ObjectScope,
    ObjectStorageSettings,
    ObjectWriteUncertain,
    native_document_storage,
)

SCOPE = ObjectScope(tenant_id=UUID(int=1), project_id=UUID(int=2))


async def authorized():
    return None


def settings(tmp_path, **values):
    return ObjectStorageSettings(
        provider="local", mode="test", local_directory=tmp_path / "objects", storage_id=UUID(int=3), **values
    )


async def source(*parts):
    for part in parts:
        yield part


def test_storage_disabled_has_no_filesystem_effect(tmp_path):
    configuration = ObjectStorageSettings.from_environment({})
    assert configuration.provider == "disabled"
    assert native_document_storage(configuration) is None
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize(
    "values",
    [
        {"provider": "local", "mode": "production", "local_directory": "/private/test"},
        {"provider": "local", "mode": "development", "local_directory": "relative"},
        {"provider": "s3"},
        {"provider": "s3", "bucket": "objects", "chunk_bytes": 1},
        {"provider": "minio", "bucket": "objects", "endpoint_url": "http://public.example"},
        {"provider": "azure"},
        {"provider": "unknown"},
    ],
)
def test_storage_configuration_refuses_incomplete_or_unsafe_providers(values):
    with pytest.raises(ValueError):
        ObjectStorageSettings(storage_id=UUID(int=3), **values)


def test_missing_native_gcs_support_is_explicit_not_a_local_fallback(monkeypatch):
    monkeypatch.setitem(sys.modules, "pyfly.ecm.adapters.gcs", None)
    configuration = ObjectStorageSettings(provider="gcs", storage_id=UUID(int=3), bucket="objects")
    with pytest.raises(RuntimeError, match="PyFly.*GCS"):
        native_document_storage(configuration)


def test_azure_binding_preserves_credential_rotation_but_detects_account_change():
    values = {"provider": "azure", "container": "objects", "storage_id": UUID(int=3)}
    original = ObjectStorageSettings(
        **values, connection_string="DefaultEndpointsProtocol=https;AccountName=one;AccountKey=first"
    )
    rotated = ObjectStorageSettings(
        **values, connection_string="DefaultEndpointsProtocol=https;AccountName=one;AccountKey=second"
    )
    changed = ObjectStorageSettings(
        **values, connection_string="DefaultEndpointsProtocol=https;AccountName=two;AccountKey=second"
    )
    assert original.binding == rotated.binding and original.binding != changed.binding


def test_native_provider_factories_use_pyfly_adapters(tmp_path):
    assert isinstance(native_document_storage(settings(tmp_path)), LocalFilesystemStorageAdapter)
    s3 = ObjectStorageSettings(provider="s3", bucket="objects", storage_id=UUID(int=3))
    client = object()
    adapter = native_document_storage(s3, s3_client=client)
    assert isinstance(adapter, AwsS3StorageAdapter) and adapter._client is client
    minio = ObjectStorageSettings(
        provider="minio", mode="test", bucket="objects", endpoint_url="http://127.0.0.1:9000", storage_id=UUID(int=3)
    )
    assert isinstance(native_document_storage(minio, s3_client=client), AwsS3StorageAdapter)
    azure = ObjectStorageSettings(
        provider="azure",
        account_url="https://example.blob.core.windows.net",
        container="objects",
        storage_id=UUID(int=3),
    )
    service = object()
    adapter = native_document_storage(azure, azure_service=service)
    assert isinstance(adapter, AzureBlobStorageAdapter) and adapter._service is service


async def test_stage_chunks_and_restore_verify_stream_and_scoped_native_keys(tmp_path):
    configuration = settings(tmp_path, chunk_bytes=4)
    adapter = native_document_storage(configuration)
    storage = ArtifactObjectStorage(configuration, adapter)
    manifest = await storage.stage(SCOPE, source(b"abc", b"defghijk"), authorize=authorized)
    assert [part.bytes for part in manifest.chunks] == [4, 4, 3]
    assert manifest.bytes == 11 and manifest.sha256 == hashlib.sha256(b"abcdefghijk").hexdigest()
    assert len({part.object_id for part in manifest.chunks}) == 3
    restored = [part async for part in storage.read(SCOPE, manifest, authorize=authorized)]
    assert b"".join(restored) == b"abcdefghijk"
    files = [path for path in configuration.local_directory.rglob("v1")]
    assert len(files) == 3
    assert all(f"tenants/{SCOPE.tenant_id.hex}/projects/{SCOPE.project_id.hex}/" in str(path) for path in files)
    assert "storage_uri" not in manifest.model_dump_json()


async def test_foreign_scope_or_backend_binding_refused_before_storage_read(tmp_path):
    configuration = settings(tmp_path)
    storage = ArtifactObjectStorage(configuration, native_document_storage(configuration))
    manifest = await storage.stage(SCOPE, source(b"payload"), authorize=authorized)
    foreign = ObjectScope(tenant_id=UUID(int=4), project_id=SCOPE.project_id)
    with pytest.raises(ValueError, match="scope"):
        _ = [part async for part in storage.read(foreign, manifest, authorize=authorized)]
    other = ArtifactObjectStorage(configuration.model_copy(update={"storage_id": UUID(int=5)}), storage.adapter)
    with pytest.raises(ValueError, match="binding"):
        _ = [part async for part in other.read(SCOPE, manifest, authorize=authorized)]


async def test_corrupt_stored_bytes_and_manifest_totals_are_not_accepted(tmp_path):
    configuration = settings(tmp_path)
    storage = ArtifactObjectStorage(configuration, native_document_storage(configuration))
    manifest = await storage.stage(SCOPE, source(b"payload"), authorize=authorized)
    wrong = manifest.model_copy(update={"sha256": "0" * 64})
    with pytest.raises(ValueError, match="manifest"):
        _ = [part async for part in storage.read(SCOPE, wrong, authorize=authorized)]
    next(configuration.local_directory.rglob("v1")).write_bytes(b"changed")
    with pytest.raises(ValueError, match="checksum"):
        _ = [part async for part in storage.read(SCOPE, manifest, authorize=authorized)]


async def test_uncertain_upload_exposes_readback_candidate_without_retry(tmp_path):
    configuration = settings(tmp_path)
    native = native_document_storage(configuration)

    class LostReply:
        name = "lost-reply"
        calls = 0

        async def upload(self, document, content):
            self.calls += 1
            await native.upload(document, content)
            raise TimeoutError("SECRET provider details")

        async def download(self, document, version=None):
            return await native.download(document, version)

    adapter = LostReply()
    storage = ArtifactObjectStorage(configuration, adapter)
    with pytest.raises(ObjectWriteUncertain) as caught:
        await storage.stage(SCOPE, source(b"payload"), authorize=authorized)
    assert adapter.calls == 1 and "SECRET" not in str(caught.value)
    assert await storage.read_chunk(SCOPE, caught.value.candidate, authorize=authorized) == b"payload"
    assert adapter.calls == 1


async def test_native_upload_receipt_and_current_authority_checked_after_effect(tmp_path):
    configuration = settings(tmp_path)
    native = native_document_storage(configuration)

    class WrongReceipt:
        name = "wrong-receipt"

        async def upload(self, document, content):
            return replace(await native.upload(document, content), content_hash="0" * 64)

    storage = ArtifactObjectStorage(configuration, WrongReceipt())
    with pytest.raises(ObjectWriteUncertain, match="readback"):
        await storage.stage(SCOPE, source(b"payload"), authorize=authorized)
    calls = 0

    async def revoked():
        nonlocal calls
        calls += 1
        if calls == 4:
            raise PermissionError("revoked")

    storage = ArtifactObjectStorage(configuration, native)
    with pytest.raises(PermissionError, match="revoked"):
        await storage.stage(SCOPE, source(b"payload"), authorize=revoked)
    assert len(list(configuration.local_directory.rglob("v1"))) == 2


async def test_restore_publishes_only_verified_complete_file_and_never_overwrites(tmp_path):
    configuration = settings(tmp_path, chunk_bytes=4)
    storage = ArtifactObjectStorage(configuration, native_document_storage(configuration))
    manifest = await storage.stage(SCOPE, source(b"abcdefghijk"), authorize=authorized)
    volume = tmp_path / "volume"
    volume.mkdir(mode=0o700)
    target = await storage.restore(SCOPE, manifest, volume, "archive.tar", authorize=authorized)
    assert target.read_bytes() == b"abcdefghijk"
    with pytest.raises(FileExistsError):
        await storage.restore(SCOPE, manifest, volume, "archive.tar", authorize=authorized)
    wrong = manifest.model_copy(update={"sha256": "0" * 64})
    with pytest.raises(ValueError, match="manifest"):
        await storage.restore(SCOPE, wrong, volume, "wrong.tar", authorize=authorized)
    assert [path.name for path in volume.iterdir()] == ["archive.tar"]
    with pytest.raises(ValueError, match="confined"):
        await storage.restore(SCOPE, manifest, volume, "../outside", authorize=authorized)


async def test_streaming_memory_envelope_and_durable_intent_precede_native_upload(tmp_path):
    configuration = settings(tmp_path, chunk_bytes=4096)
    native = native_document_storage(configuration)
    events = []

    class ObservedNative:
        name = "observed-native"

        async def upload(self, document, content):
            assert len(content) <= 4096
            assert events[-1] == (document.id.split("/")[-2], False)
            events.append((document.id.split("/")[-2], "upload"))
            return await native.upload(document, content)

        async def download(self, document, version=None):
            return await native.download(document, version)

    async def progress(chunk, confirmed):
        events.append((chunk.object_id.hex, confirmed))

    async def large_source():
        for _ in range(50):
            yield b"x" * 10000

    storage = ArtifactObjectStorage(configuration, ObservedNative())
    manifest = await storage.stage(SCOPE, large_source(), authorize=authorized, progress=progress)
    assert manifest.bytes == 500000 and len(manifest.chunks) == 123
    assert len(events) == 369 and all(events[n][1] is True for n in range(2, len(events), 3))


async def test_s3_roundtrip_uses_native_port_and_does_not_trust_etag():
    class S3:
        def __init__(self):
            self.objects = {}

        def put_object(self, **values):
            self.objects[values["Key"]] = values["Body"]
            return {"ETag": "not-a-sha256"}

        def get_object(self, **values):
            return {"Body": io.BytesIO(self.objects[values["Key"]])}

    configuration = ObjectStorageSettings(provider="s3", bucket="objects", storage_id=UUID(int=3))
    storage = ArtifactObjectStorage(configuration, native_document_storage(configuration, s3_client=S3()))
    manifest = await storage.stage(SCOPE, source(b"payload"), authorize=authorized)
    assert b"".join([part async for part in storage.read(SCOPE, manifest, authorize=authorized)]) == b"payload"
