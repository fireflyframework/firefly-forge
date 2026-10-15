# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Explicit immutable report archives; reading history never advances execution."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field
from pyfly.container import service
from pyfly.data.relational.sqlalchemy import LockMode
from sqlalchemy import Text, cast, func, select, text

from firefly_forge.campaign_report_history_persistence import (
    ForgeCampaignReportArchiveEntity,
    ForgeCampaignReportArchiveRepository,
)
from firefly_forge.campaign_reports import CampaignReport, CampaignReports, ReportState
from firefly_forge.identity_persistence import ForgeIdentityUserRepository
from firefly_forge.journal import digest
from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied, Permission
from firefly_forge.store import Store, identifier

Data = dict[str, Any]
MAX_ARCHIVES, PAGE_SIZE, MAX_ARCHIVE_BYTES = 100, 50, 16_777_216


class CampaignReportArchiveInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    request_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    expected_report_digest: str = Field(pattern=r"^[a-f0-9]{64}$")


class ArchiveSource(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    campaign_journal_head: str = Field(pattern=r"^[a-f0-9]{64}$")
    campaign_event_count: int = Field(ge=1, le=1024)
    project_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    request_digest: str = Field(pattern=r"^[a-f0-9]{64}$")


class ArchiveIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    version: Literal[1] = 1
    project_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    campaign_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    request_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    actor_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    sequence: int = Field(ge=1, le=100)
    prepared_at: str = Field(min_length=1, max_length=64)
    source: ArchiveSource
    repositories: list[str] = Field(min_length=1, max_length=200)
    digest: str = Field(pattern=r"^[a-f0-9]{64}$")


class CampaignReportArchiveReceipt(ArchiveIdentity):
    request_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    report: CampaignReport


class CampaignReportArchiveSummary(ArchiveIdentity):
    state: ReportState
    report_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    receipt_digest: str = Field(pattern=r"^[a-f0-9]{64}$")


class CampaignReportArchivePage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    project_id: str
    campaign_id: str
    items: list[CampaignReportArchiveSummary]
    next_after: int | None


def _source(report: Data) -> Data:
    campaign = report["campaign"]
    return ArchiveSource(
        campaign_journal_head=campaign["journal_head"],
        campaign_event_count=campaign["event_count"],
        project_digest=campaign["project_digest"],
        request_digest=campaign["request_digest"],
    ).model_dump()


def _signed(data: Data) -> Data:
    return {**data, "digest": digest({key: value for key, value in data.items() if key != "digest"})}


def _summary(data: Data) -> Data:
    return _signed(
        {
            **{key: data[key] for key in ArchiveIdentity.model_fields if key != "digest"},
            "state": data["report"]["state"],
            "report_digest": data["report"]["digest"],
            "receipt_digest": data["digest"],
        }
    )


def _valid_identity(data: Data) -> None:
    if (
        data != _signed(data)
        or datetime.fromisoformat(data["prepared_at"]).utcoffset() is None
        or data["repositories"] != sorted({identifier(repo).hex for repo in data["repositories"]})
    ):
        raise ValueError("Campaign report archive integrity check failed")


@service
class CampaignReportHistory:
    def __init__(self, store: Store, reports: CampaignReports, records: ForgeCampaignReportArchiveRepository) -> None:
        self.store, self.reports, self.records = store, reports, records

    async def _authorize(self, project_id: str, repositories: set[str], permission: Permission, actor: str) -> None:
        principal = CURRENT_PRINCIPAL.get()
        if principal is None or principal.id.hex != actor:
            raise AccessDenied("Campaign report history requires the current authenticated actor")
        await self.reports.access.require_project(principal, project_id, permission)
        for repo in sorted(repositories):
            await self.reports.access.require_repository(principal, repo, "read")

    @staticmethod
    def _actor() -> str:
        principal = CURRENT_PRINCIPAL.get()
        if principal is None:
            raise AccessDenied("Campaign report history requires authentication")
        return principal.id.hex

    async def _lock_actor(self, actor: str) -> None:
        # Membership changes and suspension lock this same native identity row.
        await ForgeIdentityUserRepository().find_by_id(identifier(actor), lock=LockMode.PESSIMISTIC_WRITE)

    def _view(self, row: ForgeCampaignReportArchiveEntity, project_id: str, campaign_id: str) -> Data:
        try:
            data = CampaignReportArchiveReceipt.model_validate(row.data).model_dump(mode="json")
            _valid_identity(data)
            report = data["report"]
            request = CampaignReportArchiveInput(request_id=data["request_id"], expected_report_digest=report["digest"])
            if (
                row.project_id.hex != project_id
                or row.campaign_id.hex != campaign_id
                or data["project_id"] != project_id
                or data["campaign_id"] != campaign_id
                or row.request_id != data["request_id"]
                or row.sequence != data["sequence"]
                or report["project_id"] != project_id
                or report["campaign"]["id"] != campaign_id
                or report["counts"] is None
                or report != _signed(report)
                or data["source"] != _source(report)
                or data["request_digest"] != digest(request.model_dump())
                or row.summary != _summary(data)
            ):
                raise ValueError("Inconsistent archive")
            return data
        except (ValueError, KeyError, TypeError):
            raise ValueError("Campaign report archive integrity check failed") from None

    async def _row(self, project_id: str, campaign_id: str, request_id: str) -> ForgeCampaignReportArchiveEntity | None:
        entity = ForgeCampaignReportArchiveEntity
        async with self.store._unit() as session:
            size = await session.scalar(
                select(func.octet_length(cast(entity.data, Text))).where(
                    entity.campaign_id == identifier(campaign_id), entity.request_id == request_id
                )
            )
            if size is not None and size > MAX_ARCHIVE_BYTES:
                raise ValueError("Campaign report archive exceeds its byte bound")
            row = await self.records.request(identifier(campaign_id), request_id)
            if row is not None and row.project_id.hex != project_id:
                raise KeyError("Campaign report archive not found")
            return row

    async def _campaign(self, project_id: str, campaign_id: str) -> None:
        async with self.store._unit():
            row = await self.reports.campaigns.records.find_by_id(identifier(campaign_id))
            if row is None or row.project_id.hex != project_id:
                raise KeyError("Campaign not found")

    async def get(self, project_id: str, campaign_id: str, request_id: str) -> Data:
        return await self._get(project_id, campaign_id, request_id, "read")

    async def _get(self, project_id: str, campaign_id: str, request_id: str, permission: Permission) -> Data:
        project_id, campaign_id, request_id = (identifier(value).hex for value in (project_id, campaign_id, request_id))
        actor = self._actor()
        await self._authorize(project_id, set(), permission, actor)
        row = await self._row(project_id, campaign_id, request_id)
        if row is None:
            raise KeyError("Campaign report archive not found")
        data = self._view(row, project_id, campaign_id)
        repositories = set(data["repositories"])
        await self._authorize(project_id, repositories, permission, actor)
        await self.store.admit_credentials(data)
        async with self.store._unit():
            await self._lock_actor(actor)
            await self._authorize(project_id, repositories, permission, actor)
            self.store.reject_credentials(data)
        return data

    async def archive(self, project_id: str, campaign_id: str, body: CampaignReportArchiveInput) -> Data:
        project_id, campaign_id = identifier(project_id).hex, identifier(campaign_id).hex
        actor = self._actor()
        await self._authorize(project_id, set(), "execute", actor)
        await self.store.admit_credentials(body.model_dump())
        previous = await self._row(project_id, campaign_id, body.request_id)
        if previous is not None:
            data = self._view(previous, project_id, campaign_id)
            self._same(data, actor, body)
            await self._authorize(project_id, set(data["repositories"]), "execute", actor)
            return await self._get(project_id, campaign_id, body.request_id, "execute")
        report, repositories = await self.reports._capture(project_id, campaign_id)
        if report["digest"] != body.expected_report_digest or report["counts"] is None:
            raise ValueError("Archive requires the selected coherent report digest; read the report again")
        source = _source(report)
        await self._authorize(project_id, repositories, "execute", actor)
        candidate = {
            "version": 1,
            "project_id": project_id,
            "campaign_id": campaign_id,
            "request_id": body.request_id,
            "request_digest": digest(body.model_dump()),
            "actor_id": actor,
            "source": source,
            "repositories": sorted(repositories),
            "report": report,
        }
        async with self.store._unit() as session:
            count = await session.scalar(
                select(func.count())
                .select_from(ForgeCampaignReportArchiveEntity)
                .where(ForgeCampaignReportArchiveEntity.campaign_id == identifier(campaign_id))
            )
            at = await session.scalar(select(func.clock_timestamp()))
        if count is None or count >= MAX_ARCHIVES:
            raise ValueError("Campaign report archive history has reached its bound")
        if not isinstance(at, datetime):
            raise ValueError("Database archive preparation time is unavailable")
        prepared = _signed({**candidate, "sequence": count + 1, "prepared_at": at.isoformat()})
        CampaignReportArchiveReceipt.model_validate(prepared)
        summary = _summary(prepared)
        if len(json.dumps(prepared).encode()) > MAX_ARCHIVE_BYTES:
            raise ValueError("Campaign report archive exceeds its byte bound")
        await self.store.admit_credentials({"receipt": prepared, "summary": summary})
        async with self.store._unit() as session:
            # All archive writers use this order: archive advisory, actor, campaign.
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"),
                {"key": self.store.schema + ":report-archives:" + campaign_id},
            )
            await self._lock_actor(actor)
            await self.reports.campaigns._locked(project_id, campaign_id)
            await self._authorize(project_id, repositories, "execute", actor)
            previous = await self.records.request(identifier(campaign_id), body.request_id)
            if previous is not None:
                data = self._view(previous, project_id, campaign_id)
                self._same(data, actor, body)
            else:
                count = await session.scalar(
                    select(func.count())
                    .select_from(ForgeCampaignReportArchiveEntity)
                    .where(ForgeCampaignReportArchiveEntity.campaign_id == identifier(campaign_id))
                )
                if count is None or count >= MAX_ARCHIVES:
                    raise ValueError("Campaign report archive history has reached its bound")
                if count + 1 != prepared["sequence"]:
                    raise ValueError("Archive history advanced; keep this request identity and read its exact receipt")
                data = prepared
                self.store.reject_credentials({"receipt": data, "summary": summary})
                await self.records.save(
                    ForgeCampaignReportArchiveEntity(
                        id=uuid4(),
                        project_id=identifier(project_id),
                        campaign_id=identifier(campaign_id),
                        request_id=body.request_id,
                        sequence=count + 1,
                        data=data,
                        summary=summary,
                    )
                )
        return await self._get(project_id, campaign_id, body.request_id, "execute")

    @staticmethod
    def _same(data: Data, actor: str, body: CampaignReportArchiveInput) -> None:
        if data["actor_id"] != actor or data["request_digest"] != digest(body.model_dump()):
            raise ValueError("Archive request identity belongs to a different actor or report digest")

    async def list(self, project_id: str, campaign_id: str, after: int = 0) -> Data:
        project_id, campaign_id = identifier(project_id).hex, identifier(campaign_id).hex
        if type(after) is not int or not 0 <= after <= MAX_ARCHIVES:
            raise ValueError("Archive history cursor must be an integer between 0 and 100")
        actor = self._actor()
        await self._authorize(project_id, set(), "read", actor)
        await self._campaign(project_id, campaign_id)
        entity = ForgeCampaignReportArchiveEntity
        async with self.store._unit() as session:
            statement = (
                select(entity.project_id, entity.campaign_id, entity.request_id, entity.sequence, entity.summary)
                .where(
                    entity.project_id == identifier(project_id),
                    entity.campaign_id == identifier(campaign_id),
                    entity.sequence > after,
                )
                .order_by(entity.sequence)
                .limit(PAGE_SIZE + 1)
            )
            sample = statement.subquery()
            size = await session.scalar(
                select(func.coalesce(func.sum(func.octet_length(cast(sample.c.summary, Text))), 0))
            )
            if size is None or size > MAX_ARCHIVE_BYTES:
                raise ValueError("Campaign report archive history exceeds its byte bound")
            rows = (await session.execute(statement)).all()
        checked = []
        repositories: set[str] = set()
        previous = after
        for row in rows:
            item = CampaignReportArchiveSummary.model_validate(row.summary).model_dump(mode="json")
            _valid_identity(item)
            if (
                row.project_id.hex != project_id
                or row.campaign_id.hex != campaign_id
                or item["project_id"] != project_id
                or item["campaign_id"] != campaign_id
                or item["request_id"] != row.request_id
                or item["sequence"] != row.sequence
                or not previous < row.sequence <= MAX_ARCHIVES
            ):
                raise ValueError("Campaign report archive integrity check failed")
            previous = row.sequence
            repositories.update(item["repositories"])
            checked.append(item)
        items = checked[:PAGE_SIZE]
        result = {
            "project_id": project_id,
            "campaign_id": campaign_id,
            "items": items,
            "next_after": items[-1]["sequence"] if len(rows) > PAGE_SIZE else None,
        }
        await self._authorize(project_id, repositories, "read", actor)
        await self.store.admit_credentials({"page": result, "sentinel": checked[PAGE_SIZE:]})
        async with self.store._unit():
            await self._lock_actor(actor)
            await self._authorize(project_id, repositories, "read", actor)
            self.store.reject_credentials(result)
        return result
