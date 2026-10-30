# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Current-authorized observations of published source; never a live filesystem API."""

from __future__ import annotations

import base64
import copy
import hashlib
import re
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from pyfly.container import rest_controller, service
from pyfly.web import PathVar, QueryParam, get_mapping, request_mapping
from starlette.responses import JSONResponse, Response

from firefly_forge.git_deliveries import GitDeliveries
from firefly_forge.identity import Principal
from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied, ProjectAccess
from firefly_forge.secrets import contains_known_credential
from firefly_forge.source_archive import public_entry
from firefly_forge.source_archive_index import SourceArchiveRefV1
from firefly_forge.source_archive_service import SourceArchiveManager
from firefly_forge.source_catalog import SourceCatalogEntryV1, SourceCatalogPageV1
from firefly_forge.store import identifier


class SourceCatalogObservationV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    reference: SourceArchiveRefV1
    catalog: SourceCatalogPageV1


class SourceFilePageV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    reference: SourceArchiveRefV1
    entry: SourceCatalogEntryV1
    catalog_page: int = Field(ge=0)
    entry_index: int = Field(ge=0)
    offset: int = Field(ge=0)
    byte_count: int = Field(ge=0, le=1048576)
    next_offset: int | None
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    content_base64: str = Field(max_length=1398104)
    binary: bool
    text: str | None = Field(max_length=1048576)


@service
class SourceExplorer:
    def __init__(self, manager: SourceArchiveManager, deliveries: GitDeliveries) -> None:
        self.manager, self.deliveries = manager, deliveries

    async def _run(self, run_id: str, reader: Principal) -> dict[str, Any]:
        if CURRENT_PRINCIPAL.get() != reader:
            raise AccessDenied("Source observations require the same current native reader")
        run = await self.deliveries.store.get_run(identifier(run_id).hex)
        await self.deliveries._authorize(run, ProjectAccess.actor_snapshot(reader), "read")
        if CURRENT_PRINCIPAL.get() != reader:
            raise AccessDenied("The current source reader changed")
        return {
            k: copy.deepcopy(run.get(k))
            for k in ("id", "project_id", "repository_id", "component_id", "execution_actor")
        }

    async def _capture(
        self, run_id: str, snapshot_id: str, manifest: str, reader: Principal, request_id: str | None
    ) -> tuple[SourceArchiveRefV1, dict[str, Any], dict[str, Any] | None]:
        if re.fullmatch(r"[a-f0-9]{64}", manifest) is None:
            raise ValueError("An exact source catalog manifest is required")
        run = await self._run(run_id, reader)
        publisher = self.manager.publisher
        if publisher is None:
            raise ValueError("Immutable source content is unavailable; no workspace fallback")
        ref = await publisher.index.get(identifier(snapshot_id).hex)
        if (
            ref.run_id != run_id
            or ref.project_id != run["project_id"]
            or ref.repository_id != run["repository_id"]
            or self.manager.tenant is None
            or ref.tenant_id != self.manager.tenant.hex
            or ref.storage_binding != self.manager.settings.binding
            or ref.catalog.manifest_sha256 != manifest
        ):
            raise ValueError("Source snapshot differs from its authorized run, tenant or manifest")
        checkpoint = None
        if request_id is not None:
            request_id = identifier(request_id).hex
            view = await self.deliveries.view(run_id, actor=ProjectAccess.actor_snapshot(reader))
            matches = [item for item in view["items"] if item["request_id"] == request_id]
            if len(matches) != 1 or matches[0]["source_tree"] != ref.tree:
                raise ValueError("Published source does not match the recorded checkpoint")
            checkpoint = copy.deepcopy(matches[0])
        return ref, run, checkpoint

    def _private(self, result: dict[str, Any]) -> dict[str, Any]:
        publisher = self.manager.publisher
        if publisher and contains_known_credential(result, publisher.content.helper.credential_registry()):
            raise ValueError("Source observation contains configured credentials")
        return result

    async def revisions(self, run_id: str, *, after: str | None = None) -> dict[str, Any]:
        run_id = identifier(run_id).hex
        reader = CURRENT_PRINCIPAL.get()
        if reader is None:
            raise AccessDenied("Source observations require a current native reader")
        before = await self._run(run_id, reader)
        items = []
        rows = await self.manager.publisher.index.revisions(run_id, after) if self.manager.publisher else []
        for row in rows[:128]:
            ref = SourceArchiveRefV1.model_validate(row["reference"])
            if (
                ref.run_id != run_id
                or ref.project_id != before["project_id"]
                or ref.repository_id != before["repository_id"]
                or self.manager.tenant is None
                or ref.tenant_id != self.manager.tenant.hex
            ):
                raise ValueError("Recorded source lineage differs from its current authorized scope")
            items.append(
                {"reference": ref.model_dump(mode="json"), "status": row["status"], "reason": row.get("reason")}
            )
        if await self._run(run_id, reader) != before:
            raise ValueError("Source observation scope changed")
        return self._private(
            {
                "run_id": run_id,
                "items": items,
                "next_after": items[-1]["reference"]["snapshot_id"] if len(rows) > 128 else None,
                "recording_enabled": self.manager.settings.provider != "disabled"
                and self.manager.policy.enabled
                and bool(before["project_id"])
                and UUID(before["project_id"]) not in self.manager.policy.excluded_projects,
                "availability": "recorded" if items else "unavailable",
            }
        )

    async def catalog(
        self, run_id: str, snapshot_id: str, expected_manifest: str, *, page: int = 0, request_id: str | None = None
    ) -> dict[str, Any]:
        reader = CURRENT_PRINCIPAL.get()
        if reader is None:
            raise AccessDenied("Source observations require a current native reader")
        run_id = identifier(run_id).hex
        before = await self._capture(run_id, snapshot_id, expected_manifest, reader, request_id)
        publisher = self.manager.publisher
        assert publisher is not None
        result, _ = await publisher.index.page(before[0], page)
        if await self._capture(run_id, snapshot_id, expected_manifest, reader, request_id) != before:
            raise ValueError("Source observation scope or checkpoint changed")
        return self._private(SourceCatalogObservationV1(reference=before[0], catalog=result).model_dump(mode="json"))

    async def file(
        self,
        run_id: str,
        snapshot_id: str,
        expected_manifest: str,
        *,
        page: int,
        entry_index: int,
        offset: int = 0,
        limit: int = 32768,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        if type(entry_index) is not int or entry_index < 0:
            raise ValueError("An exact catalog entry index is required")
        reader = CURRENT_PRINCIPAL.get()
        if reader is None:
            raise AccessDenied("Source observations require a current native reader")
        run_id = identifier(run_id).hex
        before = await self._capture(run_id, snapshot_id, expected_manifest, reader, request_id)
        publisher = self.manager.publisher
        assert publisher is not None
        catalog, descriptors = await publisher.index.page(before[0], page)
        if entry_index >= len(catalog.entries):
            raise ValueError("Source entry is outside its recorded catalog page")
        entry = catalog.entries[entry_index]
        public_entry(entry)
        descriptor = descriptors[entry_index]
        if entry.kind != "file" or descriptor is None:
            raise ValueError("The recorded entry has no file content")

        async def current() -> None:
            if await self._capture(run_id, snapshot_id, expected_manifest, reader, request_id) != before:
                raise ValueError("Source observation scope or checkpoint changed")

        data = await publisher.content.window(
            before[0].content_binding(), entry, descriptor, offset, limit, authorize=current
        )
        text_value: str | None
        try:
            text_value = data.decode("utf-8", errors="strict")
            if "\0" in text_value:
                text_value = None
        except UnicodeDecodeError:
            text_value = None
        result = SourceFilePageV1(
            reference=before[0],
            entry=entry,
            catalog_page=page,
            entry_index=entry_index,
            offset=offset,
            byte_count=len(data),
            next_offset=offset + len(data) if offset + len(data) < entry.byte_count else None,
            sha256=hashlib.sha256(data).hexdigest(),
            content_base64=base64.b64encode(data).decode("ascii"),
            binary=text_value is None,
            text=text_value,
        )
        await current()
        return self._private(result.model_dump(mode="json"))


@rest_controller
@request_mapping("/api/v1/runs/{run_id}/source")
class SourceExplorerController:
    def __init__(self, explorer: SourceExplorer) -> None:
        self.explorer = explorer

    @get_mapping("/revisions")
    async def revisions(self, run_id: PathVar[str], after: QueryParam[str | None] = None) -> Response:
        return JSONResponse(
            await self.explorer.revisions(run_id, after=after), headers={"Cache-Control": "private, no-store"}
        )

    @get_mapping("/revisions/{snapshot_id}/catalog")
    async def catalog(
        self,
        run_id: PathVar[str],
        snapshot_id: PathVar[str],
        expected_manifest: QueryParam[str],
        page: QueryParam[int] = 0,
        request_id: QueryParam[str | None] = None,
    ) -> Response:
        return JSONResponse(
            await self.explorer.catalog(run_id, snapshot_id, expected_manifest, page=page, request_id=request_id),
            headers={"Cache-Control": "private, no-store"},
        )

    @get_mapping("/revisions/{snapshot_id}/files/{entry_index}")
    async def file(
        self,
        run_id: PathVar[str],
        snapshot_id: PathVar[str],
        entry_index: PathVar[int],
        expected_manifest: QueryParam[str],
        page: QueryParam[int] = 0,
        offset: QueryParam[int] = 0,
        limit: QueryParam[int] = 32768,
        request_id: QueryParam[str | None] = None,
    ) -> Response:
        return JSONResponse(
            await self.explorer.file(
                run_id,
                snapshot_id,
                expected_manifest,
                page=page,
                entry_index=entry_index,
                offset=offset,
                limit=limit,
                request_id=request_id,
            ),
            headers={"Cache-Control": "private, no-store"},
        )
