# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Native headless Koinon reads retain explicit scope and source provenance."""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any, Literal

import httpx
from koinon_sdk import ApiError, KoinonClient
from koinon_sdk.contracts import SearchContext
from koinon_sdk.evidence import SourceReference
from koinon_sdk.models import CatalogEntry
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from firefly_forge.journal import digest
from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied, Permission

if TYPE_CHECKING:
    from firefly_forge.knowledge import KnowledgeManager

Data = dict[str, Any]


class KoinonScope(BaseModel):
    model_config = ConfigDict(hide_input_in_errors=True, extra="forbid", frozen=True)
    workspace_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    project_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    collection_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,64}$")
    dataset_ids: list[str] = Field(default_factory=list)

    @field_validator("dataset_ids")
    @classmethod
    def unique_datasets(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)) or any(not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", x) for x in values):
            raise ValueError("Choose unique explicit Koinon dataset identities")
        return values


class KoinonKnowledgeSettings(BaseModel):
    model_config = ConfigDict(hide_input_in_errors=True, extra="forbid")
    provider: Literal["koinon"] = "koinon"
    version: Literal[2] = 2
    enabled: bool = True
    required: bool = True
    auto_memory: bool = True
    endpoint: str = ""
    tenant_id: str = Field(default="", pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$|^$")
    access_token: str = Field(default="", max_length=16000, repr=False)
    ca_certificate: str = Field(default="", max_length=32000)
    project_scopes: dict[str, KoinonScope] = Field(default_factory=dict)
    framework_scope: KoinonScope | None = None
    top_k: int = Field(default=10, ge=1, le=100, strict=True)

    @field_validator("project_scopes")
    @classmethod
    def repository_keys(cls, values: dict[str, KoinonScope]) -> dict[str, KoinonScope]:
        if any(not re.fullmatch(r"[a-f0-9]{32}", key) for key in values):
            raise ValueError("Map canonical Forge repository identities explicitly")
        return values


class _ScopedTransport(httpx.AsyncBaseTransport):
    def __init__(self, inner: httpx.AsyncBaseTransport, endpoint: str, tenant: str) -> None:
        self.inner = inner
        url = httpx.URL(endpoint)
        self.origin = (url.scheme, url.host, url.port)
        self.prefix = url.path.rstrip("/") + "/"
        self.tenant = tenant

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if (request.url.scheme, request.url.host, request.url.port) != self.origin:
            raise ValueError("Koinon request changed its admitted origin")
        path = request.url.path.removeprefix(self.prefix)
        if not request.url.path.startswith(self.prefix) or not (
            path in {"context", "capabilities"} or path.startswith("tenants/" + self.tenant + "/")
        ):
            raise ValueError("Koinon request changed its admitted tenant")
        request.headers.pop("cookie", None)
        return await self.inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self.inner.aclose()


class KoinonKnowledge:
    def __init__(self, manager: KnowledgeManager) -> None:
        self.manager = manager

    def scope(self, settings: Data, repository_id: str, *, framework: bool = False) -> KoinonScope:
        raw = settings.get("framework_scope") if framework else settings["project_scopes"].get(repository_id)
        if raw is None:
            raise ValueError("No explicit Koinon scope is mapped to this Forge repository")
        return KoinonScope.model_validate(raw)

    async def authority(
        self, repository_id: str, settings: Data, run: Data | None = None, *, permission: Permission = "read"
    ) -> Callable[[], Awaitable[None]]:
        access = self.manager.access
        principal = CURRENT_PRINCIPAL.get()
        if access is None or principal is None and not (run or {}).get("execution_actor"):
            raise AccessDenied("Koinon requires a current authenticated repository grant")
        actor = (run or {}).get("execution_actor")

        async def verify() -> None:
            if CURRENT_PRINCIPAL.get() != principal or self.manager.settings(run) != settings:
                raise AccessDenied("Koinon caller or knowledge binding changed")
            if run is not None:
                current = await self.manager.store.get_run(run["id"])
                if any(
                    current.get(key) != run.get(key)
                    for key in ("repository_id", "project_id", "execution_actor", "knowledge_snapshot", "source_tree")
                ):
                    raise AccessDenied("Koinon current run binding changed")
            if principal is not None:
                await access.require_repository(principal, repository_id, permission)
            else:
                if run is None or run["repository_id"] != repository_id:
                    raise AccessDenied("Koinon run scope changed")
                await access.authorize_actor(actor, run.get("project_id"), permission)
            await self.manager._assert_ownership()

        await verify()
        return verify

    @asynccontextmanager
    async def client(self, settings: Data) -> AsyncIterator[KoinonClient]:
        from pyfly.data.transaction import current_unit_of_work

        from firefly_forge.knowledge import knowledge_tls

        if current_unit_of_work() is not None:
            raise ValueError("Koinon remote I/O requires no active caller transaction")
        if (
            not settings["enabled"]
            or not settings["endpoint"]
            or not settings["tenant_id"]
            or not settings["access_token"]
        ):
            raise ValueError("Configure an enabled scoped Koinon connection before retrieval")
        transport = _ScopedTransport(
            self.manager.transport
            or httpx.AsyncHTTPTransport(verify=knowledge_tls(settings["ca_certificate"]), trust_env=False, retries=0),
            settings["endpoint"],
            settings["tenant_id"],
        )
        async with KoinonClient(
            settings["endpoint"], token_provider=lambda: settings["access_token"], transport=transport
        ) as client:
            yield client

    @staticmethod
    async def grant(client: KoinonClient, settings: Data, scope: KoinonScope, permission: str) -> None:
        context = await client.context()
        workspaces = [x for x in context.workspaces if x.id == scope.workspace_id]
        if (
            not any(x.id == settings["tenant_id"] for x in context.tenants)
            or len(workspaces) != 1
            or workspaces[0].tenant_id != settings["tenant_id"]
            or workspaces[0].kind != "workspace"
            or permission not in (context.workspace_permissions or {}).get(scope.workspace_id, [])
        ):
            raise AccessDenied("Current Koinon workspace authority is unavailable")

    @staticmethod
    def scoped(entry: CatalogEntry, settings: Data, scope: KoinonScope) -> None:
        if (
            entry.tenant_id != settings["tenant_id"]
            or entry.workspace_id != scope.workspace_id
            or entry.project_id != scope.project_id
        ):
            raise AccessDenied("Koinon returned a foreign tenant, workspace or project")

    async def ancestry(self, client: KoinonClient, settings: Data, scope: KoinonScope, entry: CatalogEntry) -> set[str]:
        self.scoped(entry, settings, scope)
        seen = {entry.id}
        while entry.id != scope.project_id:
            if entry.parent_id is None or entry.parent_id in seen:
                raise AccessDenied("Koinon catalog ancestry is incomplete or cyclic")
            entry = await client.get(settings["tenant_id"], entry.parent_id)
            self.scoped(entry, settings, scope)
            seen.add(entry.id)
        if entry.kind != "project" or entry.parent_id != scope.workspace_id:
            raise AccessDenied("Koinon project mapping disagrees with its workspace")
        if scope.collection_id and scope.collection_id not in seen:
            raise AccessDenied("Koinon asset is outside its mapped collection")
        return seen

    async def bind(self, client: KoinonClient, settings: Data, scope: KoinonScope) -> None:
        project = await client.get(settings["tenant_id"], scope.project_id)
        self.scoped(project, settings, scope)
        if project.kind != "project" or project.parent_id != scope.workspace_id:
            raise AccessDenied("Koinon project mapping is unavailable")
        if scope.collection_id:
            collection = await client.get(settings["tenant_id"], scope.collection_id)
            if collection.kind not in {"collection", "corpus"}:
                raise AccessDenied("Koinon collection mapping is unavailable")
            await self.ancestry(client, settings, scope, collection)

    async def search(self, repository_id: str, query: str, settings: Data, run: Data | None = None) -> Data:
        verify = await self.authority(repository_id, settings, run)
        scopes = [self.scope(settings, repository_id)]
        if settings.get("framework_scope") and not (run or {}).get("framework_knowledge_request"):
            scopes.append(self.scope(settings, repository_id, framework=True))
        hits = []
        snapshots = []
        try:
            async with self.client(settings) as client:
                for scope in scopes:
                    await self.grant(client, settings, scope, "knowledge:search")
                    await self.bind(client, settings, scope)
                    await verify()
                    context: SearchContext = {
                        "workspace_id": scope.workspace_id,
                        "project_id": scope.project_id,
                        "limit": settings["top_k"],
                        "answer": False,
                    }
                    if scope.collection_id is not None:
                        context["collection_id"] = scope.collection_id
                    result = await client.search(settings["tenant_id"], query, **context)
                    for hit in result.hits:
                        asset = await client.get(settings["tenant_id"], hit.asset_id)
                        if asset.kind not in {"document", "knowledge_item"}:
                            raise AccessDenied("Koinon search returned an unsupported catalog asset")
                        ancestors = await self.ancestry(client, settings, scope, asset)
                        if hit.source_id is not None and hit.source_id not in ancestors:
                            raise AccessDenied("Koinon citation source is outside its asset ancestry")
                        data = hit.model_dump(mode="json")
                        if data.get("knowledge_item_id") not in {None, asset.id}:
                            raise AccessDenied("Koinon citation knowledge identity changed")
                        native_citations = [citation.model_dump(mode="json") for citation in hit.citations]
                        for citation in native_citations:
                            if (
                                citation.get("asset_id") not in {None, asset.id}
                                or citation.get("document_id") not in {None, asset.id}
                                or citation.get("source_id") not in {None, hit.source_id}
                            ):
                                raise AccessDenied("Koinon citation changed its scoped asset")
                        reference = SourceReference(
                            id=hit.id,
                            asset_id=asset.id,
                            source_id=hit.source_id,
                            title=hit.title,
                            kind="knowledge_version" if asset.kind == "knowledge_item" else "document",
                            chunk_id=hit.id,
                            source_version_id=data.get("source_version_id"),
                            extraction_id=data.get("extraction_id"),
                            knowledge_item_id=data.get("knowledge_item_id"),
                            knowledge_version_id=data.get("knowledge_version_id"),
                            knowledge_version=data.get("knowledge_version"),
                        ).model_dump(mode="json")
                        reference.update(
                            native_citations=native_citations,
                            tenant_id=settings["tenant_id"],
                            workspace_id=scope.workspace_id,
                            project_id=scope.project_id,
                            provenance="recorded"
                            if reference["source_version_id"] or reference["knowledge_version_id"]
                            else "not_recorded",
                        )
                        hits.append({**reference, "score": hit.score, "content": self.manager.safe(hit.text, settings)})
                    for response_citation in result.citations:
                        if not any(
                            response_citation.asset_id in {None, item["asset_id"]}
                            and response_citation.document_id in {None, item["asset_id"]}
                            and response_citation.source_id in {None, item["source_id"]}
                            and (
                                response_citation.asset_id
                                or response_citation.document_id
                                or response_citation.source_id
                            )
                            for item in hits
                        ):
                            raise AccessDenied("Koinon response citation has no scoped retrieved asset")
                    snapshots.append(
                        result.knowledge_settings_snapshot.model_dump(mode="json")
                        if result.knowledge_settings_snapshot
                        else None
                    )
                    await self.grant(client, settings, scope, "knowledge:search")
                    await verify()
        except (ApiError, httpx.HTTPError, ValidationError):
            raise ValueError("Koinon retrieval failed; no remote response body retained or retry attempted") from None
        from firefly_forge.prompts import prompt_text

        safe_hits = json.loads(self.manager.safe(json.dumps(hits, ensure_ascii=False), settings))
        return {
            "provider": "koinon",
            "binding_sha256": digest(settings),
            "hits": safe_hits,
            "warnings": [],
            "citations": [{k: v for k, v in hit.items() if k != "content"} for hit in safe_hits],
            "knowledge_settings_snapshots": json.loads(self.manager.safe(json.dumps(snapshots), settings)),
            "context": prompt_text("tools/knowledge-evidence", hits=json.dumps(safe_hits, ensure_ascii=False))
            if hits
            else "",
        }

    async def test(self, repository_id: str, settings: Data) -> Data:
        scope = self.scope(settings, repository_id)
        verify = await self.authority(repository_id, settings)
        async with self.client(settings) as client:
            await self.grant(client, settings, scope, "catalog:read")
            await self.bind(client, settings, scope)
            capabilities = await client.capabilities()
            if capabilities.catalog is None or not capabilities.catalog.ready:
                raise ValueError("Koinon catalog readiness is unavailable")
            await self.grant(client, settings, scope, "catalog:read")
            await verify()
        return {
            "state": "connected",
            "detail": (
                "Mapped Koinon catalog authentication succeeded; retrieval and indexing require their own evidence"
            ),
        }

    async def dataset(self, client: KoinonClient, settings: Data, scope: KoinonScope, dataset_id: str) -> None:
        if dataset_id not in scope.dataset_ids:
            raise ValueError("Choose an explicitly mapped dataset")
        value = await client.get(settings["tenant_id"], dataset_id)
        if value.kind != "dataset":
            raise AccessDenied("Koinon query target is not a dataset")
        await self.ancestry(client, settings, scope.model_copy(update={"collection_id": None}), value)

    async def schema(self, repository_id: str, dataset_id: str, settings: Data) -> Data:
        scope = self.scope(settings, repository_id)
        if dataset_id not in scope.dataset_ids:
            raise ValueError("Choose an explicitly mapped dataset")
        verify = await self.authority(repository_id, settings)
        try:
            async with self.client(settings) as client:
                await self.grant(client, settings, scope, "catalog:read")
                await self.dataset(client, settings, scope, dataset_id)
                value = await client.schema(settings["tenant_id"], dataset_id)
                await self.grant(client, settings, scope, "catalog:read")
                await verify()
        except (ApiError, httpx.HTTPError, ValidationError):
            raise ValueError("Koinon schema is unavailable; no remote response body retained") from None
        return {
            "provider": "koinon",
            "binding_sha256": digest(settings),
            "dataset_id": dataset_id,
            "tenant_id": settings["tenant_id"],
            "workspace_id": scope.workspace_id,
            "project_id": scope.project_id,
            "schema": json.loads(self.manager.safe(value.model_dump_json(), settings)),
        }

    async def chunks(
        self,
        repository_id: str,
        document_id: str,
        settings: Data,
        *,
        offset: int,
        limit: int,
        expected_identity: str | None = None,
    ) -> Data:
        if type(offset) is not int or offset < 0 or type(limit) is not int or limit < 1:
            raise ValueError("Choose a nonnegative chunk offset and positive page size")
        if offset > 0 and expected_identity is None:
            raise ValueError("Continue chunk pages with their original source version identity")
        scope = self.scope(settings, repository_id)
        verify = await self.authority(repository_id, settings)
        try:
            async with self.client(settings) as client:
                await self.grant(client, settings, scope, "knowledge:search")
                document = await client.get(settings["tenant_id"], document_id)
                if document.kind != "document":
                    raise AccessDenied("Koinon chunks require a scoped document")
                ancestors = await self.ancestry(client, settings, scope, document)
                page = await client.document_chunks(settings["tenant_id"], document_id, offset=offset, limit=limit)
                identity = {
                    key: getattr(page, key)
                    for key in ("document_id", "source_id", "native_source_id", "source_version_id", "extraction_id")
                }
                if page.source_id not in ancestors:
                    raise AccessDenied("Koinon chunk page source is outside its document ancestry")
                if page.document_id != document_id or page.offset != offset or page.limit != limit:
                    raise ValueError("Koinon chunk page identity changed")
                if expected_identity is not None and digest(identity) != expected_identity:
                    raise ValueError("Koinon chunk source version identity changed")
                if page.next_offset is not None and page.next_offset <= offset:
                    raise ValueError("Koinon chunk pagination did not advance")
                await self.grant(client, settings, scope, "knowledge:search")
                await verify()
        except (ApiError, httpx.HTTPError, ValidationError):
            raise ValueError("Koinon chunks are unavailable; no remote response body retained") from None
        return {
            "provider": "koinon",
            "binding_sha256": digest(settings),
            "source_identity_sha256": digest(identity),
            "page": json.loads(self.manager.safe(page.model_dump_json(), settings)),
        }


class KoinonSqlInput(BaseModel):
    model_config = ConfigDict(hide_input_in_errors=True, extra="forbid", frozen=True, strict=True)
    request_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    dataset_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    sql: str = Field(min_length=1, max_length=65536)


class KoinonConnectionInput(BaseModel):
    model_config = ConfigDict(hide_input_in_errors=True, extra="forbid", frozen=True)
    settings: KoinonKnowledgeSettings
    clear_access_token: bool = Field(default=False, strict=True)
