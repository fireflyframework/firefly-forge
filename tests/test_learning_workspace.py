# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Exercise new project workspace admission through the real flycanon SDK."""

import base64
import hashlib
import json
from uuid import uuid4

import httpx
import pytest
from flycanon_sdk import WorkspaceCreate
from test_learning_ledger import completed

from firefly_forge.knowledge import KnowledgeConfiguration, KnowledgeManager
from firefly_forge.learning import LearningLedger
from firefly_forge.learning_projection import LearningProjection


@pytest.mark.asyncio
async def test_new_project_workspace_fits_sdk_and_admits_one_scoped_source(store, tmp_path):
    engine, run = await completed(store, tmp_path, scoped=True)
    ledger = LearningLedger(store, engine.journal)
    ledger.engine = engine
    record = await ledger.capture_outcome(run["id"])
    requests = []
    created = {}

    async def remote(request):
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(404, json={"detail": "Workspace not found"})
        payload = json.loads(request.content)
        if request.url.path == "/api/v1/workspaces":
            model = WorkspaceCreate.model_validate(payload)
            created.update(
                id=model.id,
                name=model.name,
                tenant_id="owned",
                status="active",
                created_at="2026-10-13T00:00:00Z",
                updated_at="2026-10-13T00:00:00Z",
            )
            return httpx.Response(201, json=created)
        assert request.method == "POST" and request.url.path == "/api/v1/sources"
        assert request.headers["X-Workspace-Id"] == created["id"]
        body = base64.b64decode(payload["content_base64"])
        assert json.loads(body)["context"]["project_id"] == run["project_id"]
        assert json.loads(body)["context"]["repository_id"] == run["repository_id"]
        return httpx.Response(
            201,
            json={
                "id": "owned-scoped-source",
                "kind": "text",
                "status": "queued",
                "filename": payload["filename"],
                "content_sha256": hashlib.sha256(body).hexdigest(),
                "content_bytes": len(body),
                "n_chunks": 0,
                "metadata": payload["metadata"],
                "created_at": "2026-10-13T00:00:00Z",
                "updated_at": "2026-10-13T00:00:00Z",
            },
        )

    manager = KnowledgeManager(store, transport=httpx.MockTransport(remote))
    manager.configure(
        KnowledgeConfiguration(
            values={
                "enabled": True,
                "endpoint": "http://127.0.0.1:18500",
                "tenant_id": "owned",
                "api_key": "fixture-owned-workspace-key",
                "auto_memory": False,
            }
        )
    )
    projection = LearningProjection(store, engine.journal, ledger, manager)
    admitted = await projection.submit(record["id"], request_id=uuid4().hex)
    assert admitted["state"] == "source_admitted"
    assert len(admitted["workspace_id"]) <= 64
    assert len([r for r in requests if r.method == "POST" and r.url.path == "/api/v1/workspaces"]) == 1
    assert len([r for r in requests if r.method == "POST" and r.url.path == "/api/v1/sources"]) == 1
    repeated = await projection.submit(record["id"], request_id=uuid4().hex)
    assert repeated["workspace_id"] == admitted["workspace_id"]
    assert len(requests) == 3


def test_workspace_identity_fits_sdk_and_uses_both_full_scope_ids():
    from firefly_forge.learning_models import LearningContext

    base = {
        "repository_id": "a" * 32,
        "project_id": "b" * 32,
        "component_id": None,
        "framework": "pyfly",
        "version": "26.10.1",
        "dependency_digest": "c" * 64,
        "source_tree": "d" * 40,
        "query_digest": "e" * 64,
    }
    selected = LearningProjection.workspace_id(LearningContext.model_validate(base))
    assert len(selected) <= 64
    WorkspaceCreate(id=selected, name="Owned learning")
    for field in ("repository_id", "project_id"):
        changed = {**base, field: base[field][:-1] + "f"}
        assert LearningProjection.workspace_id(LearningContext.model_validate(changed)) != selected
    unassigned = LearningProjection.workspace_id(LearningContext.model_validate({**base, "project_id": None}))
    assert unassigned == "forge-learning-" + base["repository_id"]
    WorkspaceCreate(id=unassigned, name="Unassigned repository")
