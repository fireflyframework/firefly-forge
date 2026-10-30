# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Publication roots never contain an unbounded project file array."""

import copy
from types import SimpleNamespace
from uuid import UUID

import pytest
from test_runner_object_artifacts_host import Pools, authority_fence

from firefly_forge.source_archive_index import SourceArchiveIndex, SourceArchiveRefV1
from firefly_forge.source_catalog import SourceCatalogRefV1


class Records:
    def __init__(self):
        self.rows, self.parts = {}, {}

    async def locked(self, value):
        return self.rows.get(str(value))

    async def at(self, value, page):
        return self.parts.get((value.hex, page))

    async def save(self, row):
        if hasattr(row, "snapshot_id"):
            self.parts[(row.snapshot_id.hex, row.page)] = row
        else:
            self.rows[row.id.hex] = row

    def _require_session(self):
        async def flush():
            return None

        return SimpleNamespace(flush=flush)


def reference():
    catalog = SourceCatalogRefV1(
        artifact_id="b" * 32,
        manifest_sha256="c" * 64,
        source_digest="d" * 64,
        manifest_digest="e" * 64,
        byte_count=0,
        file_count=0,
        entry_count=0,
        page_count=0,
    )
    return SourceArchiveRefV1(
        snapshot_id="a" * 32,
        tenant_id=UUID(int=1).hex,
        project_id=UUID(int=2).hex,
        repository_id=UUID(int=3).hex,
        run_id=UUID(int=4).hex,
        attempt=1,
        head="f" * 40,
        tree="1" * 40,
        branch="recorded/topic",
        catalog=catalog,
        storage_binding="2" * 64,
    )


async def test_empty_snapshot_publishes_with_scalar_root_and_refuses_ambient_transaction():
    records = Records()
    index = SourceArchiveIndex(Pools(records), records, records)
    ref = reference()
    assert await index.begin(ref, authority_fence()) is None
    await index.seal(ref, authority_fence(), lambda: None)
    assert await index.get(ref.snapshot_id) == ref
    assert "pages" not in records.rows[ref.snapshot_id].data
    index.pools.sessions.current = lambda: object()
    with pytest.raises(RuntimeError, match="independent"):
        await index.get(ref.snapshot_id)


async def test_pending_or_failed_snapshot_never_looks_published_or_restarts_write():
    records = Records()
    index = SourceArchiveIndex(Pools(records), records, records)
    ref = reference()
    await index.begin(ref, authority_fence())
    before = copy.deepcopy(records.rows)
    with pytest.raises(ValueError, match="unavailable"):
        await index.get(ref.snapshot_id)
    with pytest.raises(RuntimeError, match="reconciliation"):
        await index.begin(ref, authority_fence())
    assert records.rows[ref.snapshot_id].data == before[ref.snapshot_id].data
    await index.fail(ref.snapshot_id, authority_fence())
    with pytest.raises(ValueError, match="unavailable"):
        await index.get(ref.snapshot_id)
