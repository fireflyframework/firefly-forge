# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Official corpus workers keep native source versions and exact version filters."""

import hashlib
import json
from email.parser import BytesParser
from email.policy import default

import httpx
from framework_knowledge_fixtures import official_fixture
from test_framework_knowledge_service_host import Records, Store
from test_koinon_knowledge_host import context, entry, settings

from firefly_forge.configuration import ConfigurationStore
from firefly_forge.framework_knowledge import FrameworkKnowledge
from firefly_forge.framework_knowledge_models import (
    FrameworkCheckUpdatesInput,
    FrameworkRefreshInput,
    FrameworkSearchInput,
)
from firefly_forge.framework_knowledge_worker import FrameworkKnowledgeWorker
from firefly_forge.knowledge import KnowledgeManager


async def native_framework(tmp_path):
    records = Records()
    store = Store(records)
    store.configuration = ConfigurationStore(tmp_path)
    state = {"posts": 0, "sources": {}, "queries": []}

    async def handle(request):
        path = request.url.path
        if path.endswith("/context"):
            value = context()
            value["workspace_permissions"]["workspace"] += ["ingest:write", "catalog:write"]
        elif path.endswith("/sources/upload"):
            state["posts"] += 1
            message = BytesParser(policy=default).parsebytes(
                b"Content-Type: " + request.headers["Content-Type"].encode() + b"\r\n\r\n" + request.content
            )
            file = next(
                part for part in message.iter_parts() if part.get_param("name", header="content-disposition") == "file"
            )
            content = file.get_payload(decode=True)
            source_id = "source" + str(state["posts"])
            source = entry(source_id, "source", parent="collection")
            source.update(name=file.get_filename(), metadata={"sha256": hashlib.sha256(content).hexdigest()})
            document = entry("doc" + str(state["posts"]), "document", parent=source_id)
            state["sources"][source_id] = (source, document, content.decode())
            value = {"source": source, "assets": [document]}
            if state.get("lose_next"):
                state["lose_next"] = False
                raise httpx.ReadTimeout("Controlled committed source reply lost", request=request)
        elif path.endswith("/catalog/project"):
            value = entry("project", "project", parent="workspace")
        elif path.endswith("/catalog/collection"):
            value = entry("collection", "collection", parent="project")
        elif "/catalog/" in path:
            identifier = path.rsplit("/", 1)[1]
            value = next(
                item
                for source, document, _ in state["sources"].values()
                for item in (source, document)
                if item["id"] == identifier
            )
        elif path.endswith("/catalog"):
            parent = request.url.params.get("parent_id")
            items = [
                source if request.url.params.get("kind") == "source" else document
                for source, document, _ in state["sources"].values()
                if source["parent_id"] == parent or document["parent_id"] == parent
            ]
            value = {"items": items, "total": len(items), "page": 1, "page_size": 25, "pages": 1}
        elif path.endswith("/chunks"):
            document_id = path.split("/")[-2]
            source, _, _ = next(value for value in state["sources"].values() if value[1]["id"] == document_id)
            value = {
                "document_id": document_id,
                "source_id": source["id"],
                "native_source_id": "native-" + source["id"],
                "source_version_id": state.get("changed_version") or "v-" + source["id"],
                "extraction_id": "extract-" + source["id"],
                "items": [],
                "offset": 0,
                "limit": 1,
                "total": 0,
                "next_offset": None,
            }
        elif path.endswith("/search"):
            body = json.loads(request.content)
            state["queries"].append(body)
            assert body["workspace_id"] == "workspace" and body["project_id"] == "project"
            selected = body["asset_ids"]
            value = {
                "hits": [
                    {
                        "id": "chunk-" + document["id"],
                        "title": source["name"],
                        "text": content,
                        "asset_id": document["id"],
                        "source_id": source["id"],
                        "score": 0.9,
                    }
                    for source, document, content in state["sources"].values()
                    if document["id"] in selected
                ]
            }
        else:
            raise AssertionError("Unexpected native framework route " + path)
        return httpx.Response(200, json=value)

    knowledge = KnowledgeManager(store, transport=httpx.MockTransport(handle))
    scope = {"workspace_id": "workspace", "project_id": "project", "collection_id": "collection", "dataset_ids": []}
    knowledge.configure(settings(framework_scope=scope))
    manager = FrameworkKnowledge(store, knowledge, records=records, transport=official_fixture()[0])

    async def authorize(*args, **kwargs):
        return {"user_id": "a" * 32, "global_admin": True}

    manager._authorize = authorize
    discovery = await manager.check_updates("pyfly", FrameworkCheckUpdatesInput())
    queued = await manager.refresh(
        "pyfly",
        "f" * 32,
        FrameworkRefreshInput(
            version=1,
            discovery_digest=discovery["release"]["digest"],
            expected_status_revision=discovery["status"]["revision"],
            policy_digest=discovery["policy_digest"],
        ),
    )
    return manager, state, queued


async def test_real_framework_worker_native_uploads_prove_ready_versions_and_exact_corpus_search(tmp_path):
    manager, state, queued = await native_framework(tmp_path)
    await FrameworkKnowledgeWorker(manager).advance("f" * 32)
    result = await manager.get_refresh("pyfly", "f" * 32)
    assert result["state"] == "ready", await manager._get("operation", "f" * 32)
    assert result["counts"]["ready_parts"] == 5 and state["posts"] == 5
    parts = [row.data for row in manager.records.rows.values() if row.kind == "part"]
    assert all(part["koinon_receipt"]["source_versions"][0]["source_version_id"] for part in parts)
    found = await manager.search(
        "pyfly", FrameworkSearchInput(query="service", corpus_ref=queued["corpus"]["corpus_ref"])
    )
    assert found["hits"] and all(hit["framework_version"] == "26.10.11" for hit in found["hits"])
    assert all(hit["source_version_id"] for hit in found["hits"])
    before = state["posts"]
    await FrameworkKnowledgeWorker(manager).advance("f" * 32, observe_only=True)
    assert state["posts"] == before


async def test_native_framework_lost_upload_observes_exact_content_without_reposting(tmp_path):
    manager, state, _ = await native_framework(tmp_path)
    state["lose_next"] = True
    await FrameworkKnowledgeWorker(manager).advance("f" * 32)
    assert state["posts"] == 1
    await FrameworkKnowledgeWorker(manager).advance("f" * 32, observe_only=True)
    assert state["posts"] == 1
    await FrameworkKnowledgeWorker(manager).advance("f" * 32)
    assert state["posts"] == 5
    assert (await manager.get_refresh("pyfly", "f" * 32))["state"] == "ready"


async def test_native_framework_refuses_changed_version_and_preserves_native_question_length(tmp_path):
    import pytest

    manager, state, queued = await native_framework(tmp_path)
    await FrameworkKnowledgeWorker(manager).advance("f" * 32)
    query = "λ" * 7000
    await manager.search("pyfly", FrameworkSearchInput(query=query, corpus_ref=queued["corpus"]["corpus_ref"]))
    assert state["queries"][-1]["question"] == query
    state["changed_version"] = "other-version"
    with pytest.raises(Exception, match="version"):
        await manager.search("pyfly", FrameworkSearchInput(query="service", corpus_ref=queued["corpus"]["corpus_ref"]))


async def test_native_framework_rejects_credential_bearing_remote_identity_before_persisting_it(tmp_path):
    manager, _, _ = await native_framework(tmp_path)
    old = manager.knowledge.transport

    async def handle(request):
        response = await old.handle_async_request(request)
        if request.url.path.endswith("/sources/upload"):
            value = json.loads(response.content)
            value["source"]["id"] = "private-machine-token"
            value["assets"][0]["parent_id"] = "private-machine-token"
            return httpx.Response(200, json=value)
        return response

    manager.knowledge.transport = httpx.MockTransport(handle)
    await FrameworkKnowledgeWorker(manager).advance("f" * 32)
    assert "private-machine-token" not in json.dumps([row.data for row in manager.records.rows.values()])
    assert (await manager.get_refresh("pyfly", "f" * 32))["state"] == "needs_reconciliation"
