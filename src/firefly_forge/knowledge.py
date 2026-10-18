# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Real flycanon SDK retrieval, explicit source indexing and verified project memory."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import ssl
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import httpx
from cryptography import x509
from flycanon_sdk import (  # type: ignore[import-untyped]
    CanonAPIError,
    CanonClient,
    CanonError,
    Citation,
    CreateKnowledgeRequest,
    SearchRequest,
    SourceMetadata,
    SubmitSourceJsonPayload,
    WorkspaceCreate,
)
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from pyfly.container import service

from firefly_forge.agentic_runtime import WorkspaceTools
from firefly_forge.configuration import ConfigurationStore, redact_secrets, validate_endpoint
from firefly_forge.process import checked
from firefly_forge.prompts import prompt_text
from firefly_forge.secrets import redact_environment
from firefly_forge.store import Store, now

if TYPE_CHECKING:
    from firefly_forge.engine import Engine

# flycanon bounds retrieval queries independently of full briefs and memory bodies.
FLYCANON_SEARCH_QUERY_LIMIT = 2000


class KnowledgeSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    endpoint: str = ""
    tenant_id: str = Field(default="", pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$|^$")
    framework_workspace: str = Field(default="", pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$|^$")
    required: bool = False
    auto_memory: bool = False
    api_key: str = Field(default="", max_length=16000)
    agent_token: str = Field(default="", max_length=16000)
    top_k: int = Field(default=5, ge=1, le=8)
    ca_certificate: str = Field(default="", max_length=32000)


class KnowledgeConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid")
    values: dict[str, Any] = Field(default_factory=dict, max_length=12)
    clear_secrets: list[str] = Field(default_factory=list, max_length=2)


class KnowledgeSearch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    repository_id: str = Field(min_length=1, max_length=100)
    query: str = Field(min_length=1, max_length=4000)


class KnowledgeIndex(BaseModel):
    model_config = ConfigDict(extra="forbid")
    paths: list[str] = Field(min_length=1, max_length=8)


class KnowledgeTest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    repository_id: str | None = None


def project_workspace(repository_id: str) -> str:
    if not re.fullmatch(r"[a-f0-9]{32}", repository_id):
        raise ValueError("Knowledge requires a canonical project repository identifier")
    return "forge-" + repository_id


def _public(settings: dict[str, Any], revision: str) -> dict[str, Any]:
    return {
        **{key: value for key, value in settings.items() if key not in {"api_key", "agent_token"}},
        "api_key_configured": bool(settings.get("api_key")),
        "agent_token_configured": bool(settings.get("agent_token")),
        "revision": revision,
    }


def _current(private: ConfigurationStore) -> tuple[dict[str, Any], str]:
    saved = private._read(private.directory / "knowledge.json")
    defaults = {"enabled": True, "required": True, "auto_memory": True} if not saved else {}
    return KnowledgeSettings.model_validate(saved.get("settings", defaults)).model_dump(), str(
        saved.get("revision", "disabled")
    )


def snapshot_knowledge(private: ConfigurationStore) -> dict[str, Any]:
    settings, _ = _current(private)
    revision = uuid.uuid4().hex
    private._write(private.revisions / f"knowledge-{revision}.json", settings)
    return _public(settings, revision)


def knowledge_tls(certificate: str) -> ssl.SSLContext:
    context = ssl.create_default_context()
    if certificate:
        try:
            ca = x509.load_pem_x509_certificate(certificate.encode())
            if not ca.extensions.get_extension_for_class(x509.BasicConstraints).value.ca:
                raise ValueError("Certificate is not a CA")
            context.load_verify_locations(cadata=certificate)
        except (ValueError, x509.ExtensionNotFound, ssl.SSLError) as exc:
            raise ValueError("Knowledge TLS requires a valid CA certificate") from exc
    return context


@service
class KnowledgeConnection:
    def __init__(self, private: ConfigurationStore) -> None:
        self.private = private

    @asynccontextmanager
    async def client(
        self,
        settings: dict[str, Any],
        workspace: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 20,
    ) -> AsyncIterator[CanonClient]:
        headers = {"X-Tenant-Id": settings["tenant_id"], "X-Workspace-Id": workspace}
        if settings["api_key"]:
            headers["X-API-Key"] = settings["api_key"]
        if settings["agent_token"]:
            headers["X-Agent-Token"] = settings["agent_token"]

        async def discard_browser_cookies(response: httpx.Response) -> None:
            # API-key callers do not carry browser sessions or double-submit CSRF cookies.
            http.cookies.clear()

        async with (
            httpx.AsyncClient(
                base_url=settings["endpoint"],
                headers=headers,
                timeout=timeout,
                follow_redirects=False,
                transport=transport,
                trust_env=False,
                verify=knowledge_tls(settings.get("ca_certificate", "")),
                event_hooks={"response": [discard_browser_cookies]},
            ) as http,
            CanonClient(
                base_url=settings["endpoint"],
                client=http,
                tenant_id=settings["tenant_id"],
                workspace_id=workspace,
                agent_token=settings["agent_token"] or None,
            ) as client,
        ):
            yield client


@service
class KnowledgeManager:
    def __init__(
        self,
        store: Store,
        engine: Engine | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        connection: KnowledgeConnection | None = None,
    ) -> None:
        self.store = store
        self.engine = engine
        self.private = store.configuration
        self.transport = transport
        self.connection = connection or KnowledgeConnection(self.private)
        self.journal: Any = None
        self.readiness: dict[str, Any] = {
            "state": "missing",
            "checked_at": None,
            "detail": "Required flycanon knowledge is missing; initialize the managed stack before product work",
        }
        settings, _ = _current(self.private)
        if settings["enabled"] and settings["endpoint"] and (settings["api_key"] or settings["agent_token"]):
            self.readiness = {
                "state": "configured",
                "checked_at": None,
                "detail": "Knowledge is configured; test the running service and cited retrieval",
            }
        elif not settings["enabled"]:
            self.readiness = {
                "state": "disabled",
                "checked_at": None,
                "detail": "Existing operator configuration disables knowledge",
            }

    def public(self) -> dict[str, Any]:
        settings, revision = _current(self.private)
        return {**_public(settings, revision), "readiness": self.readiness}

    async def _assert_ownership(self) -> None:
        if self.engine is not None and getattr(self.engine, "enforce_ownership", False):
            await self.engine.assert_ownership()

    def configure(self, request: KnowledgeConfiguration) -> dict[str, Any]:
        settings, _ = _current(self.private)
        if set(request.values) - KnowledgeSettings.model_fields.keys() or set(request.clear_secrets) - {
            "api_key",
            "agent_token",
        }:
            raise ValueError("Unknown knowledge configuration field")
        values = {key: value for key, value in request.values.items() if key not in {"api_key", "agent_token"} or value}
        settings.update(values)
        for key in request.clear_secrets:
            settings[key] = ""
        settings = KnowledgeSettings.model_validate(settings).model_dump()
        if settings["ca_certificate"]:
            knowledge_tls(settings["ca_certificate"])
        if settings["endpoint"]:
            settings["endpoint"] = validate_endpoint(settings["endpoint"])
        if settings["enabled"] and (
            not settings["endpoint"]
            or not settings["tenant_id"]
            or not (settings["api_key"] or settings["agent_token"])
        ):
            raise ValueError("Enabled knowledge requires an endpoint, tenant and API key or scoped agent token")
        with self.private.lock:
            self.private._write(
                self.private.directory / "knowledge.json", {"revision": uuid.uuid4().hex, "settings": settings}
            )
        self.readiness = {
            "state": "configured" if settings["enabled"] else "disabled",
            "checked_at": None,
            "detail": "Configuration saved; test authentication and retrieval separately",
        }
        return self.public()

    def settings(self, run: dict[str, Any] | None = None) -> dict[str, Any]:
        snapshot = (run or {}).get("knowledge_snapshot")
        if not snapshot:
            return _current(self.private)[0]
        revision = str(snapshot["revision"])
        if not re.fullmatch(r"[a-f0-9]{32}", revision):
            raise ValueError("Invalid knowledge revision")
        settings = self.private._read(self.private.revisions / f"knowledge-{revision}.json")
        if not settings:
            raise ValueError("Knowledge revision is unavailable")
        return KnowledgeSettings.model_validate(settings).model_dump()

    def credentials(self, run: dict[str, Any]) -> dict[str, str]:
        settings = self.settings(run)
        return {"FORGE_KNOWLEDGE_API_KEY": settings["api_key"], "FORGE_KNOWLEDGE_AGENT_TOKEN": settings["agent_token"]}

    def redaction_settings(self, run: dict[str, Any] | None = None) -> dict[str, str]:
        settings = self.settings()
        registered = {
            "FORGE_KNOWLEDGE_API_KEY": settings["api_key"],
            "FORGE_KNOWLEDGE_AGENT_TOKEN": settings["agent_token"],
        }
        if run and run.get("knowledge_snapshot"):
            frozen = self.settings(run)
            registered.update(
                FORGE_KNOWLEDGE_FROZEN_API_KEY=frozen["api_key"],
                FORGE_KNOWLEDGE_FROZEN_AGENT_TOKEN=frozen["agent_token"],
            )
        return registered

    def safe(self, value: str, settings: dict[str, Any]) -> str:
        return redact_secrets(
            redact_environment(value),
            {"KNOWLEDGE_API_KEY": settings["api_key"], "KNOWLEDGE_AGENT_TOKEN": settings["agent_token"]},
        )

    @asynccontextmanager
    async def client(self, settings: dict[str, Any], workspace: str) -> AsyncIterator[CanonClient]:
        async with self.connection.client(settings, workspace, transport=self.transport) as client:
            yield client

    async def initialize_managed(self) -> dict[str, Any]:
        from firefly_forge.managed_knowledge import ManagedKnowledgeProvisioner

        provisioner = ManagedKnowledgeProvisioner(self.connection, transport=self.transport)
        try:
            return await provisioner.initialize()
        finally:
            self.readiness = provisioner.readiness

    async def test(self, request: KnowledgeTest) -> dict[str, Any]:
        settings = self.settings()
        if not settings["enabled"] or not settings["api_key"]:
            self.readiness = {
                "state": "configured" if settings["enabled"] else "disabled",
                "checked_at": now(),
                "detail": (
                    "Stats authentication requires a platform API key; "
                    "agent-token retrieval is checked by a scoped search"
                ),
            }
            return self.public()
        if request.repository_id:
            await self.store.repository(request.repository_id)
        workspace = (
            project_workspace(request.repository_id)
            if request.repository_id
            else settings["framework_workspace"] or "forge"
        )
        try:
            async with self.client(settings, workspace) as client:
                await client.stats()
            self.readiness = {
                "state": "connected",
                "checked_at": now(),
                "detail": (
                    "Scoped flycanon stats authentication succeeded; indexing and retrieval need their own evidence"
                ),
            }
        except (CanonError, httpx.HTTPError, ValidationError, ValueError):
            self.readiness = {
                "state": "failed",
                "checked_at": now(),
                "detail": "Knowledge authentication or stats validation failed; no remote response body retained",
            }
        return self.public()

    async def search(self, repository_id: str, query: str, *, run: dict[str, Any] | None = None) -> dict[str, Any]:
        await self.store.repository(repository_id)
        settings = self.settings(run)
        if not settings["enabled"]:
            return {"hits": [], "warnings": [], "context": "", "citations": []}
        if not settings["endpoint"] or not (settings["api_key"] or settings["agent_token"]):
            raise ValueError("Initialize required flycanon knowledge before starting product work")
        workspaces = [project_workspace(repository_id)]
        if settings["framework_workspace"] and settings["framework_workspace"] not in workspaces:
            workspaces.append(settings["framework_workspace"])
        if self.private._read(self.private.directory / "managed-knowledge.json") and settings["api_key"]:
            try:
                async with self.client(settings, project_workspace(repository_id)) as client:
                    await self.ensure_workspace(client, repository_id)
            except (CanonError, httpx.HTTPError, ValidationError):
                raise ValueError("Required project knowledge workspace initialization failed") from None
        hits: list[dict[str, Any]] = []
        warnings = []
        retrieval_query = query[:FLYCANON_SEARCH_QUERY_LIMIT]
        for workspace in workspaces:
            try:
                async with self.client(settings, workspace) as client:
                    result = (
                        await client.agent.search(
                            SearchRequest(query=retrieval_query, top_k=settings["top_k"]),
                            idempotency_key=uuid.uuid4().hex,
                        )
                        if settings["agent_token"]
                        else await client.search(retrieval_query, top_k=settings["top_k"])
                    )
                for hit in result.hits[: settings["top_k"]]:
                    entry = {
                        key: self.safe(value, settings) if isinstance(value, str) else value
                        for key, value in hit.model_dump().items()
                        if key
                        in {
                            "chunk_id",
                            "source_id",
                            "source_filename",
                            "source_title",
                            "source_uri",
                            "section_path",
                            "page",
                            "knowledge_item_id",
                            "knowledge_version",
                            "score",
                        }
                    }
                    uri = entry.get("source_uri")
                    if uri:
                        parsed = urlsplit(uri)
                        entry["source_uri"] = (
                            uri
                            if parsed.scheme in {"https", "http"}
                            and not (parsed.username or parsed.password or parsed.query or parsed.fragment)
                            else None
                        )
                    hits.append(
                        {**entry, "workspace_id": workspace, "content": self.safe(hit.content[:2000], settings)}
                    )
            except (CanonError, httpx.HTTPError, ValidationError, ValueError):
                if settings["required"]:
                    raise ValueError("Required knowledge retrieval failed in its configured workspace") from None
                warnings.append(
                    f"Knowledge retrieval unavailable for workspace {workspace}; no context obtained from that scope"
                )
        hits = hits[:16]
        citations = [{key: value for key, value in hit.items() if key != "content"} for hit in hits]
        context = ""
        if hits:
            context = prompt_text("tools/knowledge-evidence", hits=json.dumps(hits, ensure_ascii=False)[:40000])
        return {"hits": hits, "warnings": warnings, "citations": citations, "context": context}

    async def retrieve(self, run: dict[str, Any]) -> dict[str, Any]:
        return await self.search(run["repository_id"], run["brief"], run=run)

    async def explore(self, repository_id: str, scope: str = "project", offset: int = 0) -> dict[str, Any]:
        await self.store.repository(repository_id)
        if scope not in {"project", "framework"} or not 0 <= offset <= 10000:
            raise ValueError("Choose project/framework scope and an offset between 0 and 10000")
        settings = self.settings()
        workspace = project_workspace(repository_id) if scope == "project" else settings["framework_workspace"]
        result: dict[str, Any] = {
            "workspace_id": workspace,
            "scope": scope,
            "sources": [],
            "knowledge": [],
            "offset": offset,
            "limit": 50,
            "totals": {},
            "browsing_available": False,
            "warnings": [],
        }
        if not settings["enabled"] or not workspace:
            result["warnings"] = ["Configure the requested flycanon workspace before browsing"]
            return result
        if not settings["api_key"]:
            result["warnings"] = [
                "Agent-token access supports cited search; corpus browsing requires a platform API key"
            ]
            return result
        try:
            async with self.client(settings, workspace) as client:
                sources = await client.list_sources(limit=50, offset=offset)
                knowledge = await client.list_knowledge_items(limit=50, offset=offset)
            for record in sources.items:
                data = record.model_dump(mode="json")
                result["sources"].append(
                    {
                        key: self.safe(str(data[key]), settings) if isinstance(data[key], str) else data[key]
                        for key in (
                            "id",
                            "kind",
                            "status",
                            "filename",
                            "content_sha256",
                            "content_bytes",
                            "n_chunks",
                            "created_at",
                            "updated_at",
                        )
                    }
                )
            for record in knowledge.items:
                data = record.model_dump(mode="json")
                result["knowledge"].append(
                    {
                        key: self.safe(str(data[key])[:8000], settings) if isinstance(data[key], str) else data[key]
                        for key in (
                            "id",
                            "status",
                            "title",
                            "summary",
                            "domain",
                            "current_version",
                            "created_at",
                            "updated_at",
                        )
                    }
                )
            result.update(browsing_available=True, totals={"sources": sources.total, "knowledge": knowledge.total})
        except (CanonError, httpx.HTTPError, ValidationError, ValueError):
            result["warnings"] = [
                "Scoped knowledge browsing failed; check platform-key access and workspace configuration"
            ]
        return result

    async def ensure_workspace(self, client: CanonClient, repository_id: str) -> None:
        workspace = project_workspace(repository_id)
        try:
            await client.get_workspace(workspace)
        except CanonAPIError as exc:
            if exc.status_code != 404:
                raise
            await self._assert_ownership()
            await client.create_workspace(WorkspaceCreate(id=workspace, name="firefly forge project " + repository_id))

    async def index(self, identifier: str, paths: list[str]) -> dict[str, Any]:
        await self._assert_ownership()
        if self.engine is None:
            raise ValueError("Verified source indexing requires the factory engine")
        run = await self.store.get_run(identifier)
        workspace = await self.engine.validated_workspace(identifier)
        settings = self.settings()
        if not settings["enabled"]:
            raise ValueError("Enable knowledge before indexing project source")
        tracked = set(
            (await checked(["git", "ls-tree", "-r", "--name-only", run["source_tree"]], workspace)).splitlines()
        )
        tools = WorkspaceTools(workspace, max_file_bytes=250000)
        sources = []
        contents = []
        for path in paths:
            if path not in tracked:
                raise ValueError("Index only selected files in the verified tracked source tree")
            content = await tools.read_file(path)
            if self.safe(content, settings) != content:
                raise ValueError("Selected source contains a configured credential")
            contents.append((path, content))
        if sum(len(content.encode()) for _, content in contents) > 1_000_000:
            raise ValueError("Selected source exceeds the 1 MB indexing limit")
        scope = project_workspace(run["repository_id"])
        try:
            async with self.client(settings, scope) as client:
                if settings["api_key"]:
                    await self.ensure_workspace(client, run["repository_id"])
                for path, content in contents:
                    task_id = (
                        "memory-index-"
                        + uuid.uuid5(
                            uuid.NAMESPACE_URL, path + run["source_tree"] + settings["endpoint"] + settings["tenant_id"]
                        ).hex
                    )
                    intent = (
                        await self.journal.intent(
                            identifier,
                            task_id,
                            payload={"path": path, "source_tree": run["source_tree"], "workspace_id": scope},
                        )
                        if self.journal
                        else None
                    )
                    if intent and intent["disposition"] == "completed":
                        task = next(
                            item for item in (await self.journal.plan(identifier))["tasks"] if item["id"] == task_id
                        )
                        sources.append(task["evidence"])
                        continue
                    if intent and intent["disposition"] != "execute":
                        raise ValueError(
                            "Source indexing has an uncertain remote outcome; reconcile it before resubmitting"
                        )
                    payload = SubmitSourceJsonPayload(
                        kind="text",
                        filename=path,
                        content_type="text/plain",
                        content_base64=base64.b64encode(content.encode()).decode(),
                        metadata=SourceMetadata(
                            title=path,
                            domain="engineering",
                            tags=["forge", run["repository_id"]],
                            extra={"run_id": identifier, "source_tree": run["source_tree"], "path": path},
                        ),
                    )
                    await self._assert_ownership()
                    source = (
                        await client.agent.ingest_source(
                            payload,
                            idempotency_key=(
                                f"forge-{identifier}-{uuid.uuid5(uuid.NAMESPACE_URL, path + run['source_tree']).hex}"
                            ),
                        )
                        if settings["agent_token"]
                        else await client.submit_source(payload)
                    )
                    evidence = {"source_id": source.id, "status": source.status, "path": path}
                    sources.append(evidence)
                    await self.store.update(identifier, knowledge_sources=sources)
                    if intent:
                        await self.journal.receipt(identifier, intent["idempotency_key"], evidence)
        except (CanonError, httpx.HTTPError, ValidationError):
            raise ValueError(
                "Knowledge source indexing failed; inspect the flycanon service without exposing credentials"
            ) from None
        await self.store.update(identifier, knowledge_sources=sources)
        return {"sources": sources, "workspace_id": scope}

    async def remember(self, run: dict[str, Any], *, automatic: bool = False) -> dict[str, Any]:
        await self._assert_ownership()
        run = await self.store.get_run(run["id"])
        settings = self.settings(run) if automatic else self.settings()
        if automatic and not settings["auto_memory"]:
            return {"skipped": True}
        if run.get("knowledge_memory"):
            return dict(run["knowledge_memory"])
        if run.get("knowledge_memory_status") == "writing":
            raise ValueError("Memory write has an uncertain outcome; reconcile it in flycanon before retrying")
        if self.engine is None:
            raise ValueError("Verified project memory requires the factory engine")
        await self.engine.validated_workspace(run["id"])
        if not settings["enabled"] or not settings["api_key"]:
            raise ValueError("Verified memory requires enabled knowledge with a platform API key")
        scope = project_workspace(run["repository_id"])
        body = json.dumps(
            {
                "run_id": run["id"],
                "operation": run.get("operation", "build"),
                "summary": run["summary"][:6000],
                "source_tree": run["source_tree"],
                "changed_files": run["changed_files"][:100],
                "verification": [
                    {"argv": item["argv"], "exit_code": item["exit_code"]} for item in run["final_verification"]
                ],
            },
            ensure_ascii=False,
        )
        body = self.safe(body, settings)
        if len(body.encode()) > 1_000_000:
            raise ValueError("Verified memory exceeds the 1 MB indexing limit")
        try:
            async with self.client(settings, scope) as client:
                await self.ensure_workspace(client, run["repository_id"])
                source_id = await self._memory_source(client, run, body, scope)
                retrieved = await client.search(body[:FLYCANON_SEARCH_QUERY_LIMIT], top_k=8, source_ids=[source_id])
                citations = [
                    Citation(source_id=source_id, chunk_id=hit.chunk_id, quote=hit.content[:2000])
                    for hit in retrieved.hits
                    if hit.source_id == source_id and hit.chunk_id
                ]
                if not citations:
                    raise ValueError(
                        "Verified memory source has no retrievable chunks; retry retrieval before publishing"
                    )
                await self._assert_ownership()
                intent = (
                    await self.journal.intent(
                        run["id"],
                        "memory-knowledge",
                        payload={"source_tree": run["source_tree"], "workspace_id": scope, "source_id": source_id},
                    )
                    if self.journal
                    else None
                )
                if intent and intent["disposition"] != "execute":
                    raise ValueError("Memory intent has an uncertain outcome; reconcile it before another write")
                await self.store.update(run["id"], knowledge_memory_status="writing")
                await self._assert_ownership()
                item = await client.create_knowledge(
                    CreateKnowledgeRequest(
                        title="Verified firefly forge run " + run["id"],
                        body=body,
                        summary="Verified project source and checks",
                        domain="engineering",
                        tags=["forge", run["repository_id"]],
                        actor="firefly-forge",
                        metadata={"run_id": run["id"], "source_tree": run["source_tree"]},
                        citations=citations,
                    )
                )
        except (CanonError, httpx.HTTPError, ValidationError):
            raise ValueError("Verified memory could not be saved; no raw response body retained") from None
        result = {
            "item_id": item.knowledge_item_id,
            "version": item.version,
            "workspace_id": scope,
            "source_id": source_id,
        }
        async with self.store._unit():
            await self.store.update(run["id"], knowledge_memory=result, knowledge_memory_status="saved")
            if intent:
                await self.journal.receipt(run["id"], intent["idempotency_key"], result)
        return result

    async def _memory_source(self, client: CanonClient, run: dict[str, Any], body: str, scope: str) -> str:
        content_hash = hashlib.sha256(body.encode()).hexdigest()
        saved = run.get("knowledge_memory_source", {})
        if saved:
            source = await client.get_source(saved["source_id"])
        else:
            if run.get("knowledge_memory_source_status") == "indexing":
                raise ValueError("Memory source has an uncertain outcome; reconcile it before resubmitting")
            intent = (
                await self.journal.intent(
                    run["id"],
                    "memory-source",
                    payload={"content_sha256": content_hash, "source_tree": run["source_tree"], "workspace_id": scope},
                )
                if self.journal
                else None
            )
            if intent and intent["disposition"] != "execute":
                raise ValueError("Memory source has an uncertain outcome; reconcile it before resubmitting")
            await self.store.update(run["id"], knowledge_memory_source_status="indexing")
            await self._assert_ownership()
            source = await client.submit_source(
                SubmitSourceJsonPayload(
                    kind="text",
                    filename="forge-memory.json",
                    content_type="text/plain",
                    content_base64=base64.b64encode(body.encode()).decode(),
                    metadata=SourceMetadata(
                        title="Verified firefly forge run " + run["id"],
                        domain="engineering",
                        tags=["forge", "verified-memory", run["repository_id"]],
                        extra={"run_id": run["id"], "source_tree": run["source_tree"]},
                    ),
                )
            )
            evidence = {"source_id": source.id, "status": source.status, "content_sha256": source.content_sha256}
            async with self.store._unit():
                await self.store.update(
                    run["id"], knowledge_memory_source=evidence, knowledge_memory_source_status=source.status
                )
                if intent:
                    await self.journal.receipt(run["id"], intent["idempotency_key"], evidence)
        if source.content_sha256 != content_hash or source.status != "ingested" or source.n_chunks < 1:
            raise ValueError("Verified memory source does not match its admitted content and indexed chunks")
        return str(source.id)
