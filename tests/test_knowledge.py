# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
import base64
import hashlib
import json
from types import SimpleNamespace

import httpx
import pytest

from firefly_forge.configuration import ConfigurationStore
from firefly_forge.knowledge import KnowledgeConfiguration, KnowledgeManager, snapshot_knowledge


async def repository(identifier):
    return {"id": identifier, "name": "Inventory"}


def manager(tmp_path, handler):
    store = SimpleNamespace(configuration=ConfigurationStore(tmp_path), repository=repository)
    return KnowledgeManager(store, transport=httpx.MockTransport(handler))


def settings(**changes):
    return KnowledgeConfiguration(
        values={
            "enabled": True,
            "endpoint": "http://127.0.0.1:18500",
            "tenant_id": "firefly",
            "api_key": "private-canon-key",
            "auto_memory": False,
            "required": False,
            **changes,
        }
    )


async def test_explorer_lists_actual_sdk_records_in_selected_workspace_only(tmp_path):
    seen = []

    def handler(request):
        seen.append(request.headers["X-Workspace-Id"])
        if request.url.path == "/api/v1/sources":
            items = [
                {
                    "id": "source1",
                    "kind": "text",
                    "status": "ingested",
                    "filename": "stock.py",
                    "content_sha256": "a" * 64,
                    "content_bytes": 30,
                    "n_chunks": 1,
                    "created_at": "2026-10-12T00:00:00Z",
                    "updated_at": "2026-10-12T00:00:00Z",
                }
            ]
        else:
            items = [
                {
                    "id": "knowledge1",
                    "status": "published",
                    "current_version": 2,
                    "title": "Stock behavior",
                    "domain": "software",
                    "created_at": "2026-10-12T00:00:00Z",
                    "updated_at": "2026-10-12T00:00:00Z",
                }
            ]
        return httpx.Response(200, json={"items": items, "total": 1, "limit": 50, "offset": 0})

    knowledge = manager(tmp_path, handler)
    knowledge.configure(settings(framework_workspace="frameworks"))
    result = await knowledge.explore("a" * 32, "project")
    assert result["sources"][0]["filename"] == "stock.py"
    assert result["knowledge"][0]["current_version"] == 2
    assert set(seen) == {"forge-" + "a" * 32}
    seen.clear()
    await knowledge.explore("a" * 32, "framework")
    assert set(seen) == {"frameworks"}
    assert "private-canon-key" not in json.dumps(result)


async def test_agent_token_only_explorer_does_not_borrow_platform_list_permissions(tmp_path):
    def unexpected(request):
        raise AssertionError("Agent-only mode must not call platform list endpoints")

    knowledge = manager(tmp_path, unexpected)
    knowledge.configure(settings(api_key="", agent_token="private-agent-token"))
    result = await knowledge.explore("a" * 32, "project")
    assert result["browsing_available"] is False and result["warnings"]


async def test_actual_sdk_retrieval_is_project_scoped_cited_and_frozen(tmp_path):
    seen = []

    def handler(request):
        seen.append(request)
        assert request.headers["X-API-Key"] == "private-canon-key"
        assert request.headers["X-Tenant-Id"] == "firefly"
        assert request.headers["X-Workspace-Id"] == "forge-" + "a" * 32
        return httpx.Response(
            200,
            json={
                "elapsed_ms": 2,
                "hits": [
                    {
                        "chunk_id": "chunk1",
                        "source_id": "source1",
                        "source_filename": "stock.py",
                        "section_path": "reserve",
                        "knowledge_item_id": "memory1",
                        "knowledge_version": 2,
                        "content": "Reject overselling with a conflict.",
                        "score": 0.9,
                    }
                ],
            },
        )

    knowledge = manager(tmp_path, handler)
    knowledge.configure(settings())
    snapshot = snapshot_knowledge(knowledge.private)
    knowledge.configure(settings(api_key="later-key"))
    result = await knowledge.retrieve(
        {"id": "run1", "repository_id": "a" * 32, "brief": "Reservation", "knowledge_snapshot": snapshot}
    )
    assert "Reject overselling" in result["context"]
    assert result["citations"][0]["source_id"] == "source1"
    assert result["citations"][0]["knowledge_version"] == 2
    assert "private-canon-key" not in json.dumps(snapshot)
    assert len(seen) == 1


@pytest.mark.parametrize("required", [True, False])
async def test_knowledge_outage_is_explicit_and_never_cross_project_fallback(tmp_path, required):
    def handler(request):
        raise httpx.ConnectError("private-canon-key", request=request)

    knowledge = manager(tmp_path, handler)
    knowledge.configure(settings(required=required))
    run = {
        "id": "run1",
        "repository_id": "a" * 32,
        "brief": "Reservation",
        "knowledge_snapshot": snapshot_knowledge(knowledge.private),
    }
    if required:
        with pytest.raises(ValueError, match="Required knowledge"):
            await knowledge.retrieve(run)
    else:
        result = await knowledge.retrieve(run)
        assert result["warnings"] and not result["context"]
        assert "private-canon-key" not in json.dumps(result)


def test_knowledge_configuration_is_write_only_and_rejects_credential_urls(tmp_path):
    knowledge = manager(tmp_path, lambda _: httpx.Response(500))
    public = knowledge.configure(settings())
    assert public["api_key_configured"] is True
    assert "private-canon-key" not in json.dumps(public)
    assert not public["auto_memory"]
    with pytest.raises(ValueError):
        knowledge.configure(settings(endpoint="https://key:secret@canon.example"))


def test_knowledge_redaction_registry_includes_current_and_selected_frozen_credentials(tmp_path):
    from firefly_forge.configuration import redact_secrets

    knowledge = manager(tmp_path, lambda _: httpx.Response(500))
    knowledge.configure(settings(api_key="frozen-canon-key", agent_token="frozen-agent-token"))
    snapshot = snapshot_knowledge(knowledge.private)
    knowledge.configure(settings(api_key="current-canon-key", agent_token="current-agent-token"))
    registered = knowledge.redaction_settings({"knowledge_snapshot": snapshot})
    for secret in ["frozen-canon-key", "frozen-agent-token", "current-canon-key", "current-agent-token"]:
        assert redact_secrets(secret, registered) == "[redacted]"
    assert "current-canon-key" not in json.dumps(knowledge.public())


async def memory_manager(store, tmp_path, handler):
    from firefly_forge.journal import Journal
    from firefly_forge.models import RepositoryInput, RunInput

    repo = await store.add_repository(RepositoryInput(name="Inventory", url="https://github.com/team/inventory.git"))
    run = await store.create_run(RunInput(repository_id=repo["id"], brief="Retain stock checkpoint evidence"))
    await store.update(
        run["id"],
        status="succeeded",
        source_tree="verified-tree",
        summary="Stock reservations reject overselling",
        changed_files=["stock.py"],
        final_verification=[{"argv": ["pytest"], "exit_code": 0}],
    )

    async def validated_workspace(identifier):
        assert identifier == run["id"]
        return tmp_path

    knowledge = KnowledgeManager(
        store, SimpleNamespace(validated_workspace=validated_workspace), transport=httpx.MockTransport(handler)
    )
    knowledge.journal = Journal(store)
    knowledge.configure(settings())
    return knowledge, await store.get_run(run["id"])


def memory_transport(*, fail_knowledge=False):
    saved = {"source_writes": 0, "knowledge_writes": 0, "citations": [], "body": ""}
    timestamp = "2026-10-12T00:00:00Z"

    def handler(request):
        path = request.url.path
        payload = json.loads(request.content) if request.content else {}
        if path.startswith("/api/v1/workspaces/"):
            return httpx.Response(
                200,
                json={
                    "id": path.rsplit("/", 1)[1],
                    "tenant_id": "firefly",
                    "name": "Inventory",
                    "status": "active",
                    "created_at": timestamp,
                    "updated_at": timestamp,
                },
            )
        if path == "/api/v1/sources" and request.method == "POST":
            saved["source_writes"] += 1
            saved["body"] = base64.b64decode(payload["content_base64"]).decode()
            assert json.loads(saved["body"])["source_tree"] == "verified-tree"
            assert payload["kind"] == "text"
        if path.startswith("/api/v1/sources"):
            return httpx.Response(
                200,
                json={
                    "id": "memory-source",
                    "kind": "text",
                    "status": "ingested",
                    "filename": "forge-memory.json",
                    "content_sha256": hashlib.sha256(saved["body"].encode()).hexdigest(),
                    "content_bytes": len(saved["body"].encode()),
                    "n_chunks": 1,
                    "created_at": timestamp,
                    "updated_at": timestamp,
                },
            )
        if path == "/api/v1/search":
            if payload.get("source_ids"):
                assert payload["source_ids"] == ["memory-source"]
            return httpx.Response(
                200,
                json={
                    "elapsed_ms": 1,
                    "hits": [
                        {
                            "chunk_id": "memory-chunk",
                            "source_id": "memory-source",
                            "source_filename": "forge-memory.json",
                            "content": saved["body"],
                            "score": 0.9,
                            "knowledge_item_id": "memory-item" if saved["citations"] else None,
                            "knowledge_version": 1 if saved["citations"] else None,
                        }
                    ],
                },
            )
        if path == "/api/v1/knowledge":
            saved["knowledge_writes"] += 1
            if fail_knowledge:
                return httpx.Response(503, json={"detail": "uncertain external response"})
            saved["citations"] = payload.get("citations", [])
            return httpx.Response(
                201,
                json={
                    **payload,
                    "knowledge_item_id": "memory-item",
                    "version": 1,
                    "status": "published",
                    "created_at": timestamp,
                },
            )
        raise AssertionError(str(request.url))

    return saved, handler


async def test_verified_memory_is_indexed_and_cited_before_canonical_creation(store, tmp_path):
    saved, handler = memory_transport()
    knowledge, run = await memory_manager(store, tmp_path, handler)
    result = await knowledge.remember(run)
    assert saved["source_writes"] == saved["knowledge_writes"] == 1
    assert saved["citations"][0]["source_id"] == "memory-source"
    assert saved["citations"][0]["chunk_id"] == "memory-chunk"
    assert result["source_id"] == "memory-source"
    retrieved = await knowledge.search(run["repository_id"], "Stock reservations")
    assert retrieved["hits"][0]["knowledge_item_id"] == result["item_id"]
    assert "Stock reservations reject overselling" in retrieved["hits"][0]["content"]
    assert await knowledge.remember(await store.get_run(run["id"])) == result
    assert saved["source_writes"] == saved["knowledge_writes"] == 1
    receipts = [entry for entry in await knowledge.journal.entries(run["id"]) if entry["kind"] == "receipt"]
    assert {entry["task_id"] for entry in receipts} == {"memory-source", "memory-knowledge"}


async def test_uncertain_memory_creation_never_repeats_source_or_canonical_write(store, tmp_path):
    saved, handler = memory_transport(fail_knowledge=True)
    knowledge, run = await memory_manager(store, tmp_path, handler)
    with pytest.raises(ValueError, match="could not be saved"):
        await knowledge.remember(run)
    with pytest.raises(ValueError, match="uncertain"):
        await knowledge.remember(await store.get_run(run["id"]))
    assert saved["source_writes"] == saved["knowledge_writes"] == 1


async def test_memory_retrieval_retry_uses_the_durably_admitted_source(store, tmp_path):
    saved, handler = memory_transport()
    retrieval_calls = 0

    def delayed_index(request):
        nonlocal retrieval_calls
        if request.url.path == "/api/v1/search":
            retrieval_calls += 1
            if retrieval_calls == 1:
                return httpx.Response(200, json={"elapsed_ms": 1, "hits": []})
        return handler(request)

    knowledge, run = await memory_manager(store, tmp_path, delayed_index)
    with pytest.raises(ValueError, match="no retrievable chunks"):
        await knowledge.remember(run)
    assert saved["source_writes"] == 1 and saved["knowledge_writes"] == 0
    result = await knowledge.remember(await store.get_run(run["id"]))
    assert result["source_id"] == "memory-source"
    assert saved["source_writes"] == saved["knowledge_writes"] == 1


async def test_uncertain_memory_source_write_requires_reconciliation(store, tmp_path):
    saved, handler = memory_transport()

    def uncertain_source(request):
        if request.url.path == "/api/v1/sources" and request.method == "POST":
            saved["source_writes"] += 1
            return httpx.Response(503, json={"detail": "Response lost after admission"})
        return handler(request)

    knowledge, run = await memory_manager(store, tmp_path, uncertain_source)
    with pytest.raises(ValueError, match="could not be saved"):
        await knowledge.remember(run)
    with pytest.raises(ValueError, match="uncertain"):
        await knowledge.remember(await store.get_run(run["id"]))
    assert saved["source_writes"] == 1 and saved["knowledge_writes"] == 0


async def test_lost_factory_lease_stops_memory_before_another_external_write(store, tmp_path):
    saved, handler = memory_transport()
    ownership_lost = False

    def lose_lease_after_source(request):
        nonlocal ownership_lost
        result = handler(request)
        if request.url.path == "/api/v1/sources" and request.method == "POST":
            ownership_lost = True
        return result

    knowledge, run = await memory_manager(store, tmp_path, lose_lease_after_source)

    async def assert_ownership():
        if ownership_lost:
            raise RuntimeError("PostgreSQL execution ownership was lost")

    knowledge.engine.enforce_ownership = True
    knowledge.engine.assert_ownership = assert_ownership
    with pytest.raises(RuntimeError, match="ownership was lost"):
        await knowledge.remember(run)
    assert saved["source_writes"] == 1 and saved["knowledge_writes"] == 0
