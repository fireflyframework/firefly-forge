# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Scalar publication roots and independently persisted, paged chunk proofs."""

from __future__ import annotations

import copy
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4, uuid5

from pyfly.container import repository
from pyfly.data.relational.sqlalchemy import BaseEntity, Repository
from sqlalchemy import BigInteger, Boolean, ForeignKey, String, UniqueConstraint, select, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from firefly_forge.object_storage import ObjectChunk
from firefly_forge.runner_object_artifacts import RunnerObjectChunk, RunnerObjectManifest
from firefly_forge.runner_pool_models import FactoryFence, pool_digest

if TYPE_CHECKING:
    from firefly_forge.runner_pools import RunnerPools

PAGE_SIZE = 128
ZERO = "0" * 64


class ForgeRunnerArtifactEntity(BaseEntity):
    __tablename__ = "forge_runner_artifact_publications"
    status: Mapped[str] = mapped_column(String(16))
    upload_owner: Mapped[UUID] = mapped_column()
    identity: Mapped[dict[str, Any]] = mapped_column(JSONB)
    data: Mapped[dict[str, Any]] = mapped_column(JSONB)


class ForgeRunnerArtifactChunkEntity(BaseEntity):
    __tablename__ = "forge_runner_artifact_chunks"
    __table_args__ = (UniqueConstraint("publication_id", "sequence"),)
    publication_id: Mapped[UUID] = mapped_column(ForeignKey("forge_runner_artifact_publications.id"))
    sequence: Mapped[int] = mapped_column(BigInteger)
    confirmed: Mapped[bool] = mapped_column(Boolean)
    envelope: Mapped[dict[str, Any]] = mapped_column(JSONB)


@repository
class ForgeRunnerArtifactRepository(Repository[ForgeRunnerArtifactEntity, UUID]):
    async def locked(self, key: str) -> ForgeRunnerArtifactEntity | None:
        session = self._require_session()
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"), {"key": "forge:artifact-publication:" + key}
        )
        return await session.scalar(
            select(ForgeRunnerArtifactEntity).where(ForgeRunnerArtifactEntity.id == UUID(key)).with_for_update()
        )


@repository
class ForgeRunnerArtifactChunkRepository(Repository[ForgeRunnerArtifactChunkEntity, UUID]):
    async def sequence(self, publication: UUID, sequence: int) -> ForgeRunnerArtifactChunkEntity | None:
        return await self._require_session().scalar(
            select(ForgeRunnerArtifactChunkEntity)
            .where(
                ForgeRunnerArtifactChunkEntity.publication_id == publication,
                ForgeRunnerArtifactChunkEntity.sequence == sequence,
            )
            .with_for_update()
        )

    async def after(self, publication: UUID, after: int) -> list[ForgeRunnerArtifactChunkEntity]:
        return list(
            await self._require_session().scalars(
                select(ForgeRunnerArtifactChunkEntity)
                .where(
                    ForgeRunnerArtifactChunkEntity.publication_id == publication,
                    ForgeRunnerArtifactChunkEntity.sequence > after,
                )
                .order_by(ForgeRunnerArtifactChunkEntity.sequence)
                .limit(PAGE_SIZE)
            )
        )


class RunnerArtifactPublications:
    def __init__(
        self,
        pools: RunnerPools,
        records: ForgeRunnerArtifactRepository,
        chunks: ForgeRunnerArtifactChunkRepository | None = None,
    ) -> None:
        self.pools, self.records = pools, records
        self.chunk_records = chunks or ForgeRunnerArtifactChunkRepository()

    @asynccontextmanager
    async def _unit(self) -> AsyncIterator[None]:
        if self.pools.sessions.current() is not None:
            raise RuntimeError("Artifact publication requires an independent committed native unit")
        async with self.pools._unit():
            yield

    @staticmethod
    def _same(row: ForgeRunnerArtifactEntity | None, identity: dict[str, Any]) -> ForgeRunnerArtifactEntity:
        if row is None or row.identity != identity:
            raise ValueError("Artifact publication differs from its tenant, project, invocation or storage binding")
        return row

    @staticmethod
    def _owner(row: ForgeRunnerArtifactEntity, owner: str, fence: FactoryFence) -> None:
        if (
            row.status != "pending"
            or row.upload_owner.hex != owner
            or row.data["submission_fence"] != fence.model_dump(mode="json")
        ):
            raise RuntimeError("Artifact upload ownership changed; reconciliation required")

    @staticmethod
    def _manifest_matches(identity: dict[str, Any], manifest: RunnerObjectManifest) -> bool:
        return bool(
            manifest.sha256 == identity["receipt"]["sha256"]
            and manifest.bytes == identity["receipt"]["bytes"]
            and manifest.scope.model_dump(mode="json") == identity["scope"]
            and manifest.binding == identity["storage_binding"]
            and manifest.chunk_count == identity["expected_chunks"]
        )

    async def begin(self, identity: dict[str, Any], fence: FactoryFence) -> tuple[str, dict[str, Any] | None]:
        async with self._unit():
            await self.pools._owned(fence)
            row = await self.records.locked(identity["key"])
            if row is not None:
                self._same(row, identity)
                if row.status != "published":
                    raise RuntimeError("Prior artifact upload requires readback; no automatic replay")
                return row.upload_owner.hex, copy.deepcopy(row.data["manifest"])
            owner = uuid4()
            await self.records.save(
                ForgeRunnerArtifactEntity(
                    id=UUID(identity["key"]),
                    status="pending",
                    upload_owner=owner,
                    identity=copy.deepcopy(identity),
                    data={
                        "submission_fence": fence.model_dump(mode="json"),
                        "intent_count": 0,
                        "intent_bytes": 0,
                        "confirmed_count": 0,
                        "confirmed_bytes": 0,
                        "intent_head": ZERO,
                        "confirmed_head": ZERO,
                    },
                )
            )
            await self.records._require_session().flush()
            await self.pools._owned(fence)
            return owner.hex, None

    async def progress(
        self, identity: dict[str, Any], owner: str, chunk: ObjectChunk, confirmed: bool, fence: FactoryFence
    ) -> None:
        async with self._unit():
            await self.pools._owned(fence)
            row = self._same(await self.records.locked(identity["key"]), identity)
            self._owner(row, owner, fence)
            data = copy.deepcopy(row.data)
            if confirmed:
                recorded = await self.chunk_records.sequence(row.id, data["intent_count"])
                if (
                    recorded is None
                    or recorded.confirmed
                    or data["intent_count"] != data["confirmed_count"] + 1
                    or recorded.envelope
                    != RunnerObjectChunk.capture(data["intent_count"], data["confirmed_head"], chunk).model_dump(
                        mode="json"
                    )
                ):
                    raise ValueError("Artifact confirmation differs from its exact recorded upload intent")
                recorded.confirmed = True
                await self.chunk_records.save(recorded)
                data.update(
                    confirmed_count=data["intent_count"],
                    confirmed_bytes=data["intent_bytes"],
                    confirmed_head=data["intent_head"],
                )
            else:
                position = data["intent_count"] + 1
                expected_bytes = min(identity["chunk_bytes"], identity["receipt"]["bytes"] - data["intent_bytes"])
                if (
                    data["intent_count"] != data["confirmed_count"]
                    or position > identity["expected_chunks"]
                    or chunk.bytes != expected_bytes
                    or expected_bytes <= 0
                    or chunk.scope.model_dump(mode="json") != identity["scope"]
                    or chunk.binding != identity["storage_binding"]
                    or await self.chunk_records.sequence(row.id, position) is not None
                ):
                    raise RuntimeError("An uncertain or foreign chunk cannot acquire another upload intent")
                envelope = RunnerObjectChunk.capture(position, data["confirmed_head"], chunk)
                await self.chunk_records.save(
                    ForgeRunnerArtifactChunkEntity(
                        id=uuid5(row.id, "chunk:" + str(position)),
                        publication_id=row.id,
                        sequence=position,
                        confirmed=False,
                        envelope=envelope.model_dump(mode="json"),
                    )
                )
                data.update(
                    intent_count=position, intent_bytes=data["intent_bytes"] + chunk.bytes, intent_head=envelope.hash
                )
            row.data = data
            await self.records.save(row)
            await self.pools._owned(fence)

    async def publish(
        self, identity: dict[str, Any], owner: str, manifest: RunnerObjectManifest, fence: FactoryFence
    ) -> None:
        async with self._unit():
            await self.pools._owned(fence)
            row = self._same(await self.records.locked(identity["key"]), identity)
            self._owner(row, owner, fence)
            if (
                not self._manifest_matches(identity, manifest)
                or row.data["intent_count"] != row.data["confirmed_count"]
                or row.data["confirmed_count"] != manifest.chunk_count
                or row.data["confirmed_bytes"] != manifest.bytes
                or row.data["confirmed_head"] != manifest.chain_sha256
            ):
                raise RuntimeError("Artifact publication CAS differs from its confirmed immutable upload")
            row.data = {**row.data, "manifest": manifest.model_dump(mode="json")}
            row.status = "published"
            await self.records.save(row)
            await self.pools._owned(fence)

    async def load(self, identity: dict[str, Any]) -> dict[str, Any]:
        async with self._unit():
            row = self._same(await self.records.locked(identity["key"]), identity)
            if row.status != "published":
                raise RuntimeError("Artifact publication outcome is unknown; readback is required")
            return copy.deepcopy(row.data["manifest"])

    @staticmethod
    def _observation(row: ForgeRunnerArtifactEntity) -> dict[str, Any]:
        value = {"status": row.status, "owner": row.upload_owner.hex, "identity": row.identity, "data": row.data}
        return {**copy.deepcopy(value), "digest": pool_digest(value)}

    async def pending(self, identity: dict[str, Any], fence: FactoryFence) -> dict[str, Any]:
        async with self._unit():
            await self.pools._owned(fence)
            row = self._same(await self.records.locked(identity["key"]), identity)
            if row.status not in {"pending", "published"}:
                raise RuntimeError("Artifact state does not permit explicit readback")
            return self._observation(row)

    async def chunks(
        self, identity: dict[str, Any], observation: dict[str, Any] | None = None
    ) -> AsyncIterator[RunnerObjectChunk]:
        cursor = 0
        while True:
            async with self._unit():
                root = self._same(await self.records.locked(identity["key"]), identity)
                if observation is None:
                    if root.status != "published":
                        raise RuntimeError("Chunk ledger is not published")
                elif self._observation(root) != observation:
                    raise RuntimeError("Artifact state changed during readback")
                rows = await self.chunk_records.after(root.id, cursor)
                page = []
                for row in rows:
                    envelope = RunnerObjectChunk.model_validate(row.envelope)
                    if envelope.sequence != row.sequence or (
                        observation is None and not row.confirmed and "readback" not in root.data
                    ):
                        raise ValueError("Artifact chunk record differs from its publication proof")
                    page.append(envelope)
            if not page:
                return
            for envelope in page:
                yield envelope
            cursor = page[-1].sequence

    async def publish_readback(
        self, identity: dict[str, Any], observation: dict[str, Any], manifest: RunnerObjectManifest, fence: FactoryFence
    ) -> None:
        async with self._unit():
            await self.pools._owned(fence)
            row = self._same(await self.records.locked(identity["key"]), identity)
            if (
                row.status != "pending"
                or self._observation(row) != observation
                or not self._manifest_matches(identity, manifest)
                or row.data["intent_count"] != manifest.chunk_count
                or row.data["intent_bytes"] != manifest.bytes
                or row.data["intent_head"] != manifest.chain_sha256
            ):
                raise RuntimeError("Artifact state changed after explicit readback; no publication")
            row.data = {
                **row.data,
                "manifest": manifest.model_dump(mode="json"),
                "readback": {"prior_state_sha256": observation["digest"], "fence": fence.model_dump(mode="json")},
            }
            row.status = "published"
            await self.records.save(row)
            await self.pools._owned(fence)
