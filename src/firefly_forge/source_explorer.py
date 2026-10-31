# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Current-authorized observations of published source; never a live filesystem API."""

from __future__ import annotations

import base64
import copy
import hashlib
import re
from contextlib import aclosing
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from pyfly.container import rest_controller, service
from pyfly.web import Body, PathVar, QueryParam, get_mapping, post_mapping, request_mapping
from starlette.responses import JSONResponse, Response

from firefly_forge.git_deliveries import GitDeliveries, delivery_digest
from firefly_forge.git_delivery_models import GitDeliveryInputV1
from firefly_forge.identity import Principal
from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied, ProjectAccess
from firefly_forge.runner_pool_models import pool_digest
from firefly_forge.secrets import contains_known_credential
from firefly_forge.source_archive import public_entry
from firefly_forge.source_archive_index import SourceArchiveRefV1
from firefly_forge.source_archive_scan_models import SourceCredentialScanInputV1, SourceCredentialScanReceiptV1
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
            entries = await self.deliveries._entries(run_id)
            receipts = [item for item in self.deliveries._records(entries) if item["request_id"] == request_id]
            intents = [
                item
                for item in entries
                if item["kind"] == "git_delivery_intent" and item["payload"].get("request_id") == request_id
            ]
            if len(receipts) != 1 or len(intents) != 1:
                raise ValueError("The checkpoint has no unique retained source admission")
            receipt, intent = receipts[0], intents[0]["payload"]
            request = {
                key: intent[key]
                for key in ("request_id", "expected_head", "expected_source_tree", "actor", "expected_source_digest")
                if key in intent
            }
            if request.get("expected_source_digest") is None:
                raise ValueError("The checkpoint has no retained complete source digest")
            admitted = GitDeliveryInputV1.model_validate(
                {"version": 1, **{key: value for key, value in request.items() if key != "actor"}}
            )
            # A Git tree excludes ignored public files; only the admitted catalog-scheme digest binds the cut.
            if (
                admitted.expected_source_digest != ref.catalog.source_digest
                or admitted.expected_head != receipt["parent_revision"]
                or receipt["run_id"] != run_id
                or receipt["source_tree"] != ref.tree
                or receipt["checkpoint_digest"] != matches[0]["checkpoint_digest"]
                or intent.get("request_digest") != delivery_digest(request)
            ):
                raise ValueError("Published source differs from the exact admitted checkpoint source digest")
            checkpoint = {"receipt": copy.deepcopy(matches[0]), "intent_digest": delivery_digest(intent)}
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

    async def _scan_authority(self, run: dict[str, Any], reader: Principal) -> None:
        await ProjectAccess(self.deliveries.store).authorize_actor(
            ProjectAccess.actor_snapshot(reader), run["project_id"], "members"
        )
        if CURRENT_PRINCIPAL.get() != reader:
            raise AccessDenied("The credential scan's current native owner changed")

    async def _scan_commit_authority(self, run: dict[str, Any], reader: Principal) -> None:
        from pyfly.data.relational.sqlalchemy import LockMode
        from sqlalchemy import text

        from firefly_forge.identity_persistence import ForgeIdentityUserRepository
        from firefly_forge.project_persistence import ForgeProjectRepository

        store = self.deliveries.store
        async with store._unit() as session:
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"),
                {"key": store.schema + ":members:" + run["project_id"]},
            )
            user = await ForgeIdentityUserRepository().find_by_id(reader.id, lock=LockMode.PESSIMISTIC_WRITE)
            if (
                (user is None and reader.authentication_method != "local_bootstrap")
                or user is not None
                and not user.enabled
            ):
                raise AccessDenied("The credential scan's native identity is unavailable")
            project = await ForgeProjectRepository().find_by_id(
                identifier(run["project_id"]), lock=LockMode.PESSIMISTIC_WRITE
            )
            if project is None or await self._run(run["id"], reader) != run:
                raise AccessDenied("The credential scan's current native project or run changed")
            await self._scan_authority(run, reader)

    async def scan_candidate(self, run_id: str, snapshot_id: str, expected_manifest: str) -> dict[str, Any]:
        reader = CURRENT_PRINCIPAL.get()
        if reader is None:
            raise AccessDenied("A retained-byte scan requires a current project owner")
        before = await self._capture(identifier(run_id).hex, snapshot_id, expected_manifest, reader, None)
        await self._scan_authority(before[1], reader)
        publisher = self.manager.publisher
        assert publisher is not None
        result = {
            "version": 1,
            "run_id": before[0].run_id,
            "snapshot_id": before[0].snapshot_id,
            "expected_manifest": expected_manifest,
            "expected_credential_fingerprint": publisher.content.credential_fingerprint(),
        }
        if await self._capture(run_id, snapshot_id, expected_manifest, reader, None) != before:
            raise ValueError("Source scan scope changed")
        await self._scan_authority(before[1], reader)
        return self._private(result)

    async def rescan(
        self,
        run_id: str,
        snapshot_id: str,
        request_id: str,
        body: SourceCredentialScanInputV1,
    ) -> dict[str, Any]:
        run_id, request_id = identifier(run_id).hex, identifier(request_id).hex
        reader = CURRENT_PRINCIPAL.get()
        if reader is None:
            raise AccessDenied("A retained-byte scan requires a current project owner")
        before = await self._capture(run_id, snapshot_id, body.expected_manifest, reader, None)
        publisher = self.manager.publisher
        assert publisher is not None
        ref = before[0]

        async def current() -> None:
            if (
                await self._capture(run_id, snapshot_id, body.expected_manifest, reader, None) != before
                or publisher.content.credential_fingerprint() != body.expected_credential_fingerprint
            ):
                raise ValueError("Source scan scope or configured credential registry changed")
            await self._scan_authority(before[1], reader)

        def fence() -> None:
            if (
                CURRENT_PRINCIPAL.get() != reader
                or publisher.content.credential_fingerprint() != body.expected_credential_fingerprint
            ):
                raise ValueError("The credential scan's current identity or credential registry changed")

        async def commit_authorize() -> None:
            await self._scan_commit_authority(before[1], reader)
            fence()

        await current()
        prior = await publisher.index.begin_scan(ref, request_id, body, reader.id.hex, current, commit_authorize, fence)
        if prior is not None:
            await current()
            return self._private(prior.model_dump(mode="json"))
        head, files, size = "0" * 64, 0, 0
        self._private(ref.model_dump(mode="json"))
        async with aclosing(publisher.index.credential_scan_pages(ref, current)) as pages:
            async for catalog, descriptors in pages:
                self._private(catalog.model_dump(mode="json"))
                for entry, descriptor in zip(catalog.entries, descriptors, strict=True):
                    if entry.kind != "file":
                        if descriptor is not None:
                            raise ValueError("A source directory cannot have retained file content")
                        continue
                    if descriptor is None:
                        raise ValueError("Retained source content is unavailable for credential scanning")
                    identity = await publisher.content.scan(ref.content_binding(), entry, descriptor, authorize=current)
                    head = pool_digest({"previous": head, "file": identity})
                    files += 1
                    size += entry.byte_count
        if (files, size) != (ref.catalog.file_count, ref.catalog.byte_count):
            raise ValueError("Incomplete retained source cannot receive a credential proof")
        await current()
        value = {
            "protocol": "forge-source-credential-scan-v1",
            "request_id": request_id,
            "snapshot_id": ref.snapshot_id,
            "tenant_id": ref.tenant_id,
            "project_id": ref.project_id,
            "run_id": run_id,
            "actor_id": reader.id.hex,
            "reference_sha256": pool_digest(ref.model_dump(mode="json")),
            "source_digest": ref.catalog.source_digest,
            "storage_binding": ref.storage_binding,
            "credential_fingerprint": body.expected_credential_fingerprint,
            "request_sha256": pool_digest(body.model_dump(mode="json")),
            "content_chain_sha256": head,
            "file_count": files,
            "byte_count": size,
            "no_execution_grant": True,
        }
        receipt = SourceCredentialScanReceiptV1.model_validate({**value, "digest": pool_digest(value)})
        self._private(receipt.model_dump(mode="json"))
        await publisher.index.complete_scan(ref, body, receipt, current, commit_authorize, fence)
        await current()
        return receipt.model_dump(mode="json")

    async def scan_receipt(
        self, run_id: str, snapshot_id: str, request_id: str, expected_manifest: str
    ) -> dict[str, Any]:
        run_id, request_id = identifier(run_id).hex, identifier(request_id).hex
        reader = CURRENT_PRINCIPAL.get()
        if reader is None:
            raise AccessDenied("Credential scan observations require a current native reader")
        before = await self._capture(run_id, snapshot_id, expected_manifest, reader, None)
        publisher = self.manager.publisher
        assert publisher is not None
        result = await publisher.index.scan_receipt(before[0], request_id)
        if await self._capture(run_id, snapshot_id, expected_manifest, reader, None) != before:
            raise ValueError("Credential scan observation scope changed")
        return self._private(result)

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

        fingerprint = publisher.content.credential_fingerprint()
        scan = (
            await publisher.index.credential_proof(before[0], fingerprint)
            if descriptor.get("credential_fingerprint") != fingerprint
            else None
        )
        data = await publisher.content.window(
            before[0].content_binding(), entry, descriptor, offset, limit, authorize=current, scan=scan
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

    @get_mapping("/revisions/{snapshot_id}/credential-scan-candidate")
    async def scan_candidate(
        self,
        run_id: PathVar[str],
        snapshot_id: PathVar[str],
        expected_manifest: QueryParam[str],
    ) -> Response:
        return JSONResponse(
            await self.explorer.scan_candidate(run_id, snapshot_id, expected_manifest),
            headers={"Cache-Control": "private, no-store"},
        )

    @post_mapping("/revisions/{snapshot_id}/credential-scans/{request_id}")
    async def rescan(
        self,
        run_id: PathVar[str],
        snapshot_id: PathVar[str],
        request_id: PathVar[str],
        body: Body[SourceCredentialScanInputV1],
    ) -> Response:
        return JSONResponse(
            await self.explorer.rescan(run_id, snapshot_id, request_id, body),
            headers={"Cache-Control": "private, no-store"},
        )

    @get_mapping("/revisions/{snapshot_id}/credential-scans/{request_id}")
    async def scan_receipt(
        self,
        run_id: PathVar[str],
        snapshot_id: PathVar[str],
        request_id: PathVar[str],
        expected_manifest: QueryParam[str],
    ) -> Response:
        return JSONResponse(
            await self.explorer.scan_receipt(run_id, snapshot_id, request_id, expected_manifest),
            headers={"Cache-Control": "private, no-store"},
        )

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
