# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Native durable corpus, inventory, part, operation and append-only event records."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import UUID

from pyfly.container import repository
from pyfly.data.relational.sqlalchemy import BaseEntity, Repository
from sqlalchemy import BigInteger, Index, Select, String, UniqueConstraint, case, cast, func, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

if TYPE_CHECKING:
    from firefly_forge.persistence import ForgeRunEntity


class FrameworkKnowledgeRecord(BaseEntity):
    __tablename__ = "forge_framework_knowledge_records"
    __table_args__ = (
        UniqueConstraint("kind", "key"),
        Index("ix_framework_knowledge_page", "kind", "parent", "key"),
        Index("ix_framework_knowledge_state", "kind", "framework"),
    )
    kind: Mapped[str] = mapped_column(String(16))
    key: Mapped[str] = mapped_column(String(64))
    parent: Mapped[str] = mapped_column(String(64), default="")
    framework: Mapped[str] = mapped_column(String(16))
    source_ref: Mapped[str] = mapped_column(String(32), default="", index=True)
    revision: Mapped[int] = mapped_column(default=1)
    data: Mapped[dict[str, Any]] = mapped_column(JSONB)


@repository
class FrameworkKnowledgeRecords(Repository[FrameworkKnowledgeRecord, UUID]):
    async def by_key(self, kind: str, key: str, *, lock: bool = False) -> FrameworkKnowledgeRecord | None:
        query = select(FrameworkKnowledgeRecord).where(
            FrameworkKnowledgeRecord.kind == kind, FrameworkKnowledgeRecord.key == key
        )
        if lock:
            query = query.with_for_update()
        return await self._require_session().scalar(query)

    @staticmethod
    def page_query(kind: str, parent: str, cursor: str | None, size: int) -> Select[FrameworkKnowledgeRecord]:
        query = select(FrameworkKnowledgeRecord).where(
            FrameworkKnowledgeRecord.kind == kind, FrameworkKnowledgeRecord.parent == parent
        )
        if cursor is not None:
            query = query.where(FrameworkKnowledgeRecord.key > cursor)
        return query.order_by(FrameworkKnowledgeRecord.key).limit(size)

    async def page(self, kind: str, parent: str, cursor: str | None, size: int) -> list[FrameworkKnowledgeRecord]:
        return list(await self._require_session().scalars(self.page_query(kind, parent, cursor, size)))

    async def total(self, kind: str, parent: str) -> int:
        value = await self._require_session().scalar(
            select(func.count())
            .select_from(FrameworkKnowledgeRecord)
            .where(FrameworkKnowledgeRecord.kind == kind, FrameworkKnowledgeRecord.parent == parent)
        )
        return int(value or 0)

    async def count_state(self, kind: str, parent: str, state: str) -> int:
        value = await self._require_session().scalar(
            select(func.count())
            .select_from(FrameworkKnowledgeRecord)
            .where(
                FrameworkKnowledgeRecord.kind == kind,
                FrameworkKnowledgeRecord.parent == parent,
                FrameworkKnowledgeRecord.data["state"].astext == state,
            )
        )
        return int(value or 0)

    async def progress_counts(self, corpus_ref: str) -> dict[str, int]:
        record = FrameworkKnowledgeRecord
        public = record.data["public"]
        admitted = public["state"].astext != "excluded"
        source_query = select(
            func.count().label("total_files"),
            func.count().filter(admitted).label("files"),
            func.count().filter(public["state"].astext == "excluded").label("excluded_files"),
            func.count().filter(admitted, public["kind"].astext == "document").label("documents"),
            func.count().filter(admitted, public["kind"].astext == "example").label("examples"),
            func.count().filter(admitted, public["kind"].astext == "api").label("api"),
            func.coalesce(func.sum(case((admitted, cast(public["bytes"].astext, BigInteger)), else_=0)), 0).label(
                "bytes"
            ),
        ).where(record.kind == "source", record.parent == corpus_ref)
        sources = (await self._require_session().execute(source_query)).mappings().one()
        state = record.data["state"].astext
        part_query = select(
            func.count().filter(state != "excluded").label("parts"),
            func.count().filter(state == "ready").label("ready_parts"),
            func.count().filter(state == "excluded").label("excluded_parts"),
        ).where(record.kind == "part", record.parent == corpus_ref)
        parts = (await self._require_session().execute(part_query)).mappings().one()
        return {str(key): int(value or 0) for row in (sources, parts) for key, value in row.items()}

    async def ready_exists(self, framework: str) -> bool:
        query = (
            select(FrameworkKnowledgeRecord.id)
            .where(
                FrameworkKnowledgeRecord.kind == "corpus",
                FrameworkKnowledgeRecord.framework == framework,
                FrameworkKnowledgeRecord.data["public"]["state"].astext == "ready",
            )
            .limit(1)
        )
        return await self._require_session().scalar(query) is not None

    async def ready_version(self, framework: str, version: str | None) -> list[FrameworkKnowledgeRecord]:
        return list(
            await self._require_session().scalars(
                select(FrameworkKnowledgeRecord).where(
                    FrameworkKnowledgeRecord.kind == "corpus",
                    FrameworkKnowledgeRecord.framework == framework,
                    FrameworkKnowledgeRecord.data["public"]["framework_version"].astext == version,
                    FrameworkKnowledgeRecord.data["public"]["state"].astext == "ready",
                )
            )
        )

    async def remote_source(self, corpus_ref: str, source_id: str) -> list[FrameworkKnowledgeRecord]:
        return list(
            await self._require_session().scalars(
                select(FrameworkKnowledgeRecord).where(
                    FrameworkKnowledgeRecord.kind == "part",
                    FrameworkKnowledgeRecord.parent == corpus_ref,
                    FrameworkKnowledgeRecord.data["source_id"].astext == source_id,
                )
            )
        )

    async def part_counts(self, source_ref: str) -> dict[str, int]:
        state = FrameworkKnowledgeRecord.data["state"].astext
        rows = await self._require_session().execute(
            select(state, func.count())
            .where(FrameworkKnowledgeRecord.kind == "part", FrameworkKnowledgeRecord.source_ref == source_ref)
            .group_by(state)
        )
        result: dict[str, int] = {}
        for key, count in rows:
            if not isinstance(key, str) or not isinstance(count, int):
                raise ValueError("Native reference part state count is invalid")
            result[key] = count
        return result

    async def pin_event(self, run_id: str, key: str) -> Any:
        from firefly_forge.persistence import ForgeJournalEntity

        return await self._require_session().scalar(
            select(ForgeJournalEntity).where(
                ForgeJournalEntity.run_id == UUID(hex=run_id),
                ForgeJournalEntity.kind == "framework_knowledge_pin",
                ForgeJournalEntity.idempotency_key == key,
            )
        )

    @staticmethod
    def generation_runs_query(framework: str, corpus_ref: str, cursor: str | None, size: int) -> Select[Any]:
        from firefly_forge.persistence import ForgeRunEntity

        pin = ForgeRunEntity.data["framework_knowledge_pin"]
        query = select(ForgeRunEntity).where(
            ForgeRunEntity.status == "succeeded",
            pin["framework"].astext == framework,
            pin["corpus_ref"].astext == corpus_ref,
        )
        if cursor is not None:
            query = query.where(ForgeRunEntity.id > UUID(hex=cursor))
        return query.order_by(ForgeRunEntity.id).limit(size)

    async def generation_runs(
        self, framework: str, corpus_ref: str, cursor: str | None, size: int
    ) -> list[ForgeRunEntity]:
        return list(
            await self._require_session().scalars(self.generation_runs_query(framework, corpus_ref, cursor, size))
        )

    async def pending_operations(self, cursor: str | None, size: int) -> list[FrameworkKnowledgeRecord]:
        query = select(FrameworkKnowledgeRecord).where(
            FrameworkKnowledgeRecord.kind == "operation",
            FrameworkKnowledgeRecord.data["public"]["state"].astext.in_(["queued", "running"]),
        )
        if cursor is not None:
            query = query.where(FrameworkKnowledgeRecord.key > cursor)
        return list(await self._require_session().scalars(query.order_by(FrameworkKnowledgeRecord.key).limit(size)))

    async def kind_parts(
        self, corpus_ref: str, kind: str, cursor: str | None, size: int
    ) -> list[FrameworkKnowledgeRecord]:
        from sqlalchemy.orm import aliased

        source = aliased(FrameworkKnowledgeRecord)
        query = (
            select(FrameworkKnowledgeRecord)
            .join(source, source.key == FrameworkKnowledgeRecord.source_ref)
            .where(
                FrameworkKnowledgeRecord.kind == "part",
                FrameworkKnowledgeRecord.parent == corpus_ref,
                FrameworkKnowledgeRecord.data["state"].astext == "ready",
                source.kind == "source",
                source.parent == corpus_ref,
                source.data["public"]["kind"].astext == kind,
                source.data["public"]["state"].astext == "ready",
            )
        )
        if cursor is not None:
            query = query.where(FrameworkKnowledgeRecord.key > cursor)
        return list(await self._require_session().scalars(query.order_by(FrameworkKnowledgeRecord.key).limit(size)))
