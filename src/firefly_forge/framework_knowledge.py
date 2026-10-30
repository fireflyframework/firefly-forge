# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Official, version-isolated framework knowledge with native durable authority."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from typing import Any, cast

import httpx
from pyfly.data.relational.sqlalchemy import LockMode

from firefly_forge.framework_knowledge_models import (
    CURRENT_COLLECTOR_REVISION,
    OFFICIAL_REPOSITORIES,
    FrameworkCheckUpdatesInput,
    FrameworkCountsV1,
    FrameworkName,
    FrameworkRefreshInput,
    FrameworkSearchInput,
    validate_collector_revision,
)
from firefly_forge.framework_knowledge_persistence import FrameworkKnowledgeRecord, FrameworkKnowledgeRecords
from firefly_forge.identity_persistence import ForgeIdentityUserRepository
from firefly_forge.product_provenance import content_digest as digest
from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied, ProjectAccess
from firefly_forge.prompts import prompt_text
from firefly_forge.store import Store, identifier

Data = dict[str, Any]


class FrameworkKnowledge:
    from pyfly.kernel.lifecycle import CONSUMER_PHASE

    phase = CONSUMER_PHASE + 15

    def __init__(
        self,
        store: Store,
        knowledge: Any,
        records: FrameworkKnowledgeRecords | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.store, self.knowledge = store, knowledge
        self.records = records or FrameworkKnowledgeRecords()
        self.transport = transport
        self.poller: asyncio.Task[None] | None = None
        self.active: dict[str, asyncio.Task[None]] = {}

    async def _authorize(self, *, admin: bool = False, actor: Data | None = None) -> Data:
        from firefly_forge.framework_knowledge_bootstrap import (
            CURRENT_MANAGED_FRAMEWORK_GRANT,
            authorize_managed_framework,
        )

        prepared = CURRENT_MANAGED_FRAMEWORK_GRANT.get()
        if actor is None and CURRENT_PRINCIPAL.get() is None and prepared is not None:
            actor = prepared.actor
        if actor is not None and actor.get("authentication_method") == "managed_framework_knowledge":
            return await authorize_managed_framework(self, actor)
        if actor is None:
            principal = CURRENT_PRINCIPAL.get()
            if principal is None or principal.authentication_method == "scm_mapping":
                raise AccessDenied("A current native identity is required for official knowledge")
            actor = ProjectAccess.actor_snapshot(principal)
        async with self.store._unit():
            row = await ForgeIdentityUserRepository().find_by_id(
                identifier(actor["user_id"]), lock=LockMode.PESSIMISTIC_WRITE
            )
            if row is not None and not row.enabled:
                raise AccessDenied("The knowledge identity is suspended")
            if admin or actor.get("global_admin"):
                if not await ProjectAccess(self.store).authorize_actor(actor, None, "members"):
                    raise AccessDenied("A current native administrator is required for framework updates")
            elif row is None:
                raise AccessDenied("The knowledge identity is unavailable")
        return copy.deepcopy(actor)

    @staticmethod
    def _framework(framework: str) -> FrameworkName:
        if framework not in OFFICIAL_REPOSITORIES:
            raise ValueError("Choose an official supported framework")
        return cast(FrameworkName, framework)

    async def _put(self, kind: str, key: str, framework: str, data: Data, parent: str = "") -> Data:
        row = await self.records.by_key(kind, key, lock=True)
        if row is None:
            row = FrameworkKnowledgeRecord(
                id=identifier(uuid.uuid4().hex),
                kind=kind,
                key=key,
                parent=parent,
                framework=framework,
                source_ref=data.get("source_ref", ""),
                revision=1,
                data=copy.deepcopy(data),
            )
        else:
            row.data, row.revision = copy.deepcopy(data), row.revision + 1
        await self.records.save(row)
        return copy.deepcopy(data)

    @staticmethod
    def operation_actions(record: Data) -> list[str]:
        public = record["public"]
        if public["state"] in {"ready", "failed"}:
            return []
        if public["state"] == "cancelled":
            ready = (
                not public["uncertain_sources"]
                and not record.get("owner")
                and all(v > 0 for v in public["remaining_budget"].values())
            )
            return ["observe", "renew_budget"] + (["resume"] if ready else [])
        return ["observe", "renew_budget", "cancel"]

    async def _get(self, kind: str, key: str) -> Data | None:
        async with self.store._unit():
            row = await self.records.by_key(kind, key)
            return copy.deepcopy(row.data) if row else None

    async def _status(self, framework: FrameworkName) -> Data:
        saved = await self._get("status", framework) or {}
        corpus = await self._get("corpus", saved["current_corpus"]) if saved.get("current_corpus") else None
        current = corpus["public"] if corpus and corpus["public"]["state"] == "ready" else None
        operation = await self._get("operation", saved["active_operation"]) if saved.get("active_operation") else None
        async with self.store._unit():
            has_ready = await self.records.ready_exists(framework)
        latest = saved.get("latest_stable")
        activated = (
            await self._get("generation", saved["generation_target"]) if saved.get("generation_target") else None
        )
        state = "missing"
        if current:
            state = (
                "indexed"
                if not latest
                else "current"
                if (latest["commit"], latest["framework_version"]) == (current["commit"], current["framework_version"])
                else "update_available"
            )
        if operation and operation["public"]["state"] not in {"ready", "cancelled", "failed"}:
            state = "needs_reconciliation" if operation["public"]["state"] == "needs_reconciliation" else "refreshing"
        result = dict(
            framework=framework,
            official_repository=OFFICIAL_REPOSITORIES[framework],
            current_corpus=current,
            latest_stable=latest,
            generation_target=activated["public"] if activated else None,
            discovery_retry_at=saved.get("discovery_retry_at"),
            discovery_error=saved.get("discovery_error"),
            checked_at=saved.get("checked_at"),
            state=state,
            counts=copy.deepcopy(corpus["counts"]) if current and corpus else FrameworkCountsV1().model_dump(),
            active_operation=await self._public_operation(operation) if operation else None,
            allowed_actions=["check_updates"]
            + (["activate_generation_target"] if has_ready else [])
            + (["refresh"] if not operation or operation["public"]["state"] in {"ready", "cancelled", "failed"} else [])
            + (["search", "browse"] if current else []),
        )
        result["revision"] = digest(result)
        return result

    async def status(self) -> Data:
        actor = await self._authorize()
        result: Data = {
            "version": 1,
            "frameworks": [await self._status(self._framework(f)) for f in OFFICIAL_REPOSITORIES],
        }
        await self._authorize(actor=actor)
        if not actor.get("global_admin"):
            for item in result["frameworks"]:
                item["allowed_actions"] = [a for a in item["allowed_actions"] if a in {"search", "browse"}]
                if item["active_operation"]:
                    item["active_operation"]["allowed_actions"] = []
                item["revision"] = digest({k: v for k, v in item.items() if k != "revision"})
        return result

    async def _page(self, framework: str, kind: str, parent: str, cursor: str | None, page_size: int) -> Data:
        actor = await self._authorize()
        self._framework(framework)
        if (
            type(page_size) is not int
            or not 1 <= page_size <= 200
            or (
                cursor is not None and (len(cursor) not in {32, 64} or any(c not in "0123456789abcdef" for c in cursor))
            )
        ):
            raise ValueError("Choose a valid inventory response window and opaque cursor")
        async with self.store._unit():
            rows = await self.records.page(kind, parent, cursor, page_size + 1)
            total = await self.records.total(kind, parent)
            values = [copy.deepcopy(row.data["public"]) for row in rows[:page_size]]
            next_cursor = rows[page_size - 1].key if len(rows) > page_size else None
        await self._authorize(actor=actor)
        return {"items": values, "next_cursor": next_cursor, "total": total}

    async def corpora(self, framework: str, *, cursor: str | None = None, page_size: int = 50) -> Data:
        return await self._page(framework, "corpus", framework, cursor, page_size)

    async def sources(self, framework: str, corpus_ref: str, *, cursor: str | None = None, page_size: int = 50) -> Data:
        corpus = await self._get("corpus", corpus_ref)
        if corpus is None or corpus["public"]["framework"] != self._framework(framework):
            raise KeyError("Official framework corpus not found")
        return await self._page(framework, "source", corpus_ref, cursor, page_size)

    async def _resolve(self, framework: FrameworkName, body: FrameworkSearchInput) -> Data:
        if body.corpus_ref:
            corpus = await self._get("corpus", body.corpus_ref)
        else:
            async with self.store._unit():
                rows = await self.records.ready_version(framework, body.framework_version)
                corpus = copy.deepcopy(rows[0].data) if len(rows) == 1 else None
        if corpus is None or corpus["public"]["framework"] != framework or corpus["public"]["state"] != "ready":
            raise ValueError("Select one exact ready indexed official framework corpus")
        return corpus

    async def search(self, framework: str, body: FrameworkSearchInput) -> Data:
        actor = await self._authorize()
        corpus = await self._resolve(self._framework(framework), body)

        async def guard() -> None:
            await self._authorize(actor=actor)

        return await self._search_corpus(corpus, body, guard)

    async def _search_corpus(
        self,
        corpus: Data,
        body: FrameworkSearchInput,
        guard: Callable[[], Awaitable[None]],
        *,
        source_ids: list[str] | None = None,
        part_refs: set[str] | None = None,
    ) -> Data:
        from firefly_forge.framework_knowledge_sync import canonical_part_ref

        public = corpus["public"]
        settings = self.knowledge.settings({"knowledge_snapshot": corpus["knowledge_snapshot"]})
        if settings.get("provider") == "koinon":
            from firefly_forge.framework_knowledge_koinon import search

            return await search(self, corpus, body, guard, source_ids=source_ids, part_refs=part_refs)
        if not settings["enabled"] or not settings["api_key"]:
            raise ValueError("Version-isolated official knowledge requires its configured platform search authority")
        await guard()
        async with self.knowledge.client(settings, public["workspace_id"]) as client:
            result = await client.search(
                body.query[:2000],
                top_k=settings["top_k"],
                **({"source_ids": source_ids} if source_ids is not None else {}),
            )
            hits: list[Data] = []
            validated_sources: dict[str, Any] = {}
            for hit in result.hits:
                if source_ids is not None and hit.source_id not in source_ids:
                    raise ValueError("Search returned evidence outside its pre-ranking source filter")
                if not __import__("math").isfinite(hit.score):
                    raise ValueError("Search evidence must have finite relevance scores")
                async with self.store._unit():
                    rows = await self.records.remote_source(public["corpus_ref"], hit.source_id)
                    parts = [copy.deepcopy(row.data) for row in rows]
                if not parts or any(part["state"] != "ready" for part in parts):
                    raise ValueError("Retrieved citation is outside the immutable admitted corpus membership")
                if hit.source_id not in validated_sources:
                    await guard()
                    remote = await client.get_source(hit.source_id)
                    if remote.id != hit.source_id or remote.status not in {"ingested", "ready", "completed"}:
                        raise ValueError("Retrieved citation source differs from its immutable admitted official part")
                    await guard()
                    validated_sources[hit.source_id] = remote
                remote = validated_sources[hit.source_id]
                canonical = next(
                    (part for part in parts if part["part_ref"] == remote.metadata.extra.get("forge_framework_part")),
                    None,
                )
                for candidate in parts:
                    canonical_part_ref(remote, candidate, canonical)
                candidates = [part for part in parts if part_refs is None or part["part_ref"] in part_refs]
                if not candidates:
                    raise ValueError("Retrieved citation is outside the admitted reference selection")
                part = canonical if part_refs is None else min(candidates, key=lambda item: item["part_ref"])
                if part is None:
                    raise ValueError("Retrieved canonical source has no admitted native reference")
                source = await self._get("source", part["source_ref"])
                if source is None or source["public"]["state"] != "ready":
                    raise ValueError("Retrieved official source is not completely indexed")
                entry = source["public"]
                hits.append(
                    dict(
                        kind=entry["kind"],
                        license_path=source.get("license_path"),
                        source_ref=entry["source_ref"],
                        corpus_ref=public["corpus_ref"],
                        framework=public["framework"],
                        framework_version=public["framework_version"],
                        release_tag=public["release_tag"],
                        commit=public["commit"],
                        source_id=hit.source_id,
                        chunk_id=hit.chunk_id,
                        path=entry["path"],
                        source_uri=entry["source_uri"],
                        source_sha256=entry["sha256"],
                        license_sha256=entry["license_sha256"],
                        knowledge_version=hit.knowledge_version,
                        score=hit.score,
                        content=self.knowledge.safe(hit.content[:2000], settings),
                    )
                )
        await guard()
        return {
            "corpus": public,
            "hits": hits,
            "citations": [{k: v for k, v in h.items() if k != "content"} for h in hits],
            "warnings": [],
            "context": prompt_text("tools/knowledge-evidence", hits=json.dumps(hits, ensure_ascii=False))
            if hits
            else "",
        }

    @staticmethod
    def policy() -> Data:
        import os

        values = {}
        for key, default in (("requests", 100_000), ("bytes", 1_073_741_824), ("seconds", 86_400)):
            value = int(os.environ.get("FORGE_FRAMEWORK_KNOWLEDGE_" + key.upper(), str(default)))
            if value <= 0:
                raise ValueError("Configure positive finite framework knowledge work budgets")
            values[key] = value
        return {
            "version": 1,
            "collector_revision": CURRENT_COLLECTOR_REVISION,
            "budget": values,
            "part_bytes": 65_536,
            "official_repositories": OFFICIAL_REPOSITORIES,
        }

    def _connection_snapshot(self) -> tuple[Data, str]:
        from firefly_forge.knowledge import snapshot_knowledge

        settings = self.knowledge.settings()
        if settings.get("provider") == "koinon":
            from firefly_forge.framework_knowledge_koinon import framework_scope

            framework_scope(settings)
            return snapshot_knowledge(self.store.configuration), digest(settings)
        if not settings["enabled"] or not settings["endpoint"] or not settings["api_key"]:
            raise ValueError("Initialize platform-authorized flycanon before importing official references")
        return snapshot_knowledge(self.store.configuration), digest(settings)

    async def _event(self, framework: str, kind: str, values: Data, parent: str = "") -> None:
        from firefly_forge.store import now

        await self._put("event", uuid.uuid4().hex, framework, {"kind": kind, "at": now(), **values}, parent)

    async def _lock(self, session: Any, framework: str) -> None:
        from sqlalchemy import text

        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": self.store.schema + ":framework-knowledge:" + framework},
        )

    async def _load_tree(self, framework: str, oid: str) -> list[Data] | None:
        selected = self._framework(framework)
        key = digest({"repository": OFFICIAL_REPOSITORIES[selected], "tree": oid})
        value = await self._get("tree", key)
        if value is None:
            return None
        if value.get("repository") != OFFICIAL_REPOSITORIES[selected] or value.get("oid") != oid:
            raise ValueError("Retained official tree cache has a different immutable scope")
        return cast(list[Data], value["entries"])

    async def _save_tree(self, framework: str, oid: str, entries: list[Data]) -> None:
        from firefly_forge.framework_knowledge_upstream import OfficialFrameworkClient

        selected = self._framework(framework)
        verified = OfficialFrameworkClient._verified_tree(oid, entries)
        key = digest({"repository": OFFICIAL_REPOSITORIES[selected], "tree": oid})
        value = {"repository": OFFICIAL_REPOSITORIES[selected], "oid": oid, "entries": verified}
        async with self.store._unit() as session:
            await self._lock(session, selected)
            existing = await self._get("tree", key)
            if existing is not None and existing != value:
                raise ValueError("Immutable official tree cache content cannot be replaced")
            if existing is None:
                await self._put("tree", key, selected, value, selected)

    async def check_updates(self, framework: str, body: FrameworkCheckUpdatesInput) -> Data:
        import time
        from datetime import UTC, datetime

        from firefly_forge.framework_knowledge_upstream import OfficialFrameworkClient, OfficialRateLimited

        selected = self._framework(framework)
        actor = await self._authorize(admin=True)
        policy = self.policy()
        request_id = uuid.uuid4().hex
        work = {
            "actor": actor,
            "selection": body.framework_version,
            "policy": policy,
            "state": "running",
            "spent_budget": {"requests": 0, "bytes": 0, "seconds": 0.0},
        }
        async with self.store._unit() as session:
            await self._lock(session, selected)
            await self._authorize(admin=True, actor=actor)
            saved = await self._get("status", selected) or {}
            if saved.get("discovery_retry_at") and datetime.fromisoformat(saved["discovery_retry_at"]) > datetime.now(
                UTC
            ):
                raise ValueError("Respect the retained official release rate-limit retry time")
            saved["last_check_attempt"] = datetime.now(UTC).isoformat()
            await self._put("status", selected, selected, saved)
            await self._put("discoverywork", request_id, selected, work, selected)
            await self._event(
                selected,
                "discovery_intent",
                {
                    "request_id": request_id,
                    "actor": actor,
                    "selection": body.framework_version,
                    "policy_digest": digest(policy),
                },
            )
        last = time.monotonic()

        async def current() -> None:
            await self._authorize(admin=True, actor=actor)

        async def spend(requests: int = 0, byte_count: int = 0) -> None:
            nonlocal last
            delta = time.monotonic() - last
            async with self.store._unit() as session:
                await self._lock(session, selected)
                await current()
                retained = await self._get("discoverywork", request_id)
                if retained is None or retained["state"] != "running" or retained["actor"] != actor:
                    raise ValueError("Official discovery no longer owns its native work identity")
                spent = retained["spent_budget"]
                total = policy["budget"]
                exhausted = spent["requests"] + requests > total["requests"]
                spent["seconds"] += delta
                spent["bytes"] += byte_count
                if not exhausted:
                    spent["requests"] += requests
                exhausted = exhausted or any(spent[k] > total[k] for k in total)
                await self._put("discoverywork", request_id, selected, retained, selected)
            last = time.monotonic()
            if exhausted:
                raise ValueError("Official release discovery work allowance is exhausted")

        try:
            async with OfficialFrameworkClient(
                transport=self.transport,
                assert_current=current,
                spend=spend,
                load_tree=self._load_tree,
                save_tree=self._save_tree,
            ) as upstream:
                release = (await upstream.discover(selected, body.framework_version)).model_dump()
            await spend()
            async with self.store._unit() as session:
                await self._lock(session, selected)
                await current()
                await self._put(
                    "discovery",
                    release["digest"],
                    selected,
                    {"release": release, "policy_digest": digest(policy)},
                    selected,
                )
                saved = await self._get("status", selected) or {}
                saved["checked_at"] = release["checked_at"]
                saved.pop("discovery_retry_at", None)
                saved.pop("discovery_error", None)
                if body.framework_version == "latest":
                    saved["latest_stable"] = release
                await self._put("status", selected, selected, saved)
                retained = await self._get("discoverywork", request_id)
                if retained is None:
                    raise ValueError("Native discovery work identity is unavailable")
                retained["state"] = "complete"
                await self._put("discoverywork", request_id, selected, retained, selected)
                await self._event(
                    selected,
                    "discovery_outcome",
                    {
                        "request_id": request_id,
                        "state": "complete",
                        "release_digest": release["digest"],
                        "spent_budget": retained["spent_budget"],
                    },
                )
        except (Exception, asyncio.CancelledError) as error:
            async with self.store._unit() as session:
                await self._lock(session, selected)
                retained = await self._get("discoverywork", request_id)
                if retained is None:
                    raise ValueError("Native discovery work identity is unavailable") from error
                retained["spent_budget"]["seconds"] += max(0, time.monotonic() - last)
                retained.update(state="held", error_type=type(error).__name__)
                retry_at = error.retry_at if isinstance(error, OfficialRateLimited) else None
                await self._put("discoverywork", request_id, selected, retained, selected)
                saved = await self._get("status", selected) or {}
                saved["discovery_error"] = "Official release discovery is held; review its retained work outcome"
                if retry_at:
                    saved["discovery_retry_at"] = retry_at
                await self._put("status", selected, selected, saved)
                await self._event(
                    selected,
                    "discovery_outcome",
                    {
                        "request_id": request_id,
                        "state": "held",
                        "spent_budget": retained["spent_budget"],
                        "retry_at": retry_at,
                        "sanitized_error": saved["discovery_error"],
                    },
                )
            raise
        result: Data = {
            "version": 1,
            "framework": selected,
            "release": release,
            "status": await self._status(selected),
            "policy_digest": digest(policy),
        }
        await self._authorize(admin=True, actor=actor)
        return result

    async def refresh(self, framework: str, request_id: str, body: FrameworkRefreshInput) -> Data:
        from firefly_forge.store import now

        selected = self._framework(framework)
        identifier(request_id)
        actor = await self._authorize(admin=True)
        existing = await self._get("operation", request_id)
        body_digest = digest(body.model_dump())
        if existing:
            if existing["body_digest"] != body_digest or existing["public"]["framework"] != selected:
                raise ValueError("Retained refresh identity has a different reviewed request")
            return cast(Data, existing["public"])
        policy = self.policy()
        discovery = await self._get("discovery", body.discovery_digest)
        status = await self._status(selected)
        if (
            discovery is None
            or discovery["release"]["framework"] != selected
            or discovery["policy_digest"] != body.policy_digest
            or digest(policy) != body.policy_digest
        ):
            raise ValueError("Review the exact official release and current import policy before refreshing")
        if status["revision"] != body.expected_status_revision:
            raise ValueError("Review the changed official framework status before refreshing")
        snapshot, connection_digest = self._connection_snapshot()
        release = discovery["release"]
        corpus_ref = digest(
            {
                "framework": selected,
                "commit": release["commit"],
                "version": release["framework_version"],
                "connection": connection_digest,
                "collector": validate_collector_revision(policy.get("collector_revision", 1)),
                "part_bytes": policy["part_bytes"],
            }
        )[:32]
        public_corpus = dict(
            collector_revision=validate_collector_revision(policy.get("collector_revision", 1)),
            corpus_ref=corpus_ref,
            framework=selected,
            framework_version=release["framework_version"],
            release_tag=release["release_tag"],
            commit=release["commit"],
            manifest_sha256=None,
            license_sha256=release["license_sha256"],
            workspace_id=(
                snapshot["framework_scope"]["workspace_id"]
                if snapshot.get("provider") == "koinon"
                else "ffw-" + selected + "-" + corpus_ref
            ),
            state="queued",
        )
        public = dict(
            request_id=request_id,
            framework=selected,
            corpus=public_corpus,
            state="queued",
            stage="inventory",
            counts=FrameworkCountsV1().model_dump(),
            spent_budget={"requests": 0, "bytes": 0, "seconds": 0.0},
            remaining_budget={**policy["budget"], "seconds": float(policy["budget"]["seconds"])},
            sanitized_error=None,
            uncertain_sources=0,
            allowed_actions=["observe"],
        )
        public["revision"] = digest(public)
        operation = {
            "public": public,
            "body_digest": body_digest,
            "actor": actor,
            "release": release,
            "policy": policy,
            "policy_digest": body.policy_digest,
            "knowledge_snapshot": snapshot,
            "connection_digest": connection_digest,
            "make_current": body.make_current,
            "created_at": now(),
        }
        async with self.store._unit() as session:
            await self._lock(session, selected)
            await self._authorize(admin=True, actor=actor)
            existing = await self._get("operation", request_id)
            if existing:
                if existing["body_digest"] != body_digest:
                    raise ValueError("Retained refresh identity has a different reviewed request")
                return cast(Data, existing["public"])
            fresh = await self._status(selected)
            if fresh["revision"] != body.expected_status_revision:
                raise ValueError("Framework status changed during refresh admission")
            active = fresh["active_operation"]
            if active and active["state"] not in {"ready", "failed", "cancelled"}:
                raise ValueError("Observe the retained active refresh before starting another")
            prior = await self._get("corpus", corpus_ref)
            if prior is not None:
                raise ValueError("The exact corpus already has durable custody; observe its original operation")
            await self._put(
                "corpus",
                corpus_ref,
                selected,
                {
                    "public": public_corpus,
                    "policy": copy.deepcopy(policy),
                    "policy_digest": body.policy_digest,
                    "release": release,
                    "knowledge_snapshot": snapshot,
                    "connection_digest": connection_digest,
                    "counts": FrameworkCountsV1().model_dump(),
                    "operation": request_id,
                },
                selected,
            )
            await self._put("operation", request_id, selected, operation, selected)
            saved = await self._get("status", selected) or {}
            saved["active_operation"] = request_id
            await self._put("status", selected, selected, saved)
            await self._event(
                selected,
                "refresh_intent",
                {
                    "request_id": request_id,
                    "body_digest": body_digest,
                    "release_digest": release["digest"],
                    "corpus_ref": corpus_ref,
                    "actor": actor,
                    "policy_digest": body.policy_digest,
                },
                request_id,
            )
        return copy.deepcopy(public)

    async def _public_operation(self, record: Data) -> Data:
        public: Data = copy.deepcopy(record["public"])
        if public.get("corpus") and public["state"] not in {"ready", "failed", "cancelled"}:
            async with self.store._unit():
                counts = await self.records.progress_counts(public["corpus"]["corpus_ref"])
            public["counts"] = FrameworkCountsV1.model_validate(counts).model_dump()
        return public

    async def get_refresh(self, framework: str, request_id: str) -> Data:
        actor = await self._authorize()
        record = await self._get("operation", request_id)
        if record is None or record["public"]["framework"] != self._framework(framework):
            raise KeyError("Official framework refresh not found")
        public = await self._public_operation(record)
        await self._authorize(actor=actor)
        if not actor.get("global_admin"):
            public["allowed_actions"] = []
        return public

    @asynccontextmanager
    async def _operation_scope(self, framework: str, request_id: str) -> AsyncIterator[None]:
        from firefly_forge.framework_knowledge_bootstrap import managed_framework_scope

        await self._authorize(admin=True)
        record = await self._get("operation", request_id)
        if record is None or record["public"]["framework"] != self._framework(framework):
            raise KeyError("Official framework refresh not found")
        if record["actor"].get("authentication_method") == "managed_framework_knowledge":
            async with managed_framework_scope(self, require_owner=False) as actor:
                if actor != record["actor"]:
                    raise AccessDenied("The retained managed installation authority changed")
                yield
        else:
            yield

    async def observe(self, framework: str, request_id: str) -> Data:
        async with self._operation_scope(framework, request_id):
            return await self._observe_selected(framework, request_id)

    async def _observe_selected(self, framework: str, request_id: str) -> Data:
        from firefly_forge.framework_knowledge_worker import FrameworkKnowledgeWorker

        actor = await self._authorize(admin=True)
        record = await self._get("operation", request_id)
        if record is None or record["public"]["framework"] != self._framework(framework):
            raise KeyError("Official framework refresh not found")
        if record["public"]["state"] == "cancelled" and not record["public"]["uncertain_sources"]:
            return await self.get_refresh(framework, request_id)
        await FrameworkKnowledgeWorker(self).advance(request_id, observe_only=True, observer=actor)
        return await self.get_refresh(framework, request_id)

    async def _operation_change(self, framework: str, request_id: str, body: Any, *, cancel: bool) -> Data:
        async with self._operation_scope(framework, request_id):
            return await self._change_selected(framework, request_id, body, cancel=cancel)

    async def _change_selected(self, framework: str, request_id: str, body: Any, *, cancel: bool) -> Data:
        selected = self._framework(framework)
        actor = await self._authorize(admin=True)
        body_digest = digest(body.model_dump())
        receipt_key = "cancel_receipt" if cancel else "budget_receipt"
        async with self.store._unit() as session:
            await self._lock(session, selected)
            await self._authorize(admin=True, actor=actor)
            record = await self._get("operation", request_id)
            if record is None or record["public"]["framework"] != selected:
                raise KeyError("Official framework refresh not found")
            await self._authorize(admin=True, actor=record["actor"])
            prior = record.get(receipt_key)
            if prior and prior["body_digest"] == body_digest:
                return cast(Data, prior["response"])
            public = record["public"]
            if public["revision"] != body.expected_revision:
                raise ValueError("Review the current retained refresh revision before changing its allowance")
            if public["state"] in {"ready", "failed"} or (cancel and public["state"] == "cancelled"):
                raise ValueError("Only retained unfinished refresh work can be renewed or cancelled")
            if cancel:
                record["cancel_requested"] = True
                if public.get("corpus"):
                    ref = public["corpus"]["corpus_ref"]
                    public["uncertain_sources"] = await self.records.count_state(
                        "part", ref, "submitting"
                    ) + await self.records.count_state("part", ref, "needs_reconciliation")
                public.update(state="cancelled")
            else:
                total = body.total_budget.model_dump()
                old = record["policy"]["budget"]
                spent = public["spent_budget"]
                if any(total[k] < old[k] or total[k] < spent[k] for k in total) or total == old:
                    raise ValueError("Explicit renewal must increase finite allowances and retain all spent work")
                record["policy"]["budget"] = total
                public["remaining_budget"] = {k: total[k] - spent[k] for k in total}
                if not public["uncertain_sources"] and not record.get("owner") and public["state"] != "cancelled":
                    public["state"] = "queued"
                public["allowed_actions"] = ["observe", "cancel", "renew_budget"]
            public["allowed_actions"] = self.operation_actions(record)
            public["revision"] = digest({k: v for k, v in public.items() if k != "revision"})
            record[receipt_key] = {"body_digest": body_digest, "response": copy.deepcopy(public)}
            await self._put("operation", request_id, selected, record, selected)
            await self._event(
                selected,
                "refresh_cancel" if cancel else "budget_renewal",
                {
                    "request_id": request_id,
                    "body_digest": body_digest,
                    "previous_revision": body.expected_revision,
                    "revision": public["revision"],
                    "spent_budget": public["spent_budget"],
                    "actor": actor,
                },
                request_id,
            )
        return cast(Data, copy.deepcopy(public))

    async def renew_budget(self, framework: str, request_id: str, body: Any) -> Data:
        return await self._operation_change(framework, request_id, body, cancel=False)

    async def cancel(self, framework: str, request_id: str, body: Any) -> Data:
        return await self._operation_change(framework, request_id, body, cancel=True)

    async def generation_target(self, framework: str, request_id: str) -> Data:
        actor = await self._authorize()
        identifier(request_id)
        record = await self._get("generation", request_id)
        if record is None or record["public"]["framework"] != self._framework(framework):
            raise KeyError("Framework generation target activation not found")
        await self._authorize(actor=actor)
        return cast(Data, record["public"])

    async def _generation_proof(self, session: Any, run: Data, corpus: Data, *, lock: bool) -> Data:
        from sqlalchemy import and_, or_, select

        from firefly_forge.framework_knowledge_generation import validate_generation_evidence
        from firefly_forge.sandbox_persistence import ForgeSandboxEntity

        await ProjectAccess(self.store).authorize_execution(run)
        await self.assert_run_pin(run)
        if run["framework_knowledge_pin"]["framework"] != corpus["public"]["framework"]:
            raise ValueError("Validation run names a different framework")
        journal = getattr(self.knowledge, "journal", None)
        if journal is None:
            raise ValueError("Generation validation requires canonical native journal receipts")
        tasks = await journal.tasks.find_for_run(identifier(run["id"]))
        verify_task = next((task for task in tasks if task.task_id == "verify" and task.status == "done"), None)
        if verify_task is None:
            raise ValueError("Generation validation requires its completed native QA task")
        attestation = run.get("framework_runtime_attestation")
        command = attestation.get("command") if isinstance(attestation, dict) else None
        runtime_id = command.get("execution_id") if isinstance(command, dict) else None
        if (
            not isinstance(runtime_id, str)
            or len(runtime_id) != 32
            or any(c not in "0123456789abcdef" for c in runtime_id)
        ):
            raise ValueError("Generation validation requires a native interpreter attestation")
        query = select(ForgeSandboxEntity).where(
            ForgeSandboxEntity.run_id == identifier(run["id"]),
            or_(
                ForgeSandboxEntity.id == identifier(runtime_id),
                and_(ForgeSandboxEntity.task_id == "verify", ForgeSandboxEntity.parent_attempt == verify_task.attempt),
            ),
        )
        if lock:
            query = query.with_for_update()
        native_rows = list(await session.scalars(query))
        entries = [copy.deepcopy(entry.data) for entry in await journal.records.find_for_run(identifier(run["id"]))]
        return validate_generation_evidence(run, entries, native_rows, corpus)

    async def generation_candidates(
        self, framework: str, corpus_ref: str, *, cursor: str | None = None, page_size: int = 50
    ) -> Data:
        from firefly_forge.framework_knowledge_models import (
            FrameworkGenerationCandidatesV1,
            FrameworkGenerationCandidateV1,
        )

        selected = self._framework(framework)
        actor = await self._authorize(admin=True)
        if (
            type(page_size) is not int
            or not 1 <= page_size <= 200
            or (
                cursor is not None
                and (
                    not isinstance(cursor, str) or len(cursor) != 32 or any(c not in "0123456789abcdef" for c in cursor)
                )
            )
        ):
            raise ValueError("Choose a valid candidate response window and opaque cursor")
        generation = self.store.configuration.settings_generation
        connection = digest(self.knowledge.settings())
        corpus = await self._resolve(
            selected, FrameworkSearchInput(query="generation validation candidates", corpus_ref=corpus_ref)
        )
        if corpus["connection_digest"] != connection:
            raise ValueError("Generation validation requires the current immutable corpus connection")
        items = []
        async with self.store._unit() as session:
            rows = await self.records.generation_runs(selected, corpus_ref, cursor, page_size + 1)
            for row in rows[:page_size]:
                run = copy.deepcopy(row.data)
                if run.get("id") != row.id.hex:
                    continue
                try:
                    proof = await self._generation_proof(session, run, corpus, lock=False)
                    items.append(
                        FrameworkGenerationCandidateV1(
                            validation_run_id=run["id"],
                            repository_name=run["repository_name"],
                            project_id=run.get("project_id"),
                            component_id=run.get("component_id"),
                            created_at=run["created_at"],
                            source_tree=proof["source_tree"],
                            runtime=run["framework_runtime_attestation"]["observed"],
                            validation_digest=digest(proof),
                        )
                    )
                except (AccessDenied, KeyError, TypeError, ValueError):
                    continue
            next_cursor = rows[page_size - 1].id.hex if len(rows) > page_size else None
            await self._authorize(admin=True, actor=actor)
            if generation is not self.store.configuration.settings_generation or connection != digest(
                self.knowledge.settings()
            ):
                raise ValueError("Framework connection changed during native generation candidate listing")
        return FrameworkGenerationCandidatesV1(
            framework=selected, corpus_ref=corpus_ref, items=items, next_cursor=next_cursor
        ).model_dump()

    async def activate_generation_target(self, framework: str, request_id: str, body: Any) -> Data:
        from firefly_forge.framework_knowledge_models import FrameworkGenerationTargetV1
        from firefly_forge.store import now

        selected = self._framework(framework)
        identifier(request_id)
        actor = await self._authorize(admin=True)
        body_digest = digest(body.model_dump())
        generation = self.store.configuration.settings_generation
        connection = digest(self.knowledge.settings())
        async with self.store._unit() as session:
            await self._lock(session, selected)
            await self._authorize(admin=True, actor=actor)
            existing = await self._get("generation", request_id)
            if existing is not None:
                if existing["body_digest"] != body_digest or existing["public"]["framework"] != selected:
                    raise ValueError("Generation activation identity already binds a different request")
                return cast(Data, existing["public"])
            status = await self._status(selected)
            if status["revision"] != body.expected_status_revision:
                raise ValueError("Review the current framework status before activating a generation target")
            corpus = await self._resolve(
                selected, FrameworkSearchInput(query="generation activation", corpus_ref=body.corpus_ref)
            )
            if corpus["connection_digest"] != connection:
                raise ValueError("Generation validation requires the current immutable corpus connection")
            run_row = await self.store.run_records.find_by_id(
                identifier(body.validation_run_id), lock=LockMode.PESSIMISTIC_WRITE
            )
            if run_row is None:
                raise ValueError("Select a completed native framework validation run")
            run = copy.deepcopy(run_row.data)
            if run.get("id") != run_row.id.hex:
                raise ValueError("Validation run differs from its canonical native record")
            proof = await self._generation_proof(session, run, corpus, lock=True)
            public = FrameworkGenerationTargetV1(
                request_id=request_id,
                framework=selected,
                corpus=corpus["public"],
                validation_run_id=run["id"],
                validation_source_tree=proof["source_tree"],
                validation_digest=digest(proof),
                activation_actor_digest=digest(actor),
                activated_at=now(),
                revision="0" * 64,
            ).model_dump()
            public["revision"] = digest({k: v for k, v in public.items() if k != "revision"})
            await self._event(
                selected,
                "generation_activation_intent",
                {"request_id": request_id, "body_digest": body_digest, "actor": actor, "proof_digest": digest(proof)},
                request_id,
            )
            await self._put(
                "generation",
                request_id,
                selected,
                {"body_digest": body_digest, "public": public, "proof": proof},
                selected,
            )
            saved = await self._get("status", selected) or {}
            saved["generation_target"] = request_id
            await self._put("status", selected, selected, saved)
            await self._event(
                selected,
                "generation_activation_outcome",
                {"request_id": request_id, "revision": public["revision"], "proof_digest": digest(proof)},
                request_id,
            )
            await self._authorize(admin=True, actor=actor)
            if run.get("execution_actor"):
                await self._authorize(actor=run["execution_actor"])
            if generation is not self.store.configuration.settings_generation:
                raise ValueError("Framework connection changed during native generation activation")
        return public

    async def prepare_target_request(self, fields: Data) -> Data | None:
        framework = fields["backend"]
        if framework not in OFFICIAL_REPOSITORIES or fields["operation"] == "test":
            return None
        from firefly_forge.framework_knowledge_targets import GENERATION_RELEASE_CANDIDATES

        saved = await self._get("status", framework) or {}
        activated = (
            await self._get("generation", saved["generation_target"]) if saved.get("generation_target") else None
        )
        explicit = fields.get("framework_target")
        if fields["operation"] == "build" and not explicit and activated is None:
            raise ValueError(
                "Activate an exact natively validated framework generation target before creating new projects"
            )
        supported = (
            explicit["framework_version"]
            if explicit
            else activated["public"]["corpus"]["framework_version"]
            if activated
            else GENERATION_RELEASE_CANDIDATES[framework]
        )
        return {
            "version": 1,
            "framework": framework,
            "operation": fields["operation"],
            "explicit": copy.deepcopy(fields.get("framework_target")),
            "supported_default": supported,
            "generation_activation": copy.deepcopy(activated["public"])
            if activated and not explicit and fields["operation"] == "build"
            else None,
            "validation_state": "requires_ready_corpus_native_runtime_and_product_checks",
        }

    async def freeze_run_target(self, run: Data, workspace: Any, commit: str, component: str) -> Data:
        from firefly_forge.framework_knowledge_targets import git_manifests, select_target

        request = run.get("framework_knowledge_request")
        if not request:
            return run
        await ProjectAccess(self.store).authorize_execution(run)
        files = await git_manifests(workspace, commit, self._framework(request["framework"]), component)
        version, upgrade_from = select_target(
            request["framework"],
            request["operation"],
            files,
            request["explicit"],
            supported_default=request["supported_default"],
        )
        activation = request.get("generation_activation")
        selector = (
            FrameworkSearchInput(query="framework target", corpus_ref=activation["corpus"]["corpus_ref"])
            if activation
            else FrameworkSearchInput(query="framework target", framework_version=version)
        )
        corpus = await self._resolve(request["framework"], selector)
        if activation and (
            corpus["public"] != activation["corpus"] or corpus["public"]["framework_version"] != version
        ):
            raise ValueError("The snapshotted generation activation differs from its immutable indexed corpus")
        if digest(self.knowledge.settings(run)) != corpus["connection_digest"]:
            raise ValueError("The retained run knowledge connection differs from its exact indexed corpus")
        public = corpus["public"]
        pin = {
            "version": 1,
            "run_id": run["id"],
            "repository_id": run["repository_id"],
            "project_id": run.get("project_id"),
            "framework": request["framework"],
            "framework_version": version,
            "upgrade_from": upgrade_from,
            "request_digest": digest(request),
            "source_commit": commit,
            "component": component,
            "manifests": {name: hashlib.sha256(body).hexdigest() for name, body in files.items()},
            "corpus_ref": public["corpus_ref"],
            "corpus_manifest_sha256": public["manifest_sha256"],
            "release_commit": public["commit"],
            "license_sha256": public["license_sha256"],
            "connection_digest": corpus["connection_digest"],
            "runtime_requirements": corpus["runtime_requirements"],
        }
        pin["digest"] = digest(pin)
        journal = getattr(self.knowledge, "journal", None)
        if journal is None:
            raise ValueError("Native framework target admission requires its canonical execution journal")
        async with self.store._unit():
            row = await self.store.run_records.find_by_id(identifier(run["id"]), lock=LockMode.PESSIMISTIC_WRITE)
            if (
                row is None
                or row.data.get("base_revision") != commit
                or row.data.get("framework_knowledge_request") != request
            ):
                raise ValueError("Frozen source or framework target changed during native admission")
            await ProjectAccess(self.store).authorize_execution(row.data)
            prior = await self._get("runpin", run["id"])
            if prior:
                if prior["pin"] != pin or row.data.get("framework_knowledge_pin") != pin:
                    raise ValueError("The immutable framework target cannot be changed by continuation")
            else:
                event = await journal._append(run["id"], "implement", pin["digest"], "framework_knowledge_pin", pin)
                await self._put(
                    "runpin",
                    run["id"],
                    request["framework"],
                    {"pin": pin, "event_digest": event["digest"]},
                    run["repository_id"],
                )
                row.data = {**row.data, "framework_knowledge_pin": pin}
                await self.store.run_records.save(row)
        return await self.store.get_run(run["id"])

    async def assert_run_pin(self, run: Data) -> Data:
        pin = run.get("framework_knowledge_pin")
        retained = await self._get("runpin", run["id"])
        if (
            not pin
            or retained is None
            or retained["pin"] != pin
            or digest({k: v for k, v in pin.items() if k != "digest"}) != pin["digest"]
        ):
            raise ValueError("A canonical retained framework target is required before model or knowledge use")
        if (
            pin["run_id"] != run["id"]
            or pin["repository_id"] != run["repository_id"]
            or pin["request_digest"] != digest(run.get("framework_knowledge_request"))
        ):
            raise ValueError("Framework target scope differs from the current execution")
        async with self.store._unit():
            event = await self.records.pin_event(run["id"], pin["digest"])
        if (
            event is None
            or event.digest != retained["event_digest"]
            or event.data.get("payload") != pin
            or digest({k: v for k, v in event.data.items() if k != "digest"}) != event.digest
        ):
            raise ValueError("Framework target lacks its exact canonical native journal receipt")
        if pin["source_commit"] != run.get("base_revision") or pin["project_id"] != run.get("project_id"):
            raise ValueError("Framework target differs from the retained project and source commit")
        corpus = await self._resolve(
            pin["framework"], FrameworkSearchInput(query="framework target", corpus_ref=pin["corpus_ref"])
        )
        if (
            corpus["public"]["manifest_sha256"] != pin["corpus_manifest_sha256"]
            or corpus["connection_digest"] != pin["connection_digest"]
            or corpus.get("runtime_requirements") != pin["runtime_requirements"]
            or corpus["public"]["framework_version"] != pin["framework_version"]
            or corpus["public"]["commit"] != pin["release_commit"]
        ):
            raise ValueError("The immutable indexed corpus does not match the retained execution target")
        return corpus

    async def search_for_run(self, run: Data, query: str) -> Data:
        corpus = await self.assert_run_pin(run)

        async def guard() -> None:
            await ProjectAccess(self.store).authorize_execution(run)
            await self.knowledge._assert_ownership()
            await self.assert_run_pin(run)

        native_settings = self.knowledge.settings({"knowledge_snapshot": corpus["knowledge_snapshot"]})
        native = native_settings.get("provider") == "koinon"
        body = FrameworkSearchInput(query=query[: 8000 if native else 2000], corpus_ref=corpus["public"]["corpus_ref"])
        result_limit = native_settings["top_k"] if native else 2
        hits: list[Data] = []
        for kind in ("document", "example", "api"):
            cursor = None
            best: dict[tuple[str, str | None], Data] = {}
            while True:
                async with self.store._unit():
                    rows = await self.records.kind_parts(corpus["public"]["corpus_ref"], kind, cursor, 200)
                    selected = [(row.key, row.data["source_id"]) for row in rows]
                if not selected:
                    break
                response = await self._search_corpus(
                    corpus,
                    body,
                    guard,
                    source_ids=[remote for _, remote in selected],
                    part_refs={reference for reference, _ in selected},
                )
                for hit in response["hits"]:
                    if hit["kind"] != kind:
                        raise ValueError("Official retrieval source family differs from the native inventory")
                    best[hit["source_id"], hit["chunk_id"]] = hit
                best = {
                    key: hit
                    for key, hit in sorted(
                        best.items(),
                        key=lambda item: item[1]["score"] if item[1]["score"] is not None else float("-inf"),
                        reverse=True,
                    )[:result_limit]
                }
                cursor = selected[-1][0]
            if not best:
                raise ValueError("Required exact-version " + kind + " references returned no cited context")
            hits.extend(best.values())
        await guard()
        return {
            "corpus": corpus["public"],
            "hits": hits,
            "warnings": [],
            "citations": [{k: v for k, v in hit.items() if k != "content"} for hit in hits],
            "context": self._official_evidence_context(hits),
        }

    @staticmethod
    def _official_evidence_context(hits: list[Data]) -> str:
        from firefly_forge.framework_knowledge_evidence import evidence_context

        return evidence_context(hits, [])

    async def managed_check(self, framework: str) -> None:
        import os
        from datetime import UTC, datetime

        from firefly_forge.framework_knowledge_bootstrap import managed_framework_scope

        selected = self._framework(framework)
        interval = int(os.environ.get("FORGE_FRAMEWORK_KNOWLEDGE_CHECK_SECONDS", "86400"))
        if interval <= 0:
            raise ValueError("Configure a positive official release-check cadence")
        async with managed_framework_scope(self) as actor:
            if actor is None:
                return
            saved = await self._get("status", selected) or {}
            checked = saved.get("last_check_attempt")
            if checked and (datetime.now(UTC) - datetime.fromisoformat(checked)).total_seconds() < interval:
                return
            async with self.store._unit() as session:
                await self._lock(session, selected)
                await self._authorize(admin=True, actor=actor)
                saved = await self._get("status", selected) or {}
                saved["last_check_attempt"] = datetime.now(UTC).isoformat()
                await self._put("status", selected, selected, saved)
            result = await self.check_updates(selected, FrameworkCheckUpdatesInput())
            release = result["release"]
            settings_digest = digest(self.knowledge.settings())
            policy = self.policy()
            collector = validate_collector_revision(policy.get("collector_revision", 1))
            identity = digest(
                {
                    "managed_release": release["commit"],
                    "framework": selected,
                    "connection": settings_digest,
                    "collector": collector,
                }
            )[:32]
            if await self._get("operation", identity) is not None:
                return
            if result["status"]["active_operation"] and result["status"]["active_operation"]["state"] not in {
                "ready",
                "failed",
                "cancelled",
            }:
                return
            corpus_ref = digest(
                {
                    "framework": selected,
                    "commit": release["commit"],
                    "version": release["framework_version"],
                    "connection": settings_digest,
                    "collector": collector,
                    "part_bytes": self.policy()["part_bytes"],
                }
            )[:32]
            if await self._get("corpus", corpus_ref) is not None:
                return
            await self.refresh(
                selected,
                identity,
                FrameworkRefreshInput(
                    version=1,
                    discovery_digest=release["digest"],
                    expected_status_revision=result["status"]["revision"],
                    policy_digest=result["policy_digest"],
                    make_current=True,
                ),
            )

    async def _execute_operation(self, record: Data) -> None:
        from firefly_forge.framework_knowledge_bootstrap import managed_framework_scope
        from firefly_forge.framework_knowledge_worker import FrameworkKnowledgeWorker

        await self.store.assert_ownership()
        if record["actor"].get("authentication_method") == "managed_framework_knowledge":
            async with managed_framework_scope(self) as actor:
                if actor != record["actor"]:
                    return
                await FrameworkKnowledgeWorker(self).advance(record["public"]["request_id"])
        else:
            await FrameworkKnowledgeWorker(self).advance(record["public"]["request_id"])

    async def start(self) -> None:
        import asyncio

        roles = self.store.runtime_roles
        if (roles is not None and not roles.executes_jobs) or self.poller is not None:
            return
        await self.store.assert_ownership()
        self.poller = asyncio.create_task(self._poll())

    async def stop(self) -> None:
        import asyncio

        tasks = [task for task in [self.poller, *self.active.values()] if task is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.poller = None
        self.active.clear()

    async def _poll(self) -> None:
        import asyncio
        import os

        concurrency = int(os.environ.get("FORGE_FRAMEWORK_KNOWLEDGE_CONCURRENCY", "2"))
        if concurrency <= 0:
            raise ValueError("Configure positive finite official framework refresh concurrency")

        while True:
            await self.store.assert_ownership()
            for key, task in list(self.active.items()):
                if task.done():
                    if not task.cancelled():
                        task.exception()
                    self.active.pop(key)
            for framework in OFFICIAL_REPOSITORIES:
                with suppress(Exception):
                    await self.managed_check(framework)
            cursor = None
            while True:
                async with self.store._unit():
                    rows = await self.records.pending_operations(cursor, 100)
                    values = [(row.key, copy.deepcopy(row.data)) for row in rows]
                if not values:
                    break
                for key, record in values:
                    cursor = key
                    if len(self.active) >= concurrency:
                        break
                    if key not in self.active:
                        self.active[key] = asyncio.create_task(self._execute_operation(record))
                if len(self.active) >= concurrency:
                    break
            await asyncio.sleep(1)

    async def resume(self, framework: str, request_id: str, body: Any) -> Data:
        async with self._operation_scope(framework, request_id):
            return await self._resume_selected(framework, request_id, body)

    async def _resume_selected(self, framework: str, request_id: str, body: Any) -> Data:
        selected = self._framework(framework)
        actor = await self._authorize(admin=True)
        body_digest = digest(body.model_dump())
        async with self.store._unit() as session:
            await self._lock(session, selected)
            await self._authorize(admin=True, actor=actor)
            record = await self._get("operation", request_id)
            if record is None or record["public"]["framework"] != selected:
                raise KeyError("Official framework refresh not found")
            await self._authorize(admin=True, actor=record["actor"])
            prior = record.get("resume_receipt")
            if prior and prior["body_digest"] == body_digest:
                return cast(Data, prior["response"])
            public = record["public"]
            if record.get("owner") or public["revision"] != body.expected_revision or public["state"] != "cancelled":
                raise ValueError("Resume only the exact cancelled operation after its owned worker stops")
            unknown = public["uncertain_sources"]
            if public.get("corpus"):
                ref = public["corpus"]["corpus_ref"]
                unknown = await self.records.count_state("part", ref, "submitting") + await self.records.count_state(
                    "part", ref, "needs_reconciliation"
                )
            if unknown:
                raise ValueError("Observe every uncertain source submission before resuming retained work")
            if any(v <= 0 for v in public["remaining_budget"].values()):
                raise ValueError("Explicitly renew retained work allowances before resuming")
            record.pop("cancel_requested", None)
            public.update(state="queued", sanitized_error=None, allowed_actions=["observe", "renew_budget", "cancel"])
            public["allowed_actions"] = self.operation_actions(record)
            public["revision"] = digest({k: v for k, v in public.items() if k != "revision"})
            record["resume_receipt"] = {"body_digest": body_digest, "response": copy.deepcopy(public)}
            await self._put("operation", request_id, selected, record, selected)
            await self._event(
                selected,
                "refresh_resume",
                {
                    "request_id": request_id,
                    "body_digest": body_digest,
                    "previous_revision": body.expected_revision,
                    "revision": public["revision"],
                    "spent_budget": public["spent_budget"],
                    "actor": actor,
                },
                request_id,
            )
        return cast(Data, copy.deepcopy(public))
