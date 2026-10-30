# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Native source, freshness and projection authority regression cases."""

import base64
import hashlib
import json
from datetime import timedelta
from uuid import uuid4

import httpx
import pytest
from test_learning_ledger import completed

from firefly_forge.knowledge import KnowledgeConfiguration, KnowledgeManager
from firefly_forge.learning import LearningLedger
from firefly_forge.learning_configuration import configure_learning
from firefly_forge.learning_models import LearningPolicy
from firefly_forge.learning_projection import LearningProjection
from firefly_forge.models import RunInput


async def source_case(store, tmp_path):
    engine, run = await completed(store, tmp_path, retained_knowledge=True)
    ledger = LearningLedger(store, engine.journal)
    ledger.engine = engine
    record = await ledger.capture_outcome(run["id"])
    return engine, run, ledger, record


@pytest.mark.asyncio
async def test_stricter_consuming_run_freshness_is_enforced(store, tmp_path):
    engine, run, ledger, record = await source_case(store, tmp_path)
    configure_learning(store.configuration, LearningPolicy(max_age_hours=1))
    consumer = await store.create_run(RunInput(repository_id=run["repository_id"], brief=run["brief"]))
    assert consumer["learning_snapshot"]["settings"]["max_age_hours"] == 1
    configure_learning(store.configuration, LearningPolicy(max_age_hours=72))
    native_time = ledger.records_repository.database_time

    async def aged():
        return await native_time() + timedelta(hours=2)

    ledger.records_repository.database_time = aged
    decision = await ledger.lookup(consumer["id"], run["brief"], record["envelope"]["context"])
    assert not decision["eligible"], "Two-hour-old origin remains eligible under a consuming one-hour policy"
    assert decision["reason"] == "expired" and decision["refresh_required"]
    repeated = await ledger.capture_outcome(run["id"])
    assert repeated["verified_at"] == record["verified_at"] and repeated["expires_at"] == record["expires_at"]


@pytest.mark.asyncio
@pytest.mark.parametrize("lost_after", ["workspace_read", "source_write"])
async def test_owner_loss_blocks_source_dispatch_or_admission(store, tmp_path, lost_after):
    engine, run, ledger, record = await source_case(store, tmp_path)
    await store.acquire_ownership()
    engine.enforce_ownership = True
    sources = []

    async def remote(request):
        if request.method == "GET" and request.url.path.startswith("/api/v1/workspaces/"):
            if lost_after == "workspace_read":
                await store.release_ownership()
            return httpx.Response(
                200,
                json={
                    "id": request.headers["X-Workspace-Id"],
                    "tenant_id": "owned",
                    "name": "Owned",
                    "status": "active",
                    "created_at": "2026-10-13T00:00:00Z",
                    "updated_at": "2026-10-13T00:00:00Z",
                },
            )
        assert request.method == "POST" and request.url.path == "/api/v1/sources"
        sources.append(request)
        if lost_after == "source_write":
            await store.release_ownership()
        payload = json.loads(request.content)
        content = base64.b64decode(payload["content_base64"])
        return httpx.Response(
            200,
            json={
                "id": "owned-source",
                "kind": "text",
                "status": "queued",
                "filename": payload["filename"],
                "content_sha256": hashlib.sha256(content).hexdigest(),
                "content_bytes": len(content),
                "n_chunks": 0,
                "metadata": payload["metadata"],
                "created_at": "2026-10-13T00:00:00Z",
                "updated_at": "2026-10-13T00:00:00Z",
            },
        )

    knowledge = KnowledgeManager(store, engine, transport=httpx.MockTransport(remote))
    from test_knowledge import retained_configuration

    retained_configuration(knowledge.private)
    knowledge.configure(
        KnowledgeConfiguration(
            values={
                "enabled": True,
                "endpoint": "http://127.0.0.1:18500",
                "tenant_id": "owned",
                "api_key": "fixture-private-independent-projection-key",
                "auto_memory": False,
            }
        )
    )
    projection = LearningProjection(store, engine.journal, ledger, knowledge)
    with pytest.raises(ValueError, match="unknown"):
        await projection.submit(record["id"], request_id=uuid4().hex)
    assert len(sources) == (0 if lost_after == "workspace_read" else 1)
    assert (await projection.status(record["id"]))["state"] == "unknown"
    with pytest.raises(ValueError, match="reconciliation"):
        await projection.submit(record["id"], request_id=uuid4().hex)
    assert len(sources) == (0 if lost_after == "workspace_read" else 1)


@pytest.mark.asyncio
async def test_projection_unknown_or_verified_cannot_pivot_target_revision(store, tmp_path):
    engine, run, ledger, record = await source_case(store, tmp_path)
    calls = []

    def remote(request):
        calls.append((request.headers["X-Tenant-Id"], request.url.path))
        if request.url.path.startswith("/api/v1/workspaces/"):
            return httpx.Response(
                200,
                json={
                    "id": request.headers["X-Workspace-Id"],
                    "tenant_id": "owned",
                    "name": "Owned",
                    "status": "active",
                    "created_at": "2026-10-13T00:00:00Z",
                    "updated_at": "2026-10-13T00:00:00Z",
                },
            )
        raise httpx.ReadTimeout("fixture ambiguous write", request=request)

    knowledge = KnowledgeManager(store, transport=httpx.MockTransport(remote))
    from test_knowledge import retained_configuration

    retained_configuration(knowledge.private)
    knowledge.configure(
        KnowledgeConfiguration(
            values={
                "enabled": True,
                "endpoint": "http://127.0.0.1:18500",
                "tenant_id": "owned",
                "api_key": "fixture-key-first",
                "auto_memory": False,
            }
        )
    )
    projection = LearningProjection(store, engine.journal, ledger, knowledge)
    with pytest.raises(ValueError, match="unknown"):
        await projection.submit(record["id"], request_id=uuid4().hex)
    original = await projection.status(record["id"])
    from test_knowledge import retained_configuration

    retained_configuration(knowledge.private)
    knowledge.configure(KnowledgeConfiguration(values={"tenant_id": "other", "api_key": "fixture-key-second"}))
    with pytest.raises(ValueError, match="reconciliation"):
        await projection.submit(record["id"], request_id=uuid4().hex)
    assert (await projection.status(record["id"]))["target"] == original["target"]
    assert calls == [("owned", "/api/v1/workspaces/" + original["workspace_id"]), ("owned", "/api/v1/sources")]


@pytest.mark.asyncio
async def test_mutated_source_between_validation_and_inspection_cannot_become_verified(store, tmp_path):
    engine, run = await completed(store, tmp_path)
    ledger = LearningLedger(store, engine.journal)
    ledger.engine = engine
    actual = engine.validated_workspace

    async def changed_after_validation(run_id):
        workspace = await actual(run_id)
        (workspace / "product.py").write_text("stock = 999\n")
        return workspace

    engine.validated_workspace = changed_after_validation
    with pytest.raises(ValueError, match="source changed"):
        await ledger.capture_outcome(run["id"])
    assert await ledger.records(run["repository_id"]) == []


@pytest.mark.asyncio
async def test_review_revoked_during_workspace_read_prevents_source_post(store, tmp_path):
    from firefly_forge.identity import Principal
    from firefly_forge.project_access import CURRENT_PRINCIPAL, ProjectAccess

    engine, run = await completed(store, tmp_path, scoped=True, retained_knowledge=True)
    ledger = LearningLedger(store, engine.journal)
    ledger.engine = engine
    record = await ledger.capture_outcome(run["id"])
    actor = Principal(uuid4(), "https://issuer.invalid", "independent-reviewer")
    access = ProjectAccess(store)
    await access.set_membership(run["project_id"], actor, "reviewer")
    sources = []

    async def remote(request):
        if request.method == "GET" and request.url.path.startswith("/api/v1/workspaces/"):
            await access.set_membership(run["project_id"], actor, None)
            return httpx.Response(
                200,
                json={
                    "id": request.headers["X-Workspace-Id"],
                    "tenant_id": "owned",
                    "name": "Owned",
                    "status": "active",
                    "created_at": "2026-10-13T00:00:00Z",
                    "updated_at": "2026-10-13T00:00:00Z",
                },
            )
        sources.append(request)
        raise AssertionError("Revoked review authority dispatched a source")

    knowledge = KnowledgeManager(store, transport=httpx.MockTransport(remote))
    from test_knowledge import retained_configuration

    retained_configuration(knowledge.private)
    knowledge.configure(
        KnowledgeConfiguration(
            values={
                "enabled": True,
                "endpoint": "http://127.0.0.1:18500",
                "tenant_id": "owned",
                "api_key": "fixture-project-review-key",
                "auto_memory": False,
            }
        )
    )
    projection = LearningProjection(store, engine.journal, ledger, knowledge)
    principal = CURRENT_PRINCIPAL.set(actor)
    try:
        with pytest.raises(ValueError, match="unknown"):
            await projection.submit(record["id"], request_id=uuid4().hex)
    finally:
        CURRENT_PRINCIPAL.reset(principal)
    assert sources == [] and (await projection.status(record["id"]))["state"] == "unknown"


@pytest.mark.asyncio
async def test_verified_declarations_come_from_retained_tree_not_mutable_files(store, tmp_path, monkeypatch):
    import firefly_forge.learning as module

    engine, run = await completed(store, tmp_path)
    ledger = LearningLedger(store, engine.journal)
    ledger.engine = engine
    native_observe = module.observe_context

    async def changed_declaration(run, source, workspace, query, **kwargs):
        declaration = workspace / "pyproject.toml"
        original = declaration.read_text()
        declaration.write_text(original.replace("26.10.1", "99.1.0"))
        try:
            return await native_observe(run, source, workspace, query, **kwargs)
        finally:
            declaration.write_text(original)

    monkeypatch.setattr(module, "observe_context", changed_declaration)
    outcome = await ledger.capture_outcome(run["id"])
    assert outcome["envelope"]["context"]["version"] == "26.10.1"
    assert outcome["envelope"]["context"]["source_tree"] == run["source_tree"]
