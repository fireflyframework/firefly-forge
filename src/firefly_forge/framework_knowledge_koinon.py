# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Native official corpora retain local release provenance and remote version receipts."""

from __future__ import annotations

import copy
import hashlib
import json
from typing import Any

from firefly_forge.knowledge_native_sources import catalog_children, observe_source
from firefly_forge.knowledge_platform import KoinonScope
from firefly_forge.project_access import AccessDenied

Data = dict[str, Any]


class CountedClient:
    def __init__(self, client: Any, worker: Any) -> None:
        self.client, self.worker = client, worker

    def __getattr__(self, name: str) -> Any:
        async def call(*args: Any, **kwargs: Any) -> Any:
            await self.worker._spend(requests=1)
            result = await getattr(self.client, name)(*args, **kwargs)
            await self.worker._spend()
            return result

        return call


def framework_scope(settings: Data) -> KoinonScope:
    if settings.get("provider") != "koinon" or not settings["enabled"] or not settings["access_token"]:
        raise ValueError("Configure an enabled Koinon machine connection for official references")
    scope = KoinonScope.model_validate(settings.get("framework_scope"))
    if scope.collection_id is None:
        raise ValueError("Map an existing Koinon corpus or collection for official framework ingestion")
    return scope


async def submit(worker: Any, data: Data, *, observe_only: bool) -> None:
    manager = worker.manager
    ref, framework = data["public"]["corpus"]["corpus_ref"], data["public"]["framework"]
    settings = manager.knowledge.settings({"knowledge_snapshot": data["knowledge_snapshot"]})
    scope = framework_scope(settings)
    platform = manager.knowledge.platform
    destination = scope.collection_id
    if destination is None:
        raise ValueError("An explicit native framework collection is required")
    async with platform.client(settings) as native:
        client: Any = CountedClient(native, worker)
        await platform.grant(client, settings, scope, "ingest:write")
        await platform.bind(client, settings, scope)
        async for part in worker._each("part", ref):
            if part["state"] in {"ready", "excluded"}:
                continue
            if part["state"] in {"submitting", "needs_reconciliation"}:
                matches = []
                async for source in catalog_children(client, settings, scope, destination, "source", worker._guard):
                    if source.name == part["filename"] and source.metadata.get("sha256") == part["content_sha256"]:
                        matches.append(source.id)
                if len(matches) != 1:
                    raise ValueError("Uncertain native framework upload has no unique scoped content readback")
                part.update(state="indexing", source_id=matches[0])
                async with manager.store._unit():
                    await worker._current(verify_connection=False)
                    await manager._put("part", part["part_ref"], framework, part, ref)
            if part["state"] == "pending":
                if observe_only:
                    continue
                content = worker._part_content(part, data["policy"]["part_bytes"])
                if hashlib.sha256(content).hexdigest() != part["content_sha256"]:
                    raise ValueError("Official framework part bytes changed before native upload")
                await worker._spend(byte_count=len(content))
                async with manager.store._unit():
                    await worker._current(verify_connection=False)
                    part["state"] = "submitting"
                    await manager._put("part", part["part_ref"], framework, part, ref)
                    await manager._event(
                        framework,
                        "source_intent",
                        {"part_ref": part["part_ref"], "content_sha256": part["content_sha256"], "provider": "koinon"},
                        worker.request_id,
                    )
                await platform.grant(client, settings, scope, "ingest:write")
                await worker._guard()
                result = await client.upload(
                    settings["tenant_id"],
                    filename=part["filename"],
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
                    or source.name != part["filename"]
                    or source.metadata.get("sha256") != part["content_sha256"]
                ):
                    raise AccessDenied("Official native upload receipt changed its source or mapped destination")
                for asset in result.assets:
                    platform.scoped(asset, settings, scope)
                    if asset.parent_id != source.id or asset.kind != "document":
                        raise AccessDenied("Official native upload asset changed its source parent")
                identities = json.dumps({"source_id": source.id, "assets": [asset.id for asset in result.assets]})
                if manager.knowledge.safe(identities, settings) != identities:
                    raise ValueError("Native official upload identity contains a registered credential")
                async with manager.store._unit():
                    await worker._current(verify_connection=False)
                    part.update(state="indexing", source_id=source.id, koinon_asset_ids=[x.id for x in result.assets])
                    await manager._put("part", part["part_ref"], framework, part, ref)
                    await manager._event(
                        framework,
                        "source_outcome",
                        {"part_ref": part["part_ref"], "source_id": source.id, "provider": "koinon"},
                        worker.request_id,
                    )
            if part["source_id"] and not (observe_only and data.get("cancel_requested")):
                await platform.grant(client, settings, scope, "knowledge:search")
                receipt = await observe_source(
                    platform,
                    client,
                    settings,
                    scope,
                    part["source_id"],
                    part["filename"],
                    part["content_sha256"],
                    worker._guard,
                    part.get("koinon_asset_ids"),
                )
                await platform.grant(client, settings, scope, "knowledge:search")
                if receipt["version_state"] != "recorded" or receipt["source_status"] != "ready":
                    continue
                # A native version read is necessary; retrieval must also return this exact admitted asset.
                result = await client.search(
                    settings["tenant_id"],
                    part["filename"],
                    workspace_id=scope.workspace_id,
                    project_id=scope.project_id,
                    collection_id=scope.collection_id,
                    asset_ids=receipt["asset_ids"],
                    answer=False,
                    limit=1,
                )
                if not any(
                    hit.asset_id in receipt["asset_ids"] and hit.source_id == part["source_id"] and hit.text.strip()
                    for hit in result.hits
                ):
                    continue
                await worker._guard()
                async with manager.store._unit():
                    await worker._current(verify_connection=False)
                    part.update(state="ready", canonical_part_ref=part["part_ref"], koinon_receipt=receipt)
                    await manager._put("part", part["part_ref"], framework, part, ref)
    await worker._stage("verifying")


async def search(
    manager: Any, corpus: Data, body: Any, guard: Any, *, source_ids: list[str] | None, part_refs: set[str] | None
) -> Data:
    settings = manager.knowledge.settings({"knowledge_snapshot": corpus["knowledge_snapshot"]})
    scope = framework_scope(settings)
    platform = manager.knowledge.platform
    parts: list[Data] = []
    cursor = None
    while True:
        async with manager.store._unit():
            rows = await manager.records.page("part", corpus["public"]["corpus_ref"], cursor, 128)
        if not rows:
            break
        parts.extend(
            copy.deepcopy(row.data)
            for row in rows
            if row.data["state"] == "ready"
            and (source_ids is None or row.data["source_id"] in source_ids)
            and (part_refs is None or row.data["part_ref"] in part_refs)
        )
        cursor = rows[-1].key
    targets = {}
    for part in parts:
        receipt = part.get("koinon_receipt")
        if not receipt or receipt["version_state"] != "recorded" or receipt["content_sha256"] != part["content_sha256"]:
            raise ValueError("Official native corpus lacks its exact versioned source receipt")
        for version in receipt["source_versions"]:
            targets[version["asset_id"]] = (part, version)
    hits = []
    await guard()
    async with platform.client(settings) as client:
        await platform.grant(client, settings, scope, "knowledge:search")
        asset_ids = list(targets)
        for start in range(0, len(asset_ids), 128):
            await guard()
            selected = asset_ids[start : start + 128]
            result = await client.search(
                settings["tenant_id"],
                body.query,
                workspace_id=scope.workspace_id,
                project_id=scope.project_id,
                collection_id=scope.collection_id,
                asset_ids=selected,
                answer=False,
                limit=settings["top_k"],
            )
            await guard()
            for hit in result.hits:
                if hit.asset_id not in selected:
                    raise AccessDenied("Official native retrieval returned a foreign corpus asset")
                part, version = targets[hit.asset_id]
                if hit.source_id != part["source_id"]:
                    raise AccessDenied("Official native citation changed its admitted source")
                current = await observe_source(
                    platform,
                    client,
                    settings,
                    scope,
                    part["source_id"],
                    part["filename"],
                    part["content_sha256"],
                    guard,
                    [hit.asset_id],
                )
                if current["source_versions"] != [version]:
                    raise AccessDenied("Official native source version changed after corpus admission")
                source = await manager._get("source", part["source_ref"])
                entry = source["public"]
                if entry["state"] != "ready":
                    raise ValueError("Official immutable source inventory is not ready")
                hits.append(
                    {
                        "kind": entry["kind"],
                        "license_path": source.get("license_path"),
                        "source_ref": entry["source_ref"],
                        "corpus_ref": corpus["public"]["corpus_ref"],
                        "framework": corpus["public"]["framework"],
                        "framework_version": corpus["public"]["framework_version"],
                        "release_tag": corpus["public"]["release_tag"],
                        "commit": corpus["public"]["commit"],
                        "source_id": part["source_id"],
                        "asset_id": hit.asset_id,
                        "chunk_id": hit.id,
                        "path": entry["path"],
                        "source_uri": entry["source_uri"],
                        "source_sha256": entry["sha256"],
                        "license_sha256": entry["license_sha256"],
                        "knowledge_version": None,
                        **version,
                        "score": hit.score,
                        "content": manager.knowledge.safe(hit.text, settings),
                    }
                )
        await platform.grant(client, settings, scope, "knowledge:search")
        await guard()
    safe_hits = json.loads(manager.knowledge.safe(json.dumps(hits, ensure_ascii=False), settings))
    return {
        "corpus": corpus["public"],
        "hits": safe_hits,
        "citations": [{k: v for k, v in hit.items() if k != "content"} for hit in safe_hits],
        "warnings": [],
    }
