# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Publication consumes real native chunk objects through a controlled ledger."""

import copy
import hashlib
from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from firefly_forge.object_storage import ArtifactObjectStorage, ObjectStorageSettings, native_document_storage
from firefly_forge.runner_artifact_publications import ForgeRunnerArtifactChunkEntity, RunnerArtifactPublications
from firefly_forge.runner_object_artifacts import RunnerObjectArtifacts
from firefly_forge.runner_pool_models import RunnerInvocation, RunnerPoolProfile


class Records:
    def __init__(self):
        self.rows, self.parts, self.page_sizes = {}, {}, []

    async def locked(self, key):
        return self.rows.get(key)

    async def sequence(self, publication, sequence):
        return self.parts.get((publication.hex, sequence))

    async def after(self, publication, after):
        rows = sorted(
            (row for row in self.parts.values() if row.publication_id == publication and row.sequence > after),
            key=lambda row: row.sequence,
        )[:128]
        self.page_sizes.append(len(rows))
        return rows

    async def save(self, row):
        if isinstance(row, ForgeRunnerArtifactChunkEntity):
            self.parts[(row.publication_id.hex, row.sequence)] = row
        else:
            self.rows[row.id.hex] = row

    def _require_session(self):
        return SimpleNamespace(flush=allowed)


class Pools:
    def __init__(self, records):
        self.records, self.valid, self.checks = records, True, 0
        self.sessions = SimpleNamespace(current=lambda: None)

    @asynccontextmanager
    async def _unit(self):
        prior_rows, prior_parts = copy.deepcopy(self.records.rows), copy.deepcopy(self.records.parts)
        try:
            yield
        except BaseException:
            self.records.rows, self.records.parts = prior_rows, prior_parts
            raise

    async def _owned(self, fence):
        self.checks += 1
        if not self.valid:
            raise PermissionError("Factory ownership changed")


class Ledger(RunnerArtifactPublications):
    def __init__(self):
        records = Records()
        super().__init__(Pools(records), records, records)

    @property
    def rows(self):
        return {key: {"status": row.status} for key, row in self.records.rows.items()}


def authority_fence():
    return SimpleNamespace(model_dump=lambda **_: {})


def fixture(tmp_path):
    settings = ObjectStorageSettings(
        provider="local", mode="test", local_directory=tmp_path / "objects", storage_id=UUID(int=3), chunk_bytes=4096
    )
    scratch = tmp_path / "scratch"
    scratch.mkdir(mode=0o700)
    ledger = Ledger()
    objects = ArtifactObjectStorage(settings, native_document_storage(settings))
    service = RunnerObjectArtifacts(UUID(int=1), objects, ledger, scratch)
    profile = RunnerPoolProfile(
        profile_id="test",
        daemon_id="daemon",
        approved_images=("test/worker@sha256:" + "a" * 64,),
        transfer_image="test/transfer@sha256:" + "b" * 64,
    )
    request = RunnerInvocation(
        execution_id=uuid4().hex,
        project_id=UUID(int=2),
        repository_id=UUID(int=4),
        run_id=UUID(int=5),
        profile_id=profile.profile_id,
        profile_revision=profile.revision,
        image=profile.approved_images[0],
        argv=("true",),
        source_digest="a" * 64,
        archive_digest=hashlib.sha256(b"source").hexdigest(),
        source_bytes=6,
    )
    return service, profile, request, ledger


async def allowed():
    pass


async def stream(data):
    for offset in range(0, len(data), 1024):
        yield data[offset : offset + 1024]


async def test_publication_survives_new_controller_without_local_artifact_file(tmp_path):
    service, profile, invocation, ledger = fixture(tmp_path)
    data = b"retained-result" * 1000
    receipt = {"kind": "result", "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
    await service.upload(profile, invocation, "result", receipt, stream(data), authority_fence(), authorize=allowed)
    assert list(service.scratch.iterdir()) == []
    restarted = RunnerObjectArtifacts(service.tenant_id, service.objects, ledger, service.scratch)
    assert (
        b"".join([part async for part in restarted.read(profile, invocation, "result", receipt, authorize=allowed)])
        == data
    )
    target = await restarted.restore(
        profile, invocation, "result", receipt, service.scratch, "result.tar", authorize=allowed
    )
    assert target.read_bytes() == data
    assert all(row["status"] == "published" for row in ledger.rows.values())


async def test_different_manifest_cannot_overwrite_and_known_repeat_does_not_upload(tmp_path):
    service, profile, invocation, ledger = fixture(tmp_path)
    receipt = {"kind": "output", "bytes": 3, "sha256": hashlib.sha256(b"one").hexdigest()}
    await service.upload(profile, invocation, "output", receipt, stream(b"one"), authority_fence(), authorize=allowed)
    files = sorted(service.objects.settings.local_directory.rglob("v1"))
    await service.upload(profile, invocation, "output", receipt, stream(b"one"), authority_fence(), authorize=allowed)
    assert sorted(service.objects.settings.local_directory.rglob("v1")) == files
    wrong = {**receipt, "sha256": hashlib.sha256(b"two").hexdigest()}
    with pytest.raises(ValueError, match="publication"):
        await service.upload(profile, invocation, "output", wrong, stream(b"two"), authority_fence(), authorize=allowed)
    assert len(ledger.rows) == 1
    assert sorted(service.objects.settings.local_directory.rglob("v1")) == files


async def test_bad_size_digest_or_split_credential_never_reaches_object_provider(tmp_path):
    service, profile, invocation, ledger = fixture(tmp_path)
    service.credential_registry = lambda: {"FORGE_TEST_TOKEN": "credential-split-value"}
    data = b"credential-split-value!"
    receipt = {"kind": "output", "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}

    async def split_secret():
        yield b"credential-"
        yield b"split-"
        yield b"value!"

    with pytest.raises(ValueError, match="credential"):
        await service.upload(
            profile, invocation, "output", receipt, split_secret(), authority_fence(), authorize=allowed
        )
    assert ledger.rows == {} and not list(service.objects.settings.local_directory.rglob("v1"))
    receipt = {"kind": "output", "bytes": 4, "sha256": "a" * 64}
    with pytest.raises(ValueError, match="identity"):
        await service.upload(
            profile, invocation, "output", receipt, stream(b"1234"), authority_fence(), authorize=allowed
        )
    assert ledger.rows == {} and list(service.scratch.iterdir()) == []


async def test_source_receipt_is_frozen_and_unknown_project_is_not_guessed(tmp_path):
    service, profile, invocation, _ = fixture(tmp_path)
    receipt = {"kind": "source", "bytes": 6, "sha256": "a" * 64}
    with pytest.raises(ValueError, match="source"):
        await service.upload(
            profile, invocation, "source", receipt, stream(b"source"), authority_fence(), authorize=allowed
        )
    unscoped = invocation.model_copy(update={"project_id": None})
    with pytest.raises(ValueError, match="project"):
        await service.upload(
            profile, unscoped, "source", receipt, stream(b"source"), authority_fence(), authorize=allowed
        )
