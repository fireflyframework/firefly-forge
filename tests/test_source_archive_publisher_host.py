# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Snapshot publication is complete, immutable and explicitly fails closed."""

from uuid import UUID

import pytest
from test_runner_object_artifacts_host import Ledger, Pools, allowed, authority_fence
from test_source_archive_index_host import Records

from firefly_forge.object_storage import ArtifactObjectStorage, ObjectStorageSettings, native_document_storage
from firefly_forge.runner_pool_models import pool_digest
from firefly_forge.source_archive import SourceArchiveContent
from firefly_forge.source_archive_index import SourceArchiveIndex
from firefly_forge.source_archive_service import SourceArchivePublisher
from firefly_forge.source_catalog import SourceCatalogBudget


def fixture(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    (workspace / "app.py").write_text("print(1)\n")
    (workspace / "data.bin").write_bytes(b"\x00\xffrecorded")
    (workspace / ".env").write_text("private")
    scratch = tmp_path / "scratch"
    scratch.mkdir(mode=0o700)
    catalogs = tmp_path / "catalogs"
    catalogs.mkdir(mode=0o700)
    settings = ObjectStorageSettings(
        provider="local", mode="test", local_directory=tmp_path / "objects", storage_id=UUID(int=8), chunk_bytes=4096
    )
    content = SourceArchiveContent(
        UUID(int=1), ArtifactObjectStorage(settings, native_document_storage(settings)), Ledger(), scratch
    )
    records = Records()
    index = SourceArchiveIndex(Pools(records), records, records)
    publisher = SourceArchivePublisher(content, index, catalogs, SourceCatalogBudget(min_free_bytes=0, page_entries=1))
    run = dict(
        id=UUID(int=4).hex,
        project_id=UUID(int=2).hex,
        repository_id=UUID(int=3).hex,
        attempt=1,
        branch="recorded/topic",
    )
    return publisher, workspace, run, records


async def test_publish_copies_bounded_catalog_pages_and_file_bytes_with_no_private_entries(tmp_path):
    publisher, workspace, run, records = fixture(tmp_path)
    ref = await publisher.publish(run, workspace, "a" * 40, "b" * 40, authority_fence(), authorize=allowed)
    assert ref.catalog.file_count == 2 and ref.catalog.page_count == 2
    pages = [await publisher.index.page(ref, n) for n in range(2)]
    assert [page.entries[0].path for page, _ in pages] == ["app.py", "data.bin"]
    assert len(records.parts) == 2 and all(len(page.entries) == 1 for page, _ in pages)
    assert records.rows[ref.snapshot_id].data["status"] == "published"
    assert all("chunks" not in value.data for value in records.rows.values())


async def test_changed_selected_file_during_final_publication_is_recorded_failed(tmp_path):
    publisher, workspace, run, records = fixture(tmp_path)
    save = records.save

    async def changed(row):
        await save(row)
        if getattr(row, "data", {}).get("status") == "published":
            (workspace / "app.py").write_text("changed\n")

    records.save = changed
    with pytest.raises(ValueError, match="changed"):
        await publisher.publish(run, workspace, "a" * 40, "b" * 40, authority_fence(), authorize=allowed)
    assert len(records.rows) == 1
    assert next(iter(records.rows.values())).data["status"] == "failed"


async def test_recomputed_page_chain_cannot_relabel_the_catalog_source_digest(tmp_path):
    publisher, workspace, run, records = fixture(tmp_path)
    ref = await publisher.publish(run, workspace, "a" * 40, "b" * 40, authority_fence(), authorize=allowed)
    head = "0" * 64
    for number in range(ref.catalog.page_count):
        row = records.parts[(ref.snapshot_id, number)]
        if number == 0:
            row.data["catalog"]["entries"][0]["path"] = "else.py"
        row.data["previous"] = head
        row.data["hash"] = pool_digest({k: v for k, v in row.data.items() if k != "hash"})
        head = row.data["hash"]
    records.rows[ref.snapshot_id].data["page_head"] = head
    with pytest.raises(ValueError, match="digest"):
        await publisher.index.page(ref, 0)


async def test_secret_filename_is_refused_before_any_object_write_and_recorded_unavailable(tmp_path):
    publisher, workspace, run, records = fixture(tmp_path)
    secret = "controlled-private-source-credential-938420"
    (workspace / (secret + ".txt")).write_text("not a credential payload")
    publisher.content.helper.credential_registry = lambda: {"CONTROLLED_TOKEN": secret}
    with pytest.raises(ValueError, match="credentials"):
        await publisher.publish(run, workspace, "a" * 40, "b" * 40, authority_fence(), authorize=allowed)
    assert next(iter(records.rows.values())).data["status"] == "failed"
    assert publisher.content.helper.ledger.rows == {}
