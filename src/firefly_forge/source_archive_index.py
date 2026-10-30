# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Native publication of bounded catalog pages and immutable source lineage."""

from __future__ import annotations

import copy
import hashlib
import re
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from pyfly.container import repository
from pyfly.data.relational.sqlalchemy import BaseEntity, Repository
from sqlalchemy import BigInteger, ForeignKey, UniqueConstraint, select, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from firefly_forge.runner_pool_models import FactoryFence, pool_digest
from firefly_forge.runner_pools import RunnerPools
from firefly_forge.source_archive import public_entry
from firefly_forge.source_catalog import SourceCatalogPageV1, SourceCatalogRefV1, _json


class SourceArchiveRefV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, hide_input_in_errors=True)
    protocol: Literal["forge-source-archive-v1"] = "forge-source-archive-v1"
    snapshot_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    tenant_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    project_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    repository_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    run_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    attempt: int = Field(ge=0)
    head: str = Field(pattern=r"^[a-f0-9]{40,64}$")
    tree: str = Field(pattern=r"^[a-f0-9]{40,64}$")
    branch: str | None = Field(default=None, max_length=1024)
    catalog: SourceCatalogRefV1
    storage_binding: str = Field(pattern=r"^[a-f0-9]{64}$")

    def content_binding(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "project_id": self.project_id,
            "source_digest": self.catalog.source_digest,
        }

    def identity(self) -> dict[str, Any]:
        return {**self.model_dump(mode="json", exclude={"catalog"}), "source_digest": self.catalog.source_digest}


class ForgeSourceArchiveEntity(BaseEntity):
    __tablename__ = "forge_source_archives"
    data: Mapped[dict[str, Any]] = mapped_column(JSONB)


class ForgeSourceArchivePageEntity(BaseEntity):
    __tablename__ = "forge_source_archive_pages"
    __table_args__ = (UniqueConstraint("snapshot_id", "page"),)
    snapshot_id: Mapped[UUID] = mapped_column(ForeignKey("forge_source_archives.id"))
    page: Mapped[int] = mapped_column(BigInteger)
    data: Mapped[dict[str, Any]] = mapped_column(JSONB)


@repository
class ForgeSourceArchiveRepository(Repository[ForgeSourceArchiveEntity, UUID]):
    async def locked(self, value: str) -> ForgeSourceArchiveEntity | None:
        session = self._require_session()
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"), {"key": "forge:source-archive:" + value}
        )
        return await session.scalar(
            select(ForgeSourceArchiveEntity).where(ForgeSourceArchiveEntity.id == UUID(value)).with_for_update()
        )

    async def for_run(self, run_id: str, after: str | None) -> list[ForgeSourceArchiveEntity]:
        statement = select(ForgeSourceArchiveEntity).where(
            ForgeSourceArchiveEntity.data["reference"]["run_id"].astext == run_id
        )
        if after is not None:
            statement = statement.where(ForgeSourceArchiveEntity.id > UUID(after))
        return list(await self._require_session().scalars(statement.order_by(ForgeSourceArchiveEntity.id).limit(129)))


@repository
class ForgeSourceArchivePageRepository(Repository[ForgeSourceArchivePageEntity, UUID]):
    async def at(self, value: UUID, page: int) -> ForgeSourceArchivePageEntity | None:
        return await self._require_session().scalar(
            select(ForgeSourceArchivePageEntity).where(
                ForgeSourceArchivePageEntity.snapshot_id == value, ForgeSourceArchivePageEntity.page == page
            )
        )


class SourceArchiveIndex:
    def __init__(
        self, pools: RunnerPools, roots: ForgeSourceArchiveRepository, pages: ForgeSourceArchivePageRepository
    ) -> None:
        self.pools, self.roots, self.pages = pools, roots, pages

    def _unit(self) -> AbstractAsyncContextManager[AsyncSession]:
        if self.pools.sessions.current() is not None:
            raise RuntimeError("Source publication requires an independent committed native unit")
        return self.pools._unit()

    async def begin(self, reference: SourceArchiveRefV1, fence: FactoryFence) -> SourceArchiveRefV1 | None:
        async with self._unit():
            await self.pools._owned(fence)
            row = await self.roots.locked(reference.snapshot_id)
            if row is not None:
                prior = SourceArchiveRefV1.model_validate(row.data["reference"])
                if prior.identity() != reference.identity():
                    raise ValueError("Source publication identity changed")
                if row.data["status"] != "published":
                    raise RuntimeError("Prior source publication needs reconciliation; no automatic retry")
                return prior
            await self.roots.save(
                ForgeSourceArchiveEntity(
                    id=UUID(reference.snapshot_id),
                    data={
                        "reference": reference.model_dump(mode="json"),
                        "status": "pending",
                        "page_count": 0,
                        "page_head": "0" * 64,
                        "fence": fence.model_dump(mode="json"),
                        "reason": None,
                    },
                )
            )
        return None

    async def put(
        self,
        reference: SourceArchiveRefV1,
        page: SourceCatalogPageV1,
        content: list[dict[str, Any] | None],
        fence: FactoryFence,
    ) -> None:
        if (
            page.artifact_id != reference.catalog.artifact_id
            or page.manifest_sha256 != reference.catalog.manifest_sha256
            or len(page.entries) > 128
            or len(page.entries) != len(content)
            or page.page >= reference.catalog.page_count
        ):
            raise ValueError("Source catalog page differs from its bounded publication")
        async with self._unit():
            await self.pools._owned(fence)
            row = await self.roots.locked(reference.snapshot_id)
            if (
                row is None
                or row.data["status"] != "pending"
                or row.data["reference"] != reference.model_dump(mode="json")
                or row.data["fence"] != fence.model_dump(mode="json")
                or row.data["page_count"] != page.page
            ):
                raise ValueError("Source publication owner, catalog or sequence changed")
            value = {"catalog": page.model_dump(mode="json"), "content": content, "previous": row.data["page_head"]}
            digest = pool_digest(value)
            await self.pages.save(
                ForgeSourceArchivePageEntity(
                    snapshot_id=UUID(reference.snapshot_id), page=page.page, data={**value, "hash": digest}
                )
            )
            row.data = {**row.data, "page_count": page.page + 1, "page_head": digest}
            await self.roots.save(row)

    async def seal(self, reference: SourceArchiveRefV1, fence: FactoryFence, source_fence: Callable[[], None]) -> None:
        async with self._unit():
            await self.pools._owned(fence)
            row = await self.roots.locked(reference.snapshot_id)
            if (
                row is None
                or row.data["status"] != "pending"
                or row.data["reference"] != reference.model_dump(mode="json")
                or row.data["fence"] != fence.model_dump(mode="json")
                or row.data["page_count"] != reference.catalog.page_count
            ):
                raise ValueError("Incomplete or changed source publication cannot be sealed")
            source_fence()
            row.data = {**row.data, "status": "published"}
            await self.roots.save(row)
            await self.roots._require_session().flush()
            source_fence()
        source_fence()

    async def fail(self, snapshot_id: str, fence: FactoryFence) -> None:
        async with self._unit():
            await self.pools._owned(fence)
            row = await self.roots.locked(snapshot_id)
            if row is not None and row.data["fence"] == fence.model_dump(mode="json"):
                row.data = {
                    **row.data,
                    "status": "failed",
                    "reason": "source_publication_failed_reconciliation_required",
                }
                await self.roots.save(row)

    async def get(self, snapshot_id: str) -> SourceArchiveRefV1:
        async with self._unit():
            row = await self.roots.locked(snapshot_id)
            if row is None or row.data["status"] != "published":
                raise ValueError("Immutable source content is unavailable")
            return SourceArchiveRefV1.model_validate(copy.deepcopy(row.data["reference"]))

    async def page(
        self, reference: SourceArchiveRefV1, page: int
    ) -> tuple[SourceCatalogPageV1, list[dict[str, Any] | None]]:
        if type(page) is not int or page < 0 or page >= reference.catalog.page_count:
            if page == 0 and reference.catalog.entry_count == 0:
                return SourceCatalogPageV1(
                    artifact_id=reference.catalog.artifact_id,
                    manifest_sha256=reference.catalog.manifest_sha256,
                    page=0,
                    next_page=None,
                    entries=[],
                ), []
            raise ValueError("Source catalog page is outside its immutable extent")
        async with self._unit():
            root = await self.roots.locked(reference.snapshot_id)
            if (
                root is None
                or root.data["status"] != "published"
                or root.data["reference"] != reference.model_dump(mode="json")
            ):
                raise ValueError("Published source reference changed")
            head, result = "0" * 64, None
            files_hash, source_hash = hashlib.sha256(b"{"), hashlib.sha256(b'{"files":{')
            previous, entries, files, total = None, 0, 0, 0
            for number in range(reference.catalog.page_count):
                row = await self.pages.at(UUID(reference.snapshot_id), number)
                if row is None:
                    raise ValueError("Published source catalog page is unavailable")
                value = row.data
                if (
                    value["previous"] != head
                    or pool_digest({k: v for k, v in value.items() if k != "hash"}) != value["hash"]
                ):
                    raise ValueError("Published source catalog chain changed")
                catalog = SourceCatalogPageV1.model_validate(value["catalog"])
                if (
                    catalog.page != number
                    or catalog.artifact_id != reference.catalog.artifact_id
                    or catalog.manifest_sha256 != reference.catalog.manifest_sha256
                    or len(catalog.entries) > 128
                    or len(value["content"]) != len(catalog.entries)
                    or catalog.next_page != (number + 1 if number + 1 < reference.catalog.page_count else None)
                ):
                    raise ValueError("Published source catalog scope changed")
                head = value["hash"]
                for entry in catalog.entries:
                    public_entry(entry)
                    if previous is not None and entry.path <= previous:
                        raise ValueError("Source catalog digest ordering changed")
                    payload_hash = entry.sha256 if entry.kind == "file" else ""
                    if payload_hash is None:
                        raise ValueError("Source catalog digest is unavailable")
                    encoded = (
                        (b"," if entries else b"") + _json(entry.path) + b":" + _json(payload_hash + str(entry.mode))
                    )
                    files_hash.update(encoded)
                    source_hash.update(encoded)
                    entries += 1
                    if entry.kind == "file":
                        files += 1
                        total += entry.byte_count
                    previous = entry.path
                if number == page:
                    result = catalog, copy.deepcopy(value["content"])
            files_hash.update(b"}")
            source_hash.update(b'},"protocol":"forge-verification-source-v1"}')
            if (
                files_hash.hexdigest() != reference.catalog.manifest_digest
                or source_hash.hexdigest() != reference.catalog.source_digest
                or (entries, files, total)
                != (reference.catalog.entry_count, reference.catalog.file_count, reference.catalog.byte_count)
            ):
                raise ValueError("Published catalog differs from its immutable source digest")
            if (
                head != root.data["page_head"]
                or root.data["page_count"] != reference.catalog.page_count
                or result is None
            ):
                raise ValueError("Published source catalog root changed")
            return result

    async def revisions(self, run_id: str, after: str | None) -> list[dict[str, Any]]:
        if after is not None and re.fullmatch(r"[a-f0-9]{32}", after) is None:
            raise ValueError("Use an exact recorded revision cursor")
        async with self._unit():
            return [copy.deepcopy(row.data) for row in await self.roots.for_run(run_id, after)]
