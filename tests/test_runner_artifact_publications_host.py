# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Native PostgreSQL publication implementation with controlled transaction units."""

import copy
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects.postgresql import dialect
from sqlalchemy.schema import CreateTable
from test_runner_object_artifacts_host import Pools, Records, allowed, fixture, stream

from firefly_forge.object_storage import ObjectChunk, ObjectWriteUncertain
from firefly_forge.runner_artifact_publications import ForgeRunnerArtifactEntity, RunnerArtifactPublications
from firefly_forge.runner_object_artifacts import RunnerObjectChunk


async def test_actual_ledger_publishes_exact_manifest_and_survives_service_reopen(tmp_path):
    import hashlib

    service, profile, invocation, _ = fixture(tmp_path)
    records = Records()
    pools = Pools(records)
    service.ledger = RunnerArtifactPublications(pools, records, records)
    receipt = {"kind": "output", "bytes": 4, "sha256": hashlib.sha256(b"done").hexdigest()}
    fence = SimpleNamespace(model_dump=lambda **_: {"factory_schema": "factory", "epoch": 1})
    await service.upload(profile, invocation, "output", receipt, stream(b"done"), fence, authorize=allowed)
    row = next(iter(records.rows.values()))
    assert row.status == "published" and next(iter(records.parts.values())).confirmed is True
    service.ledger = RunnerArtifactPublications(pools, records, records)
    assert (
        b"".join([part async for part in service.read(profile, invocation, "output", receipt, authorize=allowed)])
        == b"done"
    )
    assert pools.checks >= 8
    forged = {**receipt, "sha256": "a" * 64}
    with pytest.raises(ValueError, match="publication"):
        _ = [part async for part in service.read(profile, invocation, "output", forged, authorize=allowed)]
    pools.valid = False
    with pytest.raises(PermissionError):
        await service.ledger.begin(row.identity, fence)


async def test_unknown_upload_cannot_be_replayed_or_read_as_a_published_artifact(tmp_path):
    import hashlib

    service, profile, invocation, _ = fixture(tmp_path)
    records = Records()
    service.ledger = RunnerArtifactPublications(Pools(records), records, records)
    receipt = {"kind": "output", "bytes": 4, "sha256": hashlib.sha256(b"done").hexdigest()}
    identity = service.identity(profile, invocation, "output", receipt)
    fence = SimpleNamespace(model_dump=lambda **_: {"factory_schema": "factory", "epoch": 1})
    await service.ledger.begin(identity, fence)
    with pytest.raises(RuntimeError, match="replay"):
        await service.ledger.begin(identity, fence)
    with pytest.raises(RuntimeError, match="unknown"):
        await service.ledger.load(identity)


def test_publication_table_is_postgresql_metadata_not_in_memory_ecm():
    statement = str(CreateTable(ForgeRunnerArtifactEntity.__table__).compile(dialect=dialect()))
    assert "PRIMARY KEY (id)" in statement and "JSONB" in statement and "upload_owner UUID NOT NULL" in statement


async def test_lost_native_upload_reply_requires_explicit_durable_readback_without_a_second_write(tmp_path):
    import hashlib

    service, profile, invocation, _ = fixture(tmp_path)
    records = Records()
    service.ledger = RunnerArtifactPublications(Pools(records), records, records)
    native = service.objects.adapter
    writes = 0

    class Lost:
        name = "controlled-lost-reply"

        async def upload(self, document, data):
            nonlocal writes
            writes += 1
            await native.upload(document, data)
            raise TimeoutError("private response details")

        async def download(self, document, version=None):
            return await native.download(document, version)

    service.objects.adapter = Lost()
    receipt = {"kind": "output", "bytes": 4, "sha256": hashlib.sha256(b"done").hexdigest()}
    fence = SimpleNamespace(model_dump=lambda **_: {"factory_schema": "factory", "epoch": 1})
    with pytest.raises(ObjectWriteUncertain):
        await service.upload(profile, invocation, "output", receipt, stream(b"done"), fence, authorize=allowed)
    row = next(iter(records.rows.values()))
    assert row.status == "pending" and next(iter(records.parts.values())).confirmed is False
    with pytest.raises(RuntimeError, match="replay"):
        await service.upload(profile, invocation, "output", receipt, stream(b"done"), fence, authorize=allowed)
    assert writes == 1
    await service.reconcile(profile, invocation, "output", receipt, fence, authorize=allowed)
    row = next(iter(records.rows.values()))
    assert row.status == "published" and next(iter(records.parts.values())).confirmed is False
    assert "readback" in row.data and writes == 1
    assert (
        b"".join([part async for part in service.read(profile, invocation, "output", receipt, authorize=allowed)])
        == b"done"
    )


async def test_provider_acknowledgement_cannot_publish_corrupt_persisted_bytes(tmp_path):
    import hashlib

    service, profile, invocation, _ = fixture(tmp_path)
    records = Records()
    service.ledger = RunnerArtifactPublications(Pools(records), records, records)
    native = service.objects.adapter

    class Corrupted:
        name = "controlled-corrupt-persistence"

        async def upload(self, document, data):
            acknowledged = await native.upload(document, data)
            next(service.objects.settings.local_directory.rglob("v1")).write_bytes(b"bad!")
            return acknowledged

        async def download(self, document, version=None):
            return await native.download(document, version)

    service.objects.adapter = Corrupted()
    receipt = {"kind": "output", "bytes": 4, "sha256": hashlib.sha256(b"done").hexdigest()}
    fence = SimpleNamespace(model_dump=lambda **_: {"factory_schema": "factory", "epoch": 1})
    with pytest.raises(ObjectWriteUncertain):
        await service.upload(profile, invocation, "output", receipt, stream(b"done"), fence, authorize=allowed)
    row = next(iter(records.rows.values()))
    assert row.status == "pending" and next(iter(records.parts.values())).confirmed is False


async def test_large_raw_artifact_uses_scalar_root_and_keyset_chunk_pages(tmp_path):
    import hashlib

    service, profile, invocation, _ = fixture(tmp_path)
    records = Records()
    service.ledger = RunnerArtifactPublications(Pools(records), records, records)
    data = b"x" * (4096 * 140)
    receipt = {"kind": "output", "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
    fence = SimpleNamespace(model_dump=lambda **_: {"factory_schema": "factory", "epoch": 1})
    await service.upload(profile, invocation, "output", receipt, stream(data), fence, authorize=allowed)
    row = next(iter(records.rows.values()))
    assert "chunks" not in row.data
    assert row.data["manifest"]["chunk_count"] == 140
    assert len(records.parts) == 140
    assert (
        b"".join([part async for part in service.read(profile, invocation, "output", receipt, authorize=allowed)])
        == data
    )
    assert records.page_sizes == [128, 12, 0]


@pytest.mark.parametrize("fault", ["foreign_scope", "sequence", "missing", "root", "unconfirmed"])
async def test_tampered_chunk_chain_never_publishes_a_restored_file(tmp_path, fault):
    import hashlib

    service, profile, invocation, _ = fixture(tmp_path)
    records = Records()
    service.ledger = RunnerArtifactPublications(Pools(records), records, records)
    data = b"x" * 9000
    receipt = {"kind": "output", "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
    fence = SimpleNamespace(model_dump=lambda **_: {"factory_schema": "factory", "epoch": 1})
    await service.upload(profile, invocation, "output", receipt, stream(data), fence, authorize=allowed)
    root = next(iter(records.rows.values()))
    last = records.parts[(root.id.hex, 3)]
    if fault == "foreign_scope":
        value = copy.deepcopy(last.envelope["chunk"])
        value["scope"]["tenant_id"] = "00000000-0000-0000-0000-000000000009"
        forged = RunnerObjectChunk.capture(3, last.envelope["previous_hash"], ObjectChunk.model_validate(value))
        last.envelope = forged.model_dump(mode="json")
        root.data["manifest"]["chain_sha256"] = forged.hash
    elif fault == "sequence":
        last.envelope["sequence"] = 4
    elif fault == "missing":
        del records.parts[(root.id.hex, 2)]
    elif fault == "root":
        root.data["manifest"]["chain_sha256"] = "0" * 64
    else:
        last.confirmed = False
    with pytest.raises(ValueError):
        await service.restore(profile, invocation, "output", receipt, service.scratch, "held.bin", authorize=allowed)
    assert not (service.scratch / "held.bin").exists()
    assert not list(service.scratch.glob(".forge-object-stage-*"))


async def test_ambient_transaction_is_refused_before_any_provider_write(tmp_path):
    import hashlib

    service, profile, invocation, ledger = fixture(tmp_path)
    ledger.pools.sessions.current = lambda: object()
    receipt = {"kind": "output", "bytes": 4, "sha256": hashlib.sha256(b"done").hexdigest()}
    fence = SimpleNamespace(model_dump=lambda **_: {})
    with pytest.raises(RuntimeError, match="independent committed"):
        await service.upload(profile, invocation, "output", receipt, stream(b"done"), fence, authorize=allowed)
    assert ledger.rows == {} and not list(service.objects.settings.local_directory.rglob("v1"))
