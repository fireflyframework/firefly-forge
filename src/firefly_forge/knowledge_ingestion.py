# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""One-shot native source writes and external memory proposals with durable custody."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, Literal

import httpx
from koinon_sdk import ApiError
from koinon_sdk.curated import KnowledgeContent
from koinon_sdk.external_reviews import ExternalReviewInput
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from firefly_forge.knowledge_queries import KnowledgeQueries
from firefly_forge.process import checked
from firefly_forge.product_provenance import content_digest as digest
from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied, ProjectAccess

if TYPE_CHECKING:
    from firefly_forge.knowledge import KnowledgeManager

Data = dict[str, Any]


class KoinonSourceInput(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True, frozen=True)
    request_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    filename: str = Field(min_length=1, max_length=255, pattern=r"^[^/\\\x00]+$")
    content_base64: str = Field(min_length=1, repr=False)

    def content(self) -> bytes:
        try:
            value = base64.b64decode(self.content_base64, validate=True)
            text = value.decode("utf8")
        except (ValueError, binascii.Error, UnicodeError):
            raise ValueError("Supply an exact nonempty UTF-8 source document as base64") from None
        if not value or "\x00" in text:
            raise ValueError("Supply an exact nonempty UTF-8 source document")
        return value


class KoinonIngestionObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    remote_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")


class KoinonIngestion:
    def __init__(self, manager: KnowledgeManager) -> None:
        self.manager = manager

    def ledger(self, kind: Literal["source", "memory"]) -> KnowledgeQueries:
        return KnowledgeQueries(self.manager, operation=kind)

    def binding(self, run: Data, settings: Data, request_id: str, **fields: Any) -> Data:
        actor = CURRENT_PRINCIPAL.get()
        return {
            "version": 1,
            "request_id": request_id,
            "binding_sha256": digest(settings),
            "repository_id": run["repository_id"],
            "source_tree": run.get("source_tree"),
            "actor": ProjectAccess.actor_snapshot(actor) if actor is not None else run.get("execution_actor"),
            **fields,
        }

    async def source(
        self, run: Data, body: KoinonSourceInput, *, before_effect: Callable[[], Awaitable[None]] | None = None
    ) -> Data:
        settings = self.manager.settings(run)
        if settings.get("provider") != "koinon":
            raise ValueError("Native source ingestion requires a frozen Koinon binding")
        content = body.content()
        if any(self.manager.safe(value, settings) != value for value in (body.filename, content.decode())):
            raise ValueError("Remove registered credentials before source ingestion")
        platform = self.manager.platform
        scope = platform.scope(settings, run["repository_id"])
        if scope.collection_id is None:
            raise ValueError("Map an existing Koinon collection or corpus before source ingestion")
        verify = await platform.authority(run["repository_id"], settings, run, permission="edit")
        bound = self.binding(
            run,
            settings,
            body.request_id,
            filename=body.filename,
            content_sha256=hashlib.sha256(content).hexdigest(),
            content_bytes=len(content),
            scope=scope.model_dump(),
        )
        ledger = self.ledger("source")
        if await ledger.state(run, body.request_id) is None:
            async with platform.client(settings) as client:
                await platform.grant(client, settings, scope, "ingest:write")
                await platform.bind(client, settings, scope)
                await verify()
        state = await ledger.state(run, body.request_id, create=bound)
        assert state is not None
        if not state["dispatch"]:
            if state["receipt"] is None:
                raise ValueError(
                    "The original source outcome is uncertain; observe its exact scoped source without resubmitting"
                )
            await verify()
            return {"status": "submitted", "request_id": body.request_id, "receipt": state["receipt"]}
        try:
            async with platform.client(settings) as client:
                await platform.grant(client, settings, scope, "ingest:write")
                await platform.bind(client, settings, scope)
                await verify()
                if before_effect is not None:
                    await before_effect()
                result = await client.upload(
                    settings["tenant_id"],
                    filename=body.filename,
                    content=content,
                    parent_id=scope.collection_id,
                    workspace_id=scope.workspace_id,
                    project_id=scope.project_id,
                    mode="document",
                )
                source = result.source
                platform.scoped(source, settings, scope)
                if (
                    source.kind != "source"
                    or source.parent_id != scope.collection_id
                    or source.name != body.filename
                    or source.metadata.get("sha256") != bound["content_sha256"]
                ):
                    raise ValueError("Native source receipt differs from its admitted content or destination")
                for asset in result.assets:
                    platform.scoped(asset, settings, scope)
                    if asset.parent_id != source.id or asset.kind != "document":
                        raise AccessDenied("Native source asset escaped its admitted parent")
                evidence = {
                    "source_id": source.id,
                    "source_status": source.status,
                    "asset_ids": [x.id for x in result.assets],
                    "content_sha256": bound["content_sha256"],
                    "creation_causality": "observed_upload_response",
                    "origin": "external_proposal",
                    "source_version_id": None,
                    "version_state": "not_recorded",
                }
                if self.manager.safe(json.dumps(evidence), settings) != json.dumps(evidence):
                    raise ValueError("Native source identity contains a registered credential")
                completed = await ledger.state(run, body.request_id, receipt=evidence)
                assert completed is not None
                await platform.grant(client, settings, scope, "ingest:write")
                await verify()
        except (ApiError, httpx.HTTPError, ValidationError):
            raise ValueError("The original source outcome is uncertain; observe it without resubmitting") from None
        return {"status": "submitted", "request_id": body.request_id, "receipt": completed["receipt"]}

    async def index(self, run: Data, paths: list[str]) -> Data:
        from pathlib import Path

        from firefly_forge.workspace_tools import WorkspaceTools

        if self.manager.engine is None:
            raise ValueError("Verified source indexing requires the factory engine")
        workspace = await self.manager.engine.validated_workspace(run["id"])
        tracked = set(
            (await checked(["git", "ls-tree", "-r", "--name-only", run["source_tree"]], workspace)).splitlines()
        )
        tools = WorkspaceTools(workspace)
        sources = []
        for path in paths:
            if path not in tracked:
                raise ValueError("Index only selected files in the verified tracked source tree")
            content = (await tools.read_file(path)).encode()

            async def verify_source(path: str = path, content: bytes = content) -> None:
                assert self.manager.engine is not None
                if (
                    await self.manager.engine.validated_workspace(run["id"]) != workspace
                    or (await tools.read_file(path)).encode() != content
                ):
                    raise ValueError("Verified source bytes changed before native ingestion")

            request_id = digest(
                {
                    "run": run["id"],
                    "source_tree": run["source_tree"],
                    "path": path,
                    "content": hashlib.sha256(content).hexdigest(),
                    "binding": digest(self.manager.settings(run)),
                }
            )[:32]
            result = await self.source(
                run,
                KoinonSourceInput(
                    request_id=request_id, filename=Path(path).name, content_base64=base64.b64encode(content).decode()
                ),
                before_effect=verify_source,
            )
            observed = await self.observe(run, "source", request_id, result["receipt"]["source_id"])
            sources.append({"path": path, "request_id": request_id, **observed["receipt"]})
        await self.manager.store.update(run["id"], knowledge_sources=sources)
        return {"provider": "koinon", "sources": sources}

    async def status(self, run: Data, kind: Literal["source", "memory"], request_id: str) -> Data:
        if not re.fullmatch(r"[a-f0-9]{32}", request_id):
            raise ValueError("Use the original native ingestion request identity")
        settings = self.manager.settings(run)
        if settings.get("provider") != "koinon":
            raise ValueError("Native ingestion requires its frozen Koinon binding")
        verify = await self.manager.platform.authority(run["repository_id"], settings, run)
        state = await self.ledger(kind).state(run, request_id)
        await verify()
        return {
            "request_id": request_id,
            "kind": kind,
            "status": "not_found" if state is None else "outcome_unknown" if state["receipt"] is None else "recorded",
            "receipt": state["receipt"] if state else None,
            "read_started_no_remote_request": True,
        }

    async def observe(self, run: Data, kind: Literal["source", "memory"], request_id: str, remote_id: str) -> Data:
        try:
            return await self._observe(run, kind, request_id, remote_id)
        except (ApiError, httpx.HTTPError, ValidationError):
            raise ValueError(
                "Native ingestion observation is unavailable; retain the original outcome without resending"
            ) from None

    async def _observe(self, run: Data, kind: Literal["source", "memory"], request_id: str, remote_id: str) -> Data:
        if not re.fullmatch(r"[a-f0-9]{32}", request_id) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", remote_id):
            raise ValueError("Use the original operation and exact native remote identity")
        settings = self.manager.settings(run)
        if settings.get("provider") != "koinon":
            raise ValueError("Native ingestion requires its frozen Koinon binding")
        platform = self.manager.platform
        verify = await platform.authority(run["repository_id"], settings, run, permission="edit")
        state = await self.ledger(kind).state(run, request_id)
        if state is None:
            raise ValueError("Native ingestion has no original durable intent")
        bound = state["intent"]["payload"]
        scope = platform.scope(settings, run["repository_id"])
        async with platform.client(settings) as client:
            await platform.grant(client, settings, scope, "catalog:read")
            await verify()
            if kind == "memory":
                if state["receipt"] and state["receipt"]["review_id"] != remote_id:
                    raise ValueError("Observe the original saved native memory review")
                review = await client.external_review(settings["tenant_id"], remote_id)
                context = await client.context()
                evidence = self.memory_receipt(review, bound, settings, scope, context.subject)
                if evidence["commit_receipt"] is not None:
                    receipt = evidence["commit_receipt"]
                    item = await client.knowledge_version(settings["tenant_id"], receipt["item_id"], receipt["version"])
                    version = item.selected_version
                    if (
                        item.id != receipt["item_id"]
                        or item.tenant_id != settings["tenant_id"]
                        or item.workspace_id != scope.workspace_id
                        or item.project_id != scope.project_id
                        or item.destination_id != scope.collection_id
                        or version.item_id != receipt["item_id"]
                        or version.version != receipt["version"]
                        or version.version_id != receipt["native_version_id"]
                        or version.content_sha256 != bound["content_sha256"]
                        or digest(version.content.model_dump(mode="json")) != bound["content_sha256"]
                        or version.evidence_sha256 != receipt["evidence_sha256"]
                        or digest([value.model_dump(mode="json") for value in version.evidence])
                        != receipt["evidence_sha256"]
                        or version.operation_id != receipt["operation_id"]
                    ):
                        raise AccessDenied("Native memory receipt differs from its actual versioned content")
                    evidence["serving_verified"] = bool(
                        version.publication == "published"
                        and version.serving
                        and item.serving_version == receipt["version"]
                    )

            else:
                from firefly_forge.knowledge_native_sources import observe_source

                if state["receipt"] and state["receipt"]["source_id"] != remote_id:
                    raise ValueError("Observed source identity differs from the saved upload receipt")
                await platform.grant(client, settings, scope, "knowledge:search")
                evidence = await observe_source(
                    platform,
                    client,
                    settings,
                    scope,
                    remote_id,
                    bound["filename"],
                    bound["content_sha256"],
                    verify,
                    (state["receipt"] or {}).get("asset_ids"),
                )
            if self.manager.safe(json.dumps(evidence), settings) != json.dumps(evidence):
                raise ValueError("Native observed identity contains a registered credential")
            await platform.grant(client, settings, scope, "catalog:read")
            await verify()
        actor = CURRENT_PRINCIPAL.get()
        evidence.update(
            operation_source_tree=bound["source_tree"],
            binding_sha256=bound["binding_sha256"],
            current_source_matches=bound["source_tree"] == run.get("source_tree"),
            observer=ProjectAccess.actor_snapshot(actor) if actor is not None else run.get("execution_actor"),
        )
        if state["receipt"] is None:
            completed = await self.ledger(kind).state(run, request_id, receipt=evidence)
            assert completed is not None
        journal = self.manager.journal
        assert journal is not None
        await self.manager.store.assert_ownership()
        async with self.manager.store._unit():
            await journal._run(run["id"])
            await self.ledger(kind).state(run, request_id)
            await journal._append(
                run["id"],
                "knowledge",
                digest({"request": request_id, "observation": evidence}),
                "koinon_" + kind + "_observation",
                evidence,
            )
            if (
                kind == "memory"
                and evidence.get("commit_receipt")
                and evidence.get("serving_verified")
                and evidence["current_source_matches"]
            ):
                receipt = evidence["commit_receipt"]
                canonical = {
                    "provider": "koinon",
                    "item_id": receipt["item_id"],
                    "version": receipt["version"],
                    "native_item_id": receipt["native_item_id"],
                    "native_version_id": receipt["native_version_id"],
                    "workspace_id": scope.workspace_id,
                    "project_id": scope.project_id,
                    "origin": "external_proposal",
                    "authority": "unverified",
                    "content_sha256": bound["content_sha256"],
                    "review_id": remote_id,
                    "request_id": request_id,
                    "source_tree": bound["source_tree"],
                    "binding_sha256": bound["binding_sha256"],
                }
                await self.manager.store.update(run["id"], knowledge_memory=canonical, knowledge_memory_status="saved")
        await verify()
        return {"status": "observed", "request_id": request_id, "receipt": evidence}

    @staticmethod
    def memory_receipt(view: Any, bound: Data, settings: Data, scope: Any, owner_subject: str) -> Data:
        review = view.review
        if review.id != view.id or review.owner_subject != owner_subject or review.status != view.status:
            raise AccessDenied("Native memory review owner or identity changed")
        if (
            review.tenant_id != settings["tenant_id"]
            or review.destination.id != scope.collection_id
            or review.destination.workspace_id != scope.workspace_id
            or review.destination.project_id != scope.project_id
            or digest(review.after.model_dump(mode="json")) != bound["content_sha256"]
            or review.action != bound["action"]
            or any(
                x.kind != "external_proposal"
                or getattr(x, "sha256", None) != bound["body_sha256"]
                or getattr(x, "authority", None) != "unverified"
                for x in review.evidence
            )
        ):
            raise AccessDenied("Native memory review changed its source, origin or destination")
        receipt = review.receipt
        if view.status == "committed" and receipt is None:
            raise AccessDenied("Committed native memory has no actual receipt")
        if receipt is not None and (
            view.status != "committed"
            or receipt.intent_id != review.id
            or receipt.intent_revision != review.revision
            or receipt.authorization_digest != review.authorization_digest
            or receipt.item_id != review.target_item_id
            or receipt.action != bound["action"]
            or receipt.content_sha256 != bound["content_sha256"]
            or receipt.evidence_sha256 != digest([item.model_dump(mode="json") for item in review.evidence])
        ):
            raise AccessDenied("Native memory commit receipt changed its admitted content or evidence")
        return {
            "review_id": view.id,
            "status": view.status,
            "intent_id": review.id,
            "authorization_digest": review.authorization_digest,
            "revision": review.revision,
            "origin": "external_proposal",
            "authority": "unverified",
            "content_sha256": bound["content_sha256"],
            "commit_receipt": review.receipt.model_dump(mode="json") if review.receipt else None,
        }

    async def remember(self, run: Data, *, automatic: bool) -> Data:
        settings = self.manager.settings(run)
        if automatic and not settings["auto_memory"]:
            return {"skipped": True}
        if self.manager.engine is None:
            raise ValueError("Verified memory requires the factory engine")
        await self.manager.engine.validated_workspace(run["id"])
        if (
            run.get("status") != "succeeded"
            or not run.get("final_verification")
            or any(item.get("exit_code") != 0 for item in run["final_verification"])
        ):
            raise ValueError("Memory proposals require successful source and final verification evidence")
        scope = self.manager.platform.scope(settings, run["repository_id"])
        if scope.collection_id is None:
            raise ValueError("Map a Koinon collection before staging verified memory")
        verify = await self.manager.platform.authority(run["repository_id"], settings, run, permission="edit")
        content = KnowledgeContent(
            title="Verified forge run " + run["id"],
            domain="engineering",
            body=self.manager.safe(
                json.dumps(
                    {
                        "run_id": run["id"],
                        "source_tree": run["source_tree"],
                        "summary": run.get("summary"),
                        "changed_files": run.get("changed_files", []),
                        "verification": run["final_verification"],
                    },
                    ensure_ascii=False,
                ),
                settings,
            ),
            summary="Proposal derived from retained source and successful final checks",
            tags=("forge", "verified-run"),
        )
        saved = run.get("knowledge_memory", {})
        if (
            saved.get("provider") == "koinon"
            and saved.get("source_tree") == run.get("source_tree")
            and saved.get("binding_sha256") == digest(settings)
            and saved.get("content_sha256") == digest(content.model_dump(mode="json"))
        ):
            await verify()
            return dict(saved)
        request_id = digest(
            {"run": run["id"], "content": content.model_dump(mode="json"), "binding": digest(settings)}
        )[:32]
        bound = self.binding(
            run,
            settings,
            request_id,
            content_sha256=digest(content.model_dump(mode="json")),
            origin="external_proposal",
            action="create_published",
            body_sha256=hashlib.sha256(content.body.encode()).hexdigest(),
        )
        if await self.ledger("memory").state(run, request_id) is None:
            async with self.manager.platform.client(settings) as client:
                await self.manager.platform.grant(client, settings, scope, "ingest:write")
                await self.manager.platform.bind(client, settings, scope)
                await verify()
        state = await self.ledger("memory").state(run, request_id, create=bound)
        assert state is not None
        if state["dispatch"]:
            try:
                async with self.manager.platform.client(settings) as client:
                    await self.manager.platform.grant(client, settings, scope, "ingest:write")
                    await self.manager.platform.bind(client, settings, scope)
                    await verify()
                    view = await client.stage_external_review(
                        settings["tenant_id"],
                        ExternalReviewInput(
                            action="create_published", destination_id=scope.collection_id, content=content
                        ),
                        idempotency_key=request_id,
                    )
                    context = await client.context()
                    evidence = self.memory_receipt(view, bound, settings, scope, context.subject)
                    completed = await self.ledger("memory").state(run, request_id, receipt=evidence)
                    assert completed is not None
                    state = completed
                    await self.manager.platform.grant(client, settings, scope, "ingest:write")
                    await verify()
            except (ApiError, httpx.HTTPError, ValidationError):
                raise ValueError(
                    "Memory proposal outcome is uncertain; observe its native review without restaging"
                ) from None
        elif state["receipt"] is None:
            raise ValueError("Memory proposal outcome is uncertain; observe its native review without restaging")
        await verify()
        remote_status = state["receipt"]["status"]
        result_status = (
            "review_required"
            if remote_status == "preview"
            else "outcome_unknown"
            if remote_status in {"committing", "unresolved"}
            else "observe_required"
        )
        result = {
            "status": result_status,
            "request_id": request_id,
            "source_tree": run["source_tree"],
            "binding_sha256": digest(settings),
            "receipt": state["receipt"],
            "next_action": (
                "Review this external proposal in the upstream private application; "
                "no approval nonce is exposed by the SDK"
            ),
        }
        journal = self.manager.journal
        assert journal is not None
        async with self.manager.store._unit():
            await journal._run(run["id"])
            await self.ledger("memory").state(run, request_id)
            await self.manager.store.update(
                run["id"], knowledge_memory_proposal=result, knowledge_memory_status=result_status
            )
        await verify()
        return result
