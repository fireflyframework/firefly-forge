# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Project document admission with committed intent before a scoped flycanon write."""

from __future__ import annotations

import base64
import binascii
import copy
import hashlib
import json
import uuid
from collections.abc import Callable
from typing import Any

import httpx
from flycanon_sdk import CanonAPIError, CanonError, SourceMetadata, SubmitSourceJsonPayload  # type: ignore[import-untyped]
from koinon_sdk import ApiError
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from pyfly.container import rest_controller
from pyfly.data.relational.sqlalchemy import LockMode
from pyfly.web import Body, PathVar, get_mapping, post_mapping, request_mapping
from sqlalchemy import text

from firefly_forge.journal import digest as native_digest
from firefly_forge.knowledge import KnowledgeManager, project_workspace, snapshot_knowledge
from firefly_forge.knowledge_ingestion import KoinonSourceInput
from firefly_forge.knowledge_native_sources import observe_source
from firefly_forge.knowledge_persistence import ForgeKnowledgeUploadEntity, ForgeKnowledgeUploadRepository
from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied, ProjectAccess
from firefly_forge.secrets import redact_environment
from firefly_forge.store import Store, identifier, now

Data = dict[str, Any]


class KnowledgeUploadInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    filename: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9 _.-]{0,179}\.(?:txt|md|json|yaml|yml|csv)$")
    title: str = Field(min_length=1, max_length=200)
    content_base64: str = Field(min_length=1, max_length=1_333_336)

    def content(self) -> bytes:
        try:
            content = base64.b64decode(self.content_base64, validate=True)
            decoded = content.decode("utf8")
        except (binascii.Error, UnicodeError) as exc:
            raise ValueError("Upload a UTF-8 text document using valid base64") from exc
        if not content or len(content) > 1_000_000 or "\x00" in decoded:
            raise ValueError("Upload nonempty text up to 1 MB without binary content")
        return content


class KnowledgeUploadResolution(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,100}$")


class KnowledgeUploads:
    def __init__(
        self,
        store: Store,
        knowledge: KnowledgeManager,
        records: ForgeKnowledgeUploadRepository | None = None,
        redact: Callable[[str], str] = redact_environment,
    ) -> None:
        self.store, self.knowledge = store, knowledge
        self.records = records or ForgeKnowledgeUploadRepository()
        self.redact = redact
        self.active: set[str] = set()

    @staticmethod
    def public(data: Data) -> Data:
        return {key: copy.deepcopy(value) for key, value in data.items() if key != "knowledge_snapshot"}

    async def uploads(self, repository_id: str) -> list[Data]:
        await self.store.repository(repository_id)
        async with self.store._unit():
            records = [self.public(row.data) for row in await self.records.latest(identifier(repository_id))]
        for position, data in enumerate(records):
            if data["status"] == "submitting" and data["id"] not in self.active:
                records[position] = await self._change(
                    data["id"],
                    status="needs_reconciliation",
                    detail="Admission was interrupted; inspect scoped sources before another admission",
                )
        return records

    async def _change(self, upload_id: str, **changes: Any) -> Data:
        async with self.store._unit():
            row = await self.records.find_by_id(identifier(upload_id), lock=LockMode.PESSIMISTIC_WRITE)
            if row is None:
                raise KeyError("Knowledge admission not found")
            data = copy.deepcopy(row.data)
            data.update(changes, updated_at=now())
            if "status" in changes:
                data["history"].append({"status": changes["status"], "at": now()})
            row.data = data
            await self.records.save(row)
            return self.public(data)

    async def native_upload(
        self, repository_id: str, body: KoinonSourceInput, *, provenance: Data | None = None
    ) -> Data:
        await self.store.repository(repository_id)
        settings = self.knowledge.settings()
        if settings.get("provider") != "koinon":
            raise ValueError("Use the retained legacy admission route for this binding")
        platform = self.knowledge.platform
        scope = platform.scope(settings, repository_id)
        if scope.collection_id is None:
            raise ValueError("Map a Koinon collection before project document ingestion")
        verify = await platform.authority(repository_id, settings, permission="edit")
        content = body.content()
        provenance = copy.deepcopy(provenance or {})
        if provenance:
            if set(provenance) != {"analysis_id", "analysis_digest", "project_id", "project_digest", "component_ids"}:
                raise ValueError("Use exact server-derived reviewed source provenance")
            for key, size in (("analysis_id", 32), ("analysis_digest", 64), ("project_id", 32), ("project_digest", 64)):
                value = provenance[key]
                if (
                    not isinstance(value, str)
                    or len(value) != size
                    or any(char not in "0123456789abcdef" for char in value)
                ):
                    raise ValueError("Reviewed source provenance has an invalid identity")
            if not isinstance(provenance["component_ids"], list) or any(
                not isinstance(value, str) or len(value) != 32 or any(char not in "0123456789abcdef" for char in value)
                for value in provenance["component_ids"]
            ):
                raise ValueError("Reviewed source provenance has invalid component identities")
        if any(
            self.redact(value) != value or self.knowledge.safe(value, settings) != value
            for value in (body.filename, content.decode(), json.dumps(provenance))
        ):
            raise ValueError("Remove registered credentials before project source ingestion")
        async with platform.client(settings) as client:
            await platform.grant(client, settings, scope, "ingest:write")
            await platform.bind(client, settings, scope)
            await verify()
        content_sha = hashlib.sha256(content).hexdigest()
        principal = CURRENT_PRINCIPAL.get()
        if principal is None:
            raise AccessDenied("Project source ingestion requires a current authenticated identity")
        actor = ProjectAccess.actor_snapshot(principal)
        bound = {
            "filename": body.filename,
            "content_sha256": content_sha,
            "binding_sha256": native_digest(settings),
            "scope": scope.model_dump(),
            "provenance": provenance,
        }
        fingerprint = native_digest(bound)
        await self.knowledge._assert_ownership()
        async with self.store._unit() as session:
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": "forge:koinon-upload:" + repository_id + fingerprint},
            )
            original = await self.records.find_by_id(identifier(body.request_id), lock=LockMode.PESSIMISTIC_WRITE)
            if original is not None:
                if (
                    original.repository_id != identifier(repository_id)
                    or original.digest != fingerprint
                    or original.data.get("actor") != actor
                ):
                    raise ValueError("Project source request identity already belongs to another input or actor")
                await verify()
                return self.public(original.data)
            existing = await self.records.find_digest(identifier(repository_id), fingerprint)
            if existing is not None:
                await verify()
                return self.public(existing.data)
            data = {
                "version": 2,
                "provider": "koinon",
                "id": body.request_id,
                "repository_id": repository_id,
                "filename": body.filename,
                "title": body.filename,
                "content_sha256": content_sha,
                "content_bytes": len(content),
                "workspace_id": scope.workspace_id,
                "actor": actor,
                "status": "submitting",
                "created_at": now(),
                "updated_at": now(),
                "history": [{"status": "submitting", "at": now()}],
                "knowledge_snapshot": snapshot_knowledge(self.store.configuration),
                "provenance": provenance,
            }
            await self.records.save(
                ForgeKnowledgeUploadEntity(
                    id=identifier(body.request_id),
                    repository_id=identifier(repository_id),
                    digest=fingerprint,
                    data=data,
                )
            )
        self.active.add(body.request_id)
        try:
            async with platform.client(settings) as client:
                await platform.grant(client, settings, scope, "ingest:write")
                await platform.bind(client, settings, scope)
                await verify()
                result = await client.upload(
                    settings["tenant_id"],
                    filename=body.filename,
                    content=content,
                    parent_id=scope.collection_id,
                    workspace_id=scope.workspace_id,
                    project_id=scope.project_id,
                    mode="document",
                )
                platform.scoped(result.source, settings, scope)
                if (
                    result.source.kind != "source"
                    or result.source.parent_id != scope.collection_id
                    or result.source.name != body.filename
                    or result.source.metadata.get("sha256") != content_sha
                ):
                    raise ValueError("Native project source receipt differs from its admitted content")
                for asset in result.assets:
                    platform.scoped(asset, settings, scope)
                    if asset.parent_id != result.source.id or asset.kind != "document":
                        raise ValueError("Native project asset differs from its source")
                evidence = {
                    "source_id": result.source.id,
                    "source_status": result.source.status,
                    "asset_ids": [asset.id for asset in result.assets],
                    "creation_causality": "observed_upload_response",
                    "content_sha256": content_sha,
                    "origin": "external_proposal",
                    "version_state": "not_recorded",
                }
                if self.knowledge.safe(json.dumps(evidence), settings) != json.dumps(evidence):
                    raise ValueError("Native source identity contains a registered credential")
                saved = await self._change(
                    body.request_id,
                    status="submitted",
                    source_id=result.source.id,
                    source_status=result.source.status,
                    native_receipt=evidence,
                )
                await platform.grant(client, settings, scope, "ingest:write")
                await verify()
                return saved
        except (ApiError, httpx.HTTPError, ValidationError, ValueError):
            return await self._change(
                body.request_id,
                status="needs_reconciliation",
                detail="Native source outcome is uncertain; observe exact scoped content without resending",
            )
        finally:
            self.active.discard(body.request_id)

    async def native_observation(self, repository_id: str, data: Data, source_id: str, *, write: bool) -> Data:
        try:
            return await self._native_observation(repository_id, data, source_id, write=write)
        except (ApiError, httpx.HTTPError, ValidationError):
            raise ValueError(
                "Native project source observation is unavailable; retain its original admission"
            ) from None

    async def _native_observation(self, repository_id: str, data: Data, source_id: str, *, write: bool) -> Data:
        settings = self.knowledge.settings(data)
        platform = self.knowledge.platform
        scope = platform.scope(settings, repository_id)
        verify = await platform.authority(repository_id, settings, permission="edit" if write else "read")
        if data.get("source_id") and data["source_id"] != source_id:
            raise ValueError("Read back the original native source identity")
        async with platform.client(settings) as client:
            await platform.grant(client, settings, scope, "catalog:read")
            await platform.grant(client, settings, scope, "knowledge:search")
            evidence = await observe_source(
                platform,
                client,
                settings,
                scope,
                source_id,
                data["filename"],
                data["content_sha256"],
                verify,
                (data.get("native_receipt") or {}).get("asset_ids"),
            )
            if self.knowledge.safe(json.dumps(evidence), settings) != json.dumps(evidence):
                raise ValueError("Native source identity contains a registered credential")
            await platform.grant(client, settings, scope, "knowledge:search")
            await verify()
        if write:
            await self.knowledge._assert_ownership()
            principal = CURRENT_PRINCIPAL.get()
            if principal is not None:
                evidence["observer"] = ProjectAccess.actor_snapshot(principal)
            saved = await self._change(
                data["id"],
                status="submitted",
                source_id=source_id,
                source_status=evidence["source_status"],
                native_receipt=evidence,
            )
            await verify()
            return saved
        return {**self.public(data), "native_observation": evidence}

    async def upload(
        self, repository_id: str, request: KnowledgeUploadInput, *, provenance: Data | None = None
    ) -> Data:
        await self.store.repository(repository_id)
        settings = self.knowledge.settings()
        if settings.get("provider") == "koinon":
            request.content()
            return await self.native_upload(
                repository_id,
                KoinonSourceInput(
                    request_id=uuid.uuid4().hex, filename=request.filename, content_base64=request.content_base64
                ),
                provenance=provenance,
            )
        if not settings["enabled"] or not settings["api_key"]:
            raise ValueError("Project document uploads require enabled flycanon with a platform API key")
        content = request.content()
        provenance = copy.deepcopy(provenance or {})
        if provenance:
            if set(provenance) != {"analysis_id", "analysis_digest", "project_id", "project_digest", "component_ids"}:
                raise ValueError("Use server-derived reviewed source provenance only")
            for key, length in (
                ("analysis_id", 32),
                ("analysis_digest", 64),
                ("project_id", 32),
                ("project_digest", 64),
            ):
                value = provenance[key]
                if (
                    not isinstance(value, str)
                    or len(value) != length
                    or any(c not in "0123456789abcdef" for c in value)
                ):
                    raise ValueError("Reviewed source provenance has an invalid identifier or digest")
            if not isinstance(provenance["component_ids"], list) or len(provenance["component_ids"]) > 50:
                raise ValueError("Reviewed source provenance requires bounded component identifiers")
        guarded = [request.filename, request.title, content.decode(), json.dumps(provenance)]
        if any(self.redact(value) != value or self.knowledge.safe(value, settings) != value for value in guarded):
            raise ValueError("Remove registered credentials from the document before uploading")
        scope = project_workspace(repository_id)
        digest = hashlib.sha256(
            json.dumps(
                {
                    "filename": request.filename,
                    "title": request.title,
                    "content": hashlib.sha256(content).hexdigest(),
                    "endpoint": settings["endpoint"],
                    "tenant": settings["tenant_id"],
                    **({"provenance": provenance} if provenance else {}),
                },
                sort_keys=True,
            ).encode()
        ).hexdigest()
        async with self.store._unit() as session:
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": "forge:upload:" + repository_id + digest},
            )
            existing = await self.records.find_digest(identifier(repository_id), digest)
            if existing:
                return self.public(existing.data)
            snapshot = snapshot_knowledge(self.store.configuration)
            data: Data = {
                "id": uuid.uuid4().hex,
                "repository_id": repository_id,
                "filename": request.filename,
                "title": request.title,
                "content_sha256": hashlib.sha256(content).hexdigest(),
                "content_bytes": len(content),
                "workspace_id": scope,
                "status": "submitting",
                "created_at": now(),
                "updated_at": now(),
                "history": [{"status": "submitting", "at": now()}],
                "knowledge_snapshot": snapshot,
                **({"provenance": provenance} if provenance else {}),
            }
            await self.records.save(
                ForgeKnowledgeUploadEntity(
                    id=identifier(data["id"]), repository_id=identifier(repository_id), digest=digest, data=data
                )
            )
        self.active.add(data["id"])
        try:
            async with self.knowledge.client(settings, scope) as client:
                await self.knowledge._assert_ownership()
                await self.knowledge.ensure_workspace(client, repository_id)
                await self.knowledge._assert_ownership()
                source = await client.submit_source(
                    SubmitSourceJsonPayload(
                        kind="text",
                        filename=request.filename,
                        content_type="text/plain",
                        content_base64=request.content_base64,
                        metadata=SourceMetadata(
                            title=request.title,
                            domain="engineering",
                            tags=["forge", "project-document"],
                            extra={
                                "repository_id": repository_id,
                                "forge_upload_id": data["id"],
                                "content_sha256": data["content_sha256"],
                                **provenance,
                            },
                        ),
                    )
                )
            return await self._change(data["id"], status="submitted", source_id=source.id, source_status=source.status)
        except CanonAPIError as exc:
            status = "rejected" if 400 <= exc.status_code < 500 else "needs_reconciliation"
            return await self._change(
                data["id"],
                status=status,
                detail="flycanon rejected this admission"
                if status == "rejected"
                else "The remote outcome is uncertain; inspect scoped sources before another admission",
            )
        except (CanonError, OSError, ValueError):
            return await self._change(
                data["id"],
                status="needs_reconciliation",
                detail="The remote outcome is uncertain; inspect scoped sources before another admission",
            )
        finally:
            self.active.discard(data["id"])

    async def admission(self, repository_id: str, upload_id: str) -> Data:
        async with self.store._unit():
            row = await self.records.find_by_id(identifier(upload_id))
            if row is None or row.repository_id != identifier(repository_id):
                raise KeyError("Knowledge admission not found in this project")
            data = copy.deepcopy(row.data)
        if data.get("provider") == "koinon":
            return (
                await self.native_observation(repository_id, data, data["source_id"], write=False)
                if data.get("source_id")
                else self.public(data)
            )
        if data.get("source_id"):
            settings = self.knowledge.settings(data)
            try:
                async with self.knowledge.client(settings, data["workspace_id"]) as client:
                    source = await client.get_source(data["source_id"])
            except CanonError:
                return await self._change(
                    upload_id,
                    source_status="unavailable",
                    source_checked_at=now(),
                    detail="The admitted source could not be checked in its configured workspace",
                )
            return await self._change(upload_id, source_status=source.status, source_checked_at=now(), detail=None)
        return self.public(data)

    async def reconcile(self, repository_id: str, upload_id: str, request: KnowledgeUploadResolution) -> Data:
        async with self.store._unit():
            row = await self.records.find_by_id(identifier(upload_id))
            if row is None or row.repository_id != identifier(repository_id):
                raise KeyError("Knowledge admission not found in this project")
            data = copy.deepcopy(row.data)
        if data.get("provider") == "koinon":
            if upload_id in self.active:
                raise ValueError("Wait for the original native upload before readback")
            return await self.native_observation(repository_id, data, request.source_id, write=True)
        if data["status"] not in {"submitting", "needs_reconciliation"} or upload_id in self.active:
            raise ValueError("Reconcile only interrupted admissions after their active request ends")
        settings = self.knowledge.settings(data)
        async with self.knowledge.client(settings, data["workspace_id"]) as client:
            source = await client.get_source(request.source_id)
        if source.content_sha256 != data["content_sha256"] or source.filename != data["filename"]:
            raise ValueError("The scoped source must match the admitted filename and exact document digest")
        return await self._change(
            upload_id,
            status="submitted",
            source_id=source.id,
            source_status=source.status,
            source_checked_at=now(),
            detail="Reconciled against the actual scoped source",
        )


@rest_controller
@request_mapping("/api/v1/repositories/{repository_id}/knowledge")
class KnowledgeUploadsController:
    def __init__(self, uploads: KnowledgeUploads) -> None:
        self.manager = uploads

    @get_mapping("/uploads")
    async def uploads(self, repository_id: PathVar[str]) -> list[Data]:
        return await self.manager.uploads(repository_id)

    @post_mapping("/uploads", status_code=202)
    async def upload(self, repository_id: PathVar[str], body: Body[KnowledgeUploadInput]) -> Data:
        return await self.manager.upload(repository_id, body)

    @post_mapping("/koinon/uploads", status_code=202)
    async def native_upload(self, repository_id: PathVar[str], body: Body[KoinonSourceInput]) -> Data:
        return await self.manager.native_upload(repository_id, body)

    @get_mapping("/uploads/{id}")
    async def admission(self, repository_id: PathVar[str], id: PathVar[str]) -> Data:
        return await self.manager.admission(repository_id, id)

    @post_mapping("/uploads/{id}/reconcile")
    async def reconcile(
        self, repository_id: PathVar[str], id: PathVar[str], body: Body[KnowledgeUploadResolution]
    ) -> Data:
        return await self.manager.reconcile(repository_id, id, body)
