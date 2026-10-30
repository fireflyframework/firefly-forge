# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Published source content survives workspace loss without granting a live read."""

import hashlib
import os
from uuid import UUID

import pytest
from test_runner_object_artifacts_host import Ledger, allowed, authority_fence

from firefly_forge.object_storage import ArtifactObjectStorage, ObjectStorageSettings, native_document_storage
from firefly_forge.source_archive import SourceArchiveContent
from firefly_forge.source_catalog import SourceCatalogBudget, read_source_catalog_page
from firefly_forge.verification_source_preparation import prepare_verification_source


async def test_catalog_file_is_uploaded_and_read_from_scoped_objects_after_workspace_loss(tmp_path):
    workspace = tmp_path / "source"
    workspace.mkdir(mode=0o700)
    source = workspace / "example.py"
    source.write_bytes(b"print('retained')\n" * 10000)
    source.chmod(0o644)
    catalog = tmp_path / "catalog"
    catalog.mkdir(mode=0o700)
    prepared = await prepare_verification_source(
        workspace, catalog, budget=SourceCatalogBudget(min_free_bytes=0), assert_current=allowed
    )
    entry = read_source_catalog_page(catalog, prepared.reference, 0).entries[0]
    settings = ObjectStorageSettings(
        provider="local", mode="test", local_directory=tmp_path / "objects", storage_id=UUID(int=8), chunk_bytes=4096
    )
    scratch = tmp_path / "scratch"
    scratch.mkdir(mode=0o700)
    content = SourceArchiveContent(
        UUID(int=1), ArtifactObjectStorage(settings, native_document_storage(settings)), Ledger(), scratch
    )
    binding = dict(snapshot_id="a" * 32, project_id=UUID(int=2).hex, source_digest=prepared.source_digest)
    descriptor = await content.publish(binding, prepared, entry, authority_fence(), authorize=allowed)
    source.unlink()
    restored = await content.window(binding, entry, descriptor, 2, 31, authorize=allowed)
    assert restored == (b"print('retained')\n" * 10000)[2:33]
    assert descriptor["receipt"]["sha256"] == hashlib.sha256(b"print('retained')\n" * 10000).hexdigest()
    assert not list(scratch.iterdir())


@pytest.mark.parametrize(
    "path", ["../source", "/source", "a//b", "a/./b", "a\\b", ".env", "a/settings.key", ".ssh/id_rsa"]
)
async def test_public_source_entry_never_becomes_a_path_capability(tmp_path, path):
    import stat

    from firefly_forge.source_archive import public_entry
    from firefly_forge.source_catalog import SourceCatalogEntryV1

    entry = SourceCatalogEntryV1(path=path, kind="file", mode=stat.S_IFREG | 0o644, byte_count=1, sha256="a" * 64)
    with pytest.raises(ValueError):
        public_entry(entry)


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "secret"])
async def test_capture_refuses_aliases_and_known_secret_without_leaking_source_descriptors(tmp_path, kind, monkeypatch):
    workspace = tmp_path / "source"
    workspace.mkdir(mode=0o700)
    source = workspace / "file.txt"
    source.write_bytes(b"controlled-private-source-credential-938420")
    catalogs = tmp_path / "catalogs"
    catalogs.mkdir(mode=0o700)
    prepared = await prepare_verification_source(
        workspace, catalogs, budget=SourceCatalogBudget(min_free_bytes=0), assert_current=allowed
    )
    entry = read_source_catalog_page(catalogs, prepared.reference, 0).entries[0]
    settings = ObjectStorageSettings(
        provider="local", mode="test", local_directory=tmp_path / "objects", storage_id=UUID(int=8), chunk_bytes=4096
    )
    scratch = tmp_path / "scratch"
    scratch.mkdir(mode=0o700)
    ledger = Ledger()
    content = SourceArchiveContent(
        UUID(int=1), ArtifactObjectStorage(settings, native_document_storage(settings)), ledger, scratch
    )
    if kind == "secret":
        content.helper.credential_registry = lambda: {"CONTROLLED_TOKEN": "controlled-private-source-credential-938420"}
    else:
        target = tmp_path / "target"
        source.rename(target)
        if kind == "symlink":
            source.symlink_to(target)
        else:
            os.link(target, source)
    opened, closed = [], []
    native_open, native_close = os.open, os.close

    def opening(*args, **kwargs):
        result = native_open(*args, **kwargs)
        if args[0] == "file.txt":
            opened.append(result)
        return result

    def closing(fd):
        closed.append(fd)
        return native_close(fd)

    monkeypatch.setattr(os, "open", opening)
    monkeypatch.setattr(os, "close", closing)
    with pytest.raises((ValueError, OSError)):
        await content.publish(
            dict(snapshot_id="a" * 32, project_id=UUID(int=2).hex, source_digest=prepared.source_digest),
            prepared,
            entry,
            authority_fence(),
            authorize=allowed,
        )
    assert ledger.rows == {} and not list(scratch.iterdir())
    assert all(fd in closed for fd in opened), "Source descriptors must close before a refused read returns"


async def test_window_fetches_only_intersecting_chunk_bytes_and_never_restores_the_file(tmp_path, monkeypatch):
    workspace = tmp_path / "source"
    workspace.mkdir(mode=0o700)
    data = b"0123456789abcdef" * 50000
    (workspace / "large.txt").write_bytes(data)
    catalogs = tmp_path / "catalogs"
    catalogs.mkdir(mode=0o700)
    prepared = await prepare_verification_source(
        workspace, catalogs, budget=SourceCatalogBudget(min_free_bytes=0), assert_current=allowed
    )
    entry = read_source_catalog_page(catalogs, prepared.reference, 0).entries[0]
    settings = ObjectStorageSettings(
        provider="local", mode="test", local_directory=tmp_path / "objects", storage_id=UUID(int=8), chunk_bytes=4096
    )
    scratch = tmp_path / "scratch"
    scratch.mkdir(mode=0o700)
    content = SourceArchiveContent(
        UUID(int=1), ArtifactObjectStorage(settings, native_document_storage(settings)), Ledger(), scratch
    )
    binding = dict(snapshot_id="a" * 32, project_id=UUID(int=2).hex, source_digest=prepared.source_digest)
    descriptor = await content.publish(binding, prepared, entry, authority_fence(), authorize=allowed)
    reads = []
    native_read = content.helper.objects.read_chunk

    async def read(scope, chunk, **kwargs):
        reads.append(chunk)
        return await native_read(scope, chunk, **kwargs)

    def no_spool(*args, **kwargs):
        raise AssertionError("Window reads must not restore the full file")

    monkeypatch.setattr(content.helper.objects, "read_chunk", read)
    monkeypatch.setattr(content.helper, "_spooled", no_spool)
    assert await content.window(binding, entry, descriptor, 180000, 31, authorize=allowed) == data[180000:180031]
    assert len(reads) <= 3 and not list(scratch.iterdir())


async def test_changed_configured_credentials_hold_published_content_for_fresh_capture(tmp_path):
    from test_source_archive_publisher_host import fixture

    publisher, workspace, run, _ = fixture(tmp_path)
    ref = await publisher.publish(run, workspace, "a" * 40, "b" * 40, authority_fence(), authorize=allowed)
    page, descriptors = await publisher.index.page(ref, 0)
    publisher.content.helper.credential_registry = lambda: {"NEW_TOKEN": "new-configured-source-credential-72145"}
    with pytest.raises(ValueError, match="credentials"):
        await publisher.content.window(ref.content_binding(), page.entries[0], descriptors[0], 0, 3, authorize=allowed)


async def test_reader_specific_taints_are_scanned_without_invalidating_other_readers(tmp_path):
    from test_source_archive_publisher_host import fixture

    from firefly_forge.secrets import ephemeral_credentials

    publisher, workspace, run, _ = fixture(tmp_path)
    credential = "new-reader-session-private-credential-99591"
    (workspace / "app.py").write_text(credential)
    with ephemeral_credentials({"ACCESS_TOKEN": "first-reader-private-credential-81231"}):
        ref = await publisher.publish(run, workspace, "a" * 40, "b" * 40, authority_fence(), authorize=allowed)
    page0, descriptors0 = await publisher.index.page(ref, 0)
    page1, descriptors1 = await publisher.index.page(ref, 1)
    with ephemeral_credentials({"ACCESS_TOKEN": credential}):
        assert (
            await publisher.content.window(
                ref.content_binding(), page1.entries[0], descriptors1[0], 0, 3, authorize=allowed
            )
            == b"\x00\xffr"
        )
        with pytest.raises(ValueError, match="credentials"):
            await publisher.content.window(
                ref.content_binding(), page0.entries[0], descriptors0[0], 0, 3, authorize=allowed
            )
