# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
import importlib
import json

import httpx
import pytest
from test_learning_ledger import completed

from firefly_forge.knowledge import KnowledgeConfiguration, KnowledgeManager
from firefly_forge.learning import LearningLedger


@pytest.mark.asyncio
async def test_unknown_source_submission_is_persistent_and_never_automatically_replayed(store, tmp_path):
    engine, run = await completed(store, tmp_path)
    ledger = LearningLedger(store, engine.journal)
    ledger.engine = engine
    record = await ledger.capture_outcome(run["id"])
    calls = []

    def remote(request):
        calls.append(request)
        if request.url.path.startswith("/api/v1/workspaces/"):
            return httpx.Response(
                200,
                json={
                    "id": "forge-" + run["repository_id"],
                    "name": "Owned workspace",
                    "tenant_id": "owned",
                    "status": "active",
                    "created_at": "2026-10-13T00:00:00Z",
                    "updated_at": "2026-10-13T00:00:00Z",
                },
            )
        assert request.method == "POST" and request.url.path == "/api/v1/sources"
        raise httpx.ReadTimeout("Unknown submission receipt", request=request)

    manager = KnowledgeManager(store, transport=httpx.MockTransport(remote))
    manager.configure(
        KnowledgeConfiguration(
            values={
                "enabled": True,
                "endpoint": "http://127.0.0.1:18500",
                "tenant_id": "owned",
                "api_key": "fixture-projection-key",
                "auto_memory": False,
            }
        )
    )
    module = importlib.import_module("firefly_forge.learning_projection")
    service = module.LearningProjection(store, engine.journal, ledger, manager)
    with pytest.raises(ValueError, match="unknown"):
        await service.submit(record["id"], request_id="a" * 32)
    state = await service.status(record["id"])
    assert state["state"] == "unknown" and state["target"]["revision"]
    with pytest.raises(ValueError, match="reconciliation"):
        await service.submit(record["id"], request_id="a" * 32)
    assert len([request for request in calls if request.url.path == "/api/v1/sources"]) == 1
    assert "fixture-projection-key" not in json.dumps(state)


@pytest.mark.asyncio
@pytest.mark.parametrize("drift", ["content", "metadata", "citation", "none"])
async def test_projection_requires_matching_indexed_source_and_citation(store, tmp_path, drift):
    import base64
    import hashlib

    engine, run = await completed(store, tmp_path)
    ledger = LearningLedger(store, engine.journal)
    ledger.engine = engine
    record = await ledger.capture_outcome(run["id"])
    admitted = {}
    calls = []

    def remote(request):
        calls.append(request)
        assert request.headers["X-Workspace-Id"] == "forge-learning-" + run["repository_id"]
        if request.url.path.startswith("/api/v1/workspaces/"):
            return httpx.Response(
                200,
                json={
                    "id": "forge-" + run["repository_id"],
                    "tenant_id": "owned",
                    "name": "Owned",
                    "status": "active",
                    "created_at": "2026-10-13T00:00:00Z",
                    "updated_at": "2026-10-13T00:00:00Z",
                },
            )
        if request.method == "POST" and request.url.path == "/api/v1/sources":
            payload = json.loads(request.content)
            admitted.update(
                {
                    "id": "owned-source",
                    "kind": "text",
                    "status": "queued",
                    "filename": payload["filename"],
                    "content_sha256": hashlib.sha256(base64.b64decode(payload["content_base64"])).hexdigest(),
                    "content_bytes": len(base64.b64decode(payload["content_base64"])),
                    "n_chunks": 0,
                    "metadata": payload["metadata"],
                    "created_at": "2026-10-13T00:00:00Z",
                    "updated_at": "2026-10-13T00:00:00Z",
                }
            )
            return httpx.Response(200, json=admitted)
        if request.method == "GET" and request.url.path == "/api/v1/sources/owned-source":
            source = {**admitted, "status": "ingested", "n_chunks": 1}
            if drift == "content":
                source["content_sha256"] = "0" * 64
            if drift == "metadata":
                source["metadata"] = {**source["metadata"], "extra": {"learning_record_id": "other"}}
            return httpx.Response(200, json=source)
        assert request.url.path == "/api/v1/search" and json.loads(request.content)["source_ids"] == ["owned-source"]
        return httpx.Response(
            200,
            json={
                "elapsed_ms": 1,
                "hits": [
                    {
                        "source_id": "other-source" if drift == "citation" else "owned-source",
                        "chunk_id": "owned-chunk",
                        "source_filename": admitted["filename"],
                        "content": "Local procedure",
                        "score": 1,
                    }
                ],
            },
        )

    manager = KnowledgeManager(store, transport=httpx.MockTransport(remote))
    manager.configure(
        KnowledgeConfiguration(
            values={
                "enabled": True,
                "endpoint": "http://127.0.0.1:18500",
                "tenant_id": "owned",
                "api_key": "fixture-projection-key",
                "auto_memory": False,
            }
        )
    )
    service = importlib.import_module("firefly_forge.learning_projection").LearningProjection(
        store, engine.journal, ledger, manager
    )
    queued = await service.submit(record["id"], request_id="a" * 32)
    assert queued["state"] == "source_admitted"
    if drift != "none":
        with pytest.raises(ValueError, match="match|citations"):
            await service.observe(record["id"], request_id="b" * 32)
        assert (await service.status(record["id"]))["state"] == "source_admitted"
    else:
        indexed = await service.observe(record["id"], request_id="b" * 32)
        assert indexed["state"] == "verified" and indexed["citations"] == [
            {"source_id": "owned-source", "chunk_id": "owned-chunk"}
        ]
        assert await service.observe(record["id"], request_id="b" * 32) == indexed
        assert await service.submit(record["id"], request_id="a" * 32) == indexed
    assert len([r for r in calls if r.method == "POST" and r.url.path == "/api/v1/sources"]) == 1


@pytest.mark.asyncio
async def test_database_refuses_projection_payload_replacement(store, tmp_path):
    from sqlalchemy import update
    from sqlalchemy.exc import DBAPIError

    from firefly_forge.learning_persistence import ForgeLearningProjectionEntity

    engine, run = await completed(store, tmp_path)
    ledger = LearningLedger(store, engine.journal)
    ledger.engine = engine
    record = await ledger.capture_outcome(run["id"])
    with pytest.raises(DBAPIError, match="immutable"):
        async with store._unit() as session:
            await session.execute(update(ForgeLearningProjectionEntity).values(data={"payload": {"fabricated": True}}))
    async with store._unit():
        from firefly_forge.store import identifier

        projection = await ledger.projections.for_record(identifier(record["id"]))
        assert projection.data["payload"] == record["envelope"]


@pytest.mark.asyncio
async def test_concurrent_and_cancelled_submission_has_one_intent_and_read_only_recovery(store, tmp_path):
    import asyncio
    import base64
    import hashlib

    engine, run = await completed(store, tmp_path)
    ledger = LearningLedger(store, engine.journal)
    ledger.engine = engine
    record = await ledger.capture_outcome(run["id"])
    started = asyncio.Event()
    admitted = {}
    calls = []

    async def remote(request):
        calls.append(request)
        workspace = "forge-learning-" + run["repository_id"]
        assert request.headers["X-Workspace-Id"] == workspace
        if request.url.path.startswith("/api/v1/workspaces/"):
            return httpx.Response(
                200,
                json={
                    "id": workspace,
                    "tenant_id": "owned",
                    "name": "Owned learning",
                    "status": "active",
                    "created_at": "2026-10-13T00:00:00Z",
                    "updated_at": "2026-10-13T00:00:00Z",
                },
            )
        if request.method == "POST" and request.url.path == "/api/v1/sources":
            payload = json.loads(request.content)
            content = base64.b64decode(payload["content_base64"])
            admitted.update(
                {
                    "id": "owned-source",
                    "kind": "text",
                    "status": "ingested",
                    "filename": payload["filename"],
                    "content_sha256": hashlib.sha256(content).hexdigest(),
                    "content_bytes": len(content),
                    "n_chunks": 1,
                    "metadata": payload["metadata"],
                    "created_at": "2026-10-13T00:00:00Z",
                    "updated_at": "2026-10-13T00:00:00Z",
                }
            )
            started.set()
            await asyncio.Event().wait()
        if request.method == "GET" and request.url.path == "/api/v1/sources/owned-source":
            return httpx.Response(200, json=admitted)
        assert request.url.path == "/api/v1/search"
        return httpx.Response(
            200,
            json={
                "elapsed_ms": 1,
                "hits": [
                    {"source_id": "owned-source", "chunk_id": "owned-chunk", "content": "Local procedure", "score": 1}
                ],
            },
        )

    manager = KnowledgeManager(store, transport=httpx.MockTransport(remote))
    manager.configure(
        KnowledgeConfiguration(
            values={
                "enabled": True,
                "endpoint": "http://127.0.0.1:18500",
                "tenant_id": "owned",
                "api_key": "fixture-projection-key",
                "auto_memory": False,
            }
        )
    )
    service = importlib.import_module("firefly_forge.learning_projection").LearningProjection(
        store, engine.journal, ledger, manager
    )
    pending = asyncio.create_task(service.submit(record["id"], request_id="a" * 32))
    await asyncio.wait_for(started.wait(), 4)
    try:
        with pytest.raises(ValueError, match="reconciliation"):
            await service.submit(record["id"], request_id="b" * 32)
        with pytest.raises(ValueError, match="running"):
            await service.observe(record["id"], source_id="owned-source", request_id="c" * 32)
    finally:
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
    assert (await service.status(record["id"]))["state"] == "unknown"
    recovered = await service.observe(record["id"], source_id="owned-source", request_id="c" * 32)
    assert recovered["state"] == "verified"
    assert len([r for r in calls if r.method == "POST" and r.url.path == "/api/v1/sources"]) == 1
