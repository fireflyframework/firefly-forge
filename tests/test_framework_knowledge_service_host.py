# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Durable refresh intent and source-isolated corpus selection with controlled storage."""

import copy
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest


class Records:
    def __init__(self):
        self.rows = {}

    async def by_key(self, kind, key, **kwargs):
        return self.rows.get((kind, key))

    async def save(self, row):
        self.rows[row.kind, row.key] = row
        return row

    async def page(self, kind, parent, cursor, size):
        return [
            r
            for (k, key), r in sorted(self.rows.items())
            if k == kind and r.parent == parent and (not cursor or key > cursor)
        ][:size]

    async def total(self, kind, parent):
        return sum(r.kind == kind and r.parent == parent for r in self.rows.values())

    async def count_state(self, kind, parent, state):
        return sum(r.kind == kind and r.parent == parent and r.data.get("state") == state for r in self.rows.values())

    async def part_counts(self, source_ref):
        result = {}
        for row in self.rows.values():
            if row.kind == "part" and row.data["source_ref"] == source_ref:
                state = row.data["state"]
                result[state] = result.get(state, 0) + 1
        return result

    async def remote_source(self, corpus_ref, source_id):
        return [
            row
            for row in self.rows.values()
            if row.kind == "part" and row.parent == corpus_ref and row.data["source_id"] == source_id
        ]

    async def progress_counts(self, corpus_ref):
        from firefly_forge.framework_knowledge_models import FrameworkCountsV1

        counts = FrameworkCountsV1().model_dump()
        for row in self.rows.values():
            if row.parent != corpus_ref:
                continue
            if row.kind == "source":
                public = row.data["public"]
                counts["total_files"] += 1
                if public["state"] == "excluded":
                    counts["excluded_files"] += 1
                else:
                    counts["files"] += 1
                    counts["bytes"] += public.get("bytes", 0)
                    if public.get("kind") in {"document", "example", "api"}:
                        counts[{"document": "documents", "example": "examples", "api": "api"}[public["kind"]]] += 1
            elif row.kind == "part":
                counts["parts"] += row.data["state"] != "excluded"
                counts["ready_parts"] += row.data["state"] == "ready"
                counts["excluded_parts"] += row.data["state"] == "excluded"
        return counts

    async def ready_exists(self, framework):
        return any(
            row.kind == "corpus" and row.framework == framework and row.data["public"]["state"] == "ready"
            for row in self.rows.values()
        )

    async def ready_version(self, framework, version):
        return [
            row
            for row in self.rows.values()
            if row.kind == "corpus"
            and row.framework == framework
            and row.data["public"]["framework_version"] == version
            and row.data["public"]["state"] == "ready"
        ]


class Store:
    def __init__(self, records):
        self.records = records
        self.schema = "fixture"
        self.configuration = SimpleNamespace(settings_generation=object())

    @asynccontextmanager
    async def _unit(self):
        before = copy.deepcopy(self.records.rows)

        class Session:
            async def execute(self, *args):
                pass

        try:
            yield Session()
        except BaseException:
            self.records.rows = before
            raise


async def test_cached_status_is_not_falsely_current_without_ready_index():
    from firefly_forge.framework_knowledge import FrameworkKnowledge

    records = Records()
    manager = FrameworkKnowledge(Store(records), SimpleNamespace(), records=records)

    async def authorize(*args, **kwargs):
        return {"user_id": "a" * 32}

    manager._authorize = authorize
    result = await manager.status()
    assert len(result["frameworks"]) == 2
    assert all(x["state"] == "missing" and x["current_corpus"] is None for x in result["frameworks"])
    assert all(x["counts"]["total_files"] == 0 and "search" not in x["allowed_actions"] for x in result["frameworks"])


async def test_search_refuses_missing_or_unready_corpus_before_remote_io():
    from firefly_forge.framework_knowledge import FrameworkKnowledge
    from firefly_forge.framework_knowledge_models import FrameworkSearchInput

    records = Records()
    manager = FrameworkKnowledge(Store(records), SimpleNamespace(), records=records)

    async def authorize(*args, **kwargs):
        return {"user_id": "a" * 32}

    manager._authorize = authorize
    with pytest.raises(ValueError, match="ready|indexed|corpus"):
        await manager.search("pyfly", FrameworkSearchInput(query="service", corpus_ref="b" * 32))


async def test_discovery_is_durable_but_not_an_index_and_refresh_identity_is_retained():
    from test_framework_knowledge_upstream_host import official_fixture

    from firefly_forge.framework_knowledge import FrameworkKnowledge
    from firefly_forge.framework_knowledge_models import FrameworkCheckUpdatesInput, FrameworkRefreshInput

    records = Records()
    transport, requests, *_ = official_fixture()
    manager = FrameworkKnowledge(Store(records), SimpleNamespace(), records=records, transport=transport)

    async def authorize(*args, **kwargs):
        return {"user_id": "a" * 32, "global_admin": True}

    manager._authorize = authorize
    manager._connection_snapshot = lambda: ({"revision": "d" * 32}, "e" * 64)
    result = await manager.check_updates("pyfly", FrameworkCheckUpdatesInput())
    assert result["status"]["state"] == "missing" and result["status"]["latest_stable"] is not None
    assert result["status"]["current_corpus"] is None
    assert any(row.kind == "event" and row.data["kind"] == "discovery_intent" for row in records.rows.values())
    body = FrameworkRefreshInput(
        version=1,
        discovery_digest=result["release"]["digest"],
        expected_status_revision=result["status"]["revision"],
        policy_digest=result["policy_digest"],
    )
    count = len(requests)
    first = await manager.refresh("pyfly", "f" * 32, body)
    assert first["state"] == "queued" and first["counts"]["files"] == 0 and len(requests) == count
    assert await manager.refresh("pyfly", "f" * 32, body) == first
    assert any(row.kind == "event" and row.data["kind"] == "refresh_intent" for row in records.rows.values())
    changed = body.model_copy(update={"make_current": False})
    with pytest.raises(ValueError, match="identity|different"):
        await manager.refresh("pyfly", "f" * 32, changed)


async def test_late_discovery_actor_revocation_never_changes_selected_release():
    from test_framework_knowledge_upstream_host import official_fixture

    from firefly_forge.framework_knowledge import FrameworkKnowledge
    from firefly_forge.framework_knowledge_models import FrameworkCheckUpdatesInput
    from firefly_forge.project_access import AccessDenied

    records = Records()
    transport, requests, *_ = official_fixture()
    manager = FrameworkKnowledge(Store(records), SimpleNamespace(), records=records, transport=transport)

    async def authorize(*args, **kwargs):
        if len(requests) > 2:
            raise AccessDenied("revoked")
        return {"user_id": "a" * 32, "global_admin": True}

    manager._authorize = authorize
    with pytest.raises(AccessDenied):
        await manager.check_updates("pyfly", FrameworkCheckUpdatesInput())
    status = await manager._status("pyfly")
    assert status["latest_stable"] is None and status["current_corpus"] is None


async def test_refresh_worker_commits_part_intent_before_post_and_never_replays_unknown(tmp_path):
    from test_framework_knowledge_upstream_host import official_fixture

    from firefly_forge.framework_knowledge import FrameworkKnowledge
    from firefly_forge.framework_knowledge_models import FrameworkCheckUpdatesInput, FrameworkRefreshInput
    from firefly_forge.framework_knowledge_worker import FrameworkKnowledgeWorker

    records = Records()
    store = Store(records)
    store.configuration.directory = tmp_path
    transport, *_ = official_fixture()
    client = SimpleNamespace()
    posts = []

    async def submit(payload):
        pending = [r for r in records.rows.values() if r.kind == "part" and r.data["state"] == "submitting"]
        assert len(pending) == 1
        posts.append(payload)
        raise TimeoutError("controlled unknown remote result")

    client.submit_source = submit

    async def list_sources(**kwargs):
        return SimpleNamespace(items=[], total=0)

    client.list_sources = list_sources

    async def workspace(*args, **kwargs):
        return None

    client.get_workspace = workspace
    client.create_workspace = workspace

    class Knowledge:
        def settings(self, *args):
            return {"enabled": True, "endpoint": "https://knowledge.example", "api_key": "fixture", "top_k": 5}

        @asynccontextmanager
        async def client(self, *args):
            yield client

        async def _assert_ownership(self):
            pass

    manager = FrameworkKnowledge(store, Knowledge(), records=records, transport=transport)

    async def authorize(*args, **kwargs):
        return {"user_id": "a" * 32, "global_admin": True}

    manager._authorize = authorize
    from firefly_forge.product_provenance import content_digest

    manager._connection_snapshot = lambda: ({"revision": "d" * 32}, content_digest(manager.knowledge.settings()))
    result = await manager.check_updates("pyfly", FrameworkCheckUpdatesInput())
    body = FrameworkRefreshInput(
        version=1,
        discovery_digest=result["release"]["digest"],
        expected_status_revision=result["status"]["revision"],
        policy_digest=result["policy_digest"],
    )
    await manager.refresh("pyfly", "f" * 32, body)
    await FrameworkKnowledgeWorker(manager).advance("f" * 32)
    held = await manager.get_refresh("pyfly", "f" * 32)
    assert held["state"] == "needs_reconciliation" and held["uncertain_sources"] == 1 and len(posts) == 1
    await manager.observe("pyfly", "f" * 32)
    assert len(posts) == 1
    assert (await manager.get_refresh("pyfly", "f" * 32))["state"] == "needs_reconciliation"


async def test_full_official_fixture_uses_real_sdk_and_exact_workspace_citations(tmp_path):
    import base64
    import hashlib
    import json

    import httpx
    from test_framework_knowledge_upstream_host import official_fixture

    from firefly_forge.configuration import ConfigurationStore
    from firefly_forge.framework_knowledge import FrameworkKnowledge
    from firefly_forge.framework_knowledge_models import (
        FrameworkCheckUpdatesInput,
        FrameworkRefreshInput,
        FrameworkSearchInput,
    )
    from firefly_forge.framework_knowledge_worker import FrameworkKnowledgeWorker
    from firefly_forge.knowledge import KnowledgeConfiguration, KnowledgeManager

    records = Records()
    store = Store(records)
    store.configuration = ConfigurationStore(tmp_path)
    remote = {}
    seen = []
    timestamp = "2026-10-26T00:00:00Z"

    def handler(request):
        seen.append(request)
        path = request.url.path
        payload = json.loads(request.content) if request.content else {}
        workspace = request.headers["X-Workspace-Id"]
        assert workspace.startswith("ffw-pyfly-")
        if path.startswith("/api/v1/workspaces/"):
            return httpx.Response(
                200,
                json={
                    "id": workspace,
                    "tenant_id": "firefly",
                    "name": "Official",
                    "status": "active",
                    "created_at": timestamp,
                    "updated_at": timestamp,
                },
            )
        if path == "/api/v1/sources" and request.method == "POST":
            body = base64.b64decode(payload["content_base64"])
            identity = "source-" + str(len(remote) + 1)
            remote[identity] = {
                "id": identity,
                "kind": "text",
                "status": "ingested",
                "filename": payload["filename"],
                "content_sha256": hashlib.sha256(body).hexdigest(),
                "content_bytes": len(body),
                "n_chunks": 1,
                "metadata": payload["metadata"],
                "created_at": timestamp,
                "updated_at": timestamp,
                "body": body.decode(),
            }
            return httpx.Response(201, json=remote[identity])
        if path.startswith("/api/v1/sources/"):
            return httpx.Response(200, json=remote[path.rsplit("/", 1)[1]])
        if path == "/api/v1/search":
            identities = payload.get("source_ids") or list(remote)[:1]
            return httpx.Response(
                200,
                json={
                    "elapsed_ms": 1,
                    "hits": [
                        {
                            "chunk_id": "chunk-" + i,
                            "source_id": i,
                            "source_filename": remote[i]["filename"],
                            "content": remote[i]["body"],
                            "score": 0.9,
                            "knowledge_version": None,
                        }
                        for i in identities
                    ],
                },
            )
        raise AssertionError(path)

    knowledge = KnowledgeManager(store, transport=httpx.MockTransport(handler))
    knowledge.configure(
        KnowledgeConfiguration(
            values={
                "enabled": True,
                "endpoint": "http://127.0.0.1:18500",
                "tenant_id": "firefly",
                "api_key": "private-fixture-key",
            }
        )
    )
    transport, *_ = official_fixture()
    manager = FrameworkKnowledge(store, knowledge, records=records, transport=transport)

    async def authorize(*args, **kwargs):
        return {"user_id": "a" * 32, "global_admin": True}

    manager._authorize = authorize
    result = await manager.check_updates("pyfly", FrameworkCheckUpdatesInput())
    body = FrameworkRefreshInput(
        version=1,
        discovery_digest=result["release"]["digest"],
        expected_status_revision=result["status"]["revision"],
        policy_digest=result["policy_digest"],
    )
    queued = await manager.refresh("pyfly", "f" * 32, body)
    await manager.observe("pyfly", "f" * 32)
    assert (await manager.get_refresh("pyfly", "f" * 32))["state"] == "queued"
    await FrameworkKnowledgeWorker(manager).advance("f" * 32)
    final = await manager.get_refresh("pyfly", "f" * 32)
    assert final["state"] == "ready", await manager._get("operation", "f" * 32)
    assert final["counts"]["files"] == 5 and final["counts"]["ready_parts"] == 5 and len(remote) == 5
    result = await manager.search(
        "pyfly", FrameworkSearchInput(query="service", corpus_ref=queued["corpus"]["corpus_ref"])
    )
    assert result["hits"] and result["citations"][0]["commit"] == "a" * 40
    assert result["citations"][0]["framework_version"] == "26.10.11"
    assert all("private-fixture-key" not in json.dumps(x.data) for x in records.rows.values())


async def test_inventory_pages_accept_their_own_cursors_beyond_fifty():
    from firefly_forge.framework_knowledge import FrameworkKnowledge

    records = Records()
    manager = FrameworkKnowledge(Store(records), SimpleNamespace(), records=records)

    async def authorize(*args, **kwargs):
        return {"user_id": "a" * 32}

    manager._authorize = authorize
    for n in range(137):
        key = f"{n:032x}"
        async with manager.store._unit():
            await manager._put("corpus", key, "pyfly", {"public": {"corpus_ref": key}}, "pyfly")
            await manager._put("source", key, "pyfly", {"public": {"source_ref": key}}, "a" * 32)
    for kind, parent in (("corpus", "pyfly"), ("source", "a" * 32)):
        cursor = None
        all_items = []
        while True:
            result = await manager._page("pyfly", kind, parent, cursor, 50)
            all_items.extend(result["items"])
            cursor = result["next_cursor"]
            assert result["total"] == 137
            if cursor is None:
                break
        assert len(all_items) == 137


async def test_budget_renewal_retains_spent_and_cancel_has_cas_and_no_reset():
    from firefly_forge.framework_knowledge import FrameworkKnowledge
    from firefly_forge.framework_knowledge_models import FrameworkBudgetRenewalInput, FrameworkRefreshCancelInput
    from firefly_forge.product_provenance import content_digest

    records = Records()
    manager = FrameworkKnowledge(Store(records), SimpleNamespace(), records=records)

    async def authorize(*args, **kwargs):
        return {"user_id": "a" * 32, "global_admin": True}

    manager._authorize = authorize
    public = {
        "request_id": "f" * 32,
        "framework": "pyfly",
        "state": "needs_reconciliation",
        "stage": "submitting",
        "spent_budget": {"requests": 3, "bytes": 123, "seconds": 10.0},
        "remaining_budget": {"requests": 0, "bytes": 0, "seconds": 0.0},
        "uncertain_sources": 1,
        "allowed_actions": ["observe"],
        "sanitized_error": "retained",
    }
    public["revision"] = content_digest(public)
    data = {
        "public": public,
        "actor": await authorize(),
        "policy": {"budget": {"requests": 3, "bytes": 123, "seconds": 10}},
    }
    async with manager.store._unit():
        await manager._put("operation", "f" * 32, "pyfly", data, "pyfly")
    body = FrameworkBudgetRenewalInput(
        version=1, expected_revision=public["revision"], total_budget={"requests": 17, "bytes": 1000, "seconds": 90}
    )
    renewed = await manager.renew_budget("pyfly", "f" * 32, body)
    assert renewed["spent_budget"] == public["spent_budget"] and renewed["remaining_budget"]["requests"] == 14
    assert renewed["state"] == "needs_reconciliation" and renewed["uncertain_sources"] == 1
    assert await manager.renew_budget("pyfly", "f" * 32, body) == renewed
    with pytest.raises(ValueError):
        await manager.cancel(
            "pyfly", "f" * 32, FrameworkRefreshCancelInput(version=1, expected_revision=public["revision"])
        )
    cancelled = await manager.cancel(
        "pyfly", "f" * 32, FrameworkRefreshCancelInput(version=1, expected_revision=renewed["revision"])
    )
    assert cancelled["state"] == "cancelled" and cancelled["spent_budget"] == public["spent_budget"]
    assert cancelled["uncertain_sources"] == 1


async def test_native_reader_sees_no_admin_update_actions():
    from firefly_forge.framework_knowledge import FrameworkKnowledge

    records = Records()
    manager = FrameworkKnowledge(Store(records), SimpleNamespace(), records=records)

    async def authorize(*args, **kwargs):
        return {"user_id": "a" * 32, "global_admin": False}

    manager._authorize = authorize
    result = await manager.status()
    assert all(not item["allowed_actions"] for item in result["frameworks"])


async def test_cancelled_operation_resumes_same_identity_without_reset_and_unknown_blocks():
    from firefly_forge.framework_knowledge import FrameworkKnowledge
    from firefly_forge.framework_knowledge_models import FrameworkRefreshResumeInput
    from firefly_forge.product_provenance import content_digest

    records = Records()
    manager = FrameworkKnowledge(Store(records), SimpleNamespace(), records=records)

    async def authorize(*args, **kwargs):
        return {"user_id": "a" * 32, "global_admin": True}

    manager._authorize = authorize
    public = {
        "request_id": "f" * 32,
        "framework": "pyfly",
        "state": "cancelled",
        "stage": "submitting",
        "spent_budget": {"requests": 3, "bytes": 123, "seconds": 10.0},
        "remaining_budget": {"requests": 14, "bytes": 877, "seconds": 80.0},
        "uncertain_sources": 1,
        "allowed_actions": ["observe"],
    }
    public["revision"] = content_digest(public)
    data = {
        "public": public,
        "actor": await authorize(),
        "cancel_requested": True,
        "policy": {"budget": {"requests": 17, "bytes": 1000, "seconds": 90}},
    }
    async with manager.store._unit():
        await manager._put("operation", "f" * 32, "pyfly", data, "pyfly")
    with pytest.raises(ValueError, match="uncertain|observe"):
        await manager.resume(
            "pyfly", "f" * 32, FrameworkRefreshResumeInput(version=1, expected_revision=public["revision"])
        )
    data["public"]["uncertain_sources"] = 0
    data["public"]["revision"] = content_digest(data["public"])
    async with manager.store._unit():
        await manager._put("operation", "f" * 32, "pyfly", data, "pyfly")
    body = FrameworkRefreshResumeInput(version=1, expected_revision=data["public"]["revision"])
    resumed = await manager.resume("pyfly", "f" * 32, body)
    assert resumed["state"] == "queued" and resumed["request_id"] == "f" * 32
    assert resumed["spent_budget"] == public["spent_budget"]
    assert await manager.resume("pyfly", "f" * 32, body) == resumed


async def test_shared_controlled_flycanon_fixture_completes_real_sdk_ingestion(tmp_path):
    from framework_knowledge_fixtures import ControlledFlycanon, official_fixture

    from firefly_forge.configuration import ConfigurationStore
    from firefly_forge.framework_knowledge import FrameworkKnowledge
    from firefly_forge.framework_knowledge_models import FrameworkCheckUpdatesInput, FrameworkRefreshInput
    from firefly_forge.framework_knowledge_worker import FrameworkKnowledgeWorker
    from firefly_forge.knowledge import KnowledgeConfiguration, KnowledgeManager

    records = Records()
    store = Store(records)
    store.configuration = ConfigurationStore(tmp_path)
    remote = ControlledFlycanon()
    knowledge = KnowledgeManager(store, transport=remote.transport())
    knowledge.configure(
        KnowledgeConfiguration(
            values={
                "enabled": True,
                "endpoint": "http://127.0.0.1:18500",
                "tenant_id": "forge",
                "api_key": "private-controlled-fixture",
            }
        )
    )
    manager = FrameworkKnowledge(store, knowledge, records=records, transport=official_fixture()[0])

    async def authorize(*args, **kwargs):
        return {"user_id": "a" * 32, "global_admin": True}

    manager._authorize = authorize
    discovery = await manager.check_updates("pyfly", FrameworkCheckUpdatesInput())
    await manager.refresh(
        "pyfly",
        "f" * 32,
        FrameworkRefreshInput(
            version=1,
            discovery_digest=discovery["release"]["digest"],
            expected_status_revision=discovery["status"]["revision"],
            policy_digest=discovery["policy_digest"],
        ),
    )
    await FrameworkKnowledgeWorker(manager).advance("f" * 32)
    final = await manager.get_refresh("pyfly", "f" * 32)
    assert final["state"] == "ready" and len(remote.posts) == 5


async def test_budget_exhaustion_before_post_can_renew_without_uncertain_replay(tmp_path, monkeypatch):
    from framework_knowledge_fixtures import ControlledFlycanon, official_fixture

    from firefly_forge.configuration import ConfigurationStore
    from firefly_forge.framework_knowledge import FrameworkKnowledge
    from firefly_forge.framework_knowledge_models import (
        FrameworkBudgetRenewalInput,
        FrameworkCheckUpdatesInput,
        FrameworkRefreshInput,
    )
    from firefly_forge.framework_knowledge_worker import FrameworkKnowledgeWorker
    from firefly_forge.knowledge import KnowledgeConfiguration, KnowledgeManager

    monkeypatch.setenv("FORGE_FRAMEWORK_KNOWLEDGE_REQUESTS", "6")
    records = Records()
    store = Store(records)
    store.configuration = ConfigurationStore(tmp_path)
    remote = ControlledFlycanon()
    knowledge = KnowledgeManager(store, transport=remote.transport())
    knowledge.configure(
        KnowledgeConfiguration(
            values={
                "enabled": True,
                "endpoint": "http://127.0.0.1:18500",
                "tenant_id": "forge",
                "api_key": "private-controlled",
            }
        )
    )
    manager = FrameworkKnowledge(store, knowledge, records=records, transport=official_fixture()[0])

    async def authorize(*args, **kwargs):
        return {"user_id": "a" * 32, "global_admin": True}

    manager._authorize = authorize
    discovery = await manager.check_updates("pyfly", FrameworkCheckUpdatesInput())
    await manager.refresh(
        "pyfly",
        "f" * 32,
        FrameworkRefreshInput(
            version=1,
            discovery_digest=discovery["release"]["digest"],
            expected_status_revision=discovery["status"]["revision"],
            policy_digest=discovery["policy_digest"],
        ),
    )
    await FrameworkKnowledgeWorker(manager).advance("f" * 32)
    held = await manager.get_refresh("pyfly", "f" * 32)
    assert held["state"] == "needs_reconciliation" and held["uncertain_sources"] == 0 and not remote.posts
    assert held["spent_budget"]["requests"] == 6
    data = await manager._get("operation", "f" * 32)
    budget = {**data["policy"]["budget"], "requests": 100}
    await manager.renew_budget(
        "pyfly",
        "f" * 32,
        FrameworkBudgetRenewalInput(version=1, expected_revision=held["revision"], total_budget=budget),
    )
    await FrameworkKnowledgeWorker(manager).advance("f" * 32)
    complete = await manager.get_refresh("pyfly", "f" * 32)
    assert complete["state"] == "ready" and len(remote.posts) == 5 and complete["spent_budget"]["requests"] > 6


async def host_refresh_case(tmp_path, *, transport=None):
    from framework_knowledge_fixtures import ControlledFlycanon, official_fixture

    from firefly_forge.configuration import ConfigurationStore
    from firefly_forge.framework_knowledge import FrameworkKnowledge
    from firefly_forge.framework_knowledge_models import FrameworkCheckUpdatesInput, FrameworkRefreshInput
    from firefly_forge.knowledge import KnowledgeConfiguration, KnowledgeManager

    records = Records()
    store = Store(records)
    store.configuration = ConfigurationStore(tmp_path)
    remote = ControlledFlycanon()
    knowledge = KnowledgeManager(store, transport=remote.transport())
    knowledge.configure(
        KnowledgeConfiguration(
            values={
                "enabled": True,
                "endpoint": "http://127.0.0.1:18500",
                "tenant_id": "forge",
                "api_key": "private-controlled",
            }
        )
    )
    manager = FrameworkKnowledge(store, knowledge, records=records, transport=transport or official_fixture()[0])

    async def authorize(*args, **kwargs):
        return {"user_id": "a" * 32, "global_admin": True}

    manager._authorize = authorize
    discovery = await manager.check_updates("pyfly", FrameworkCheckUpdatesInput())
    await manager.refresh(
        "pyfly",
        "f" * 32,
        FrameworkRefreshInput(
            version=1,
            discovery_digest=discovery["release"]["digest"],
            expected_status_revision=discovery["status"]["revision"],
            policy_digest=discovery["policy_digest"],
        ),
    )
    return manager, remote


@pytest.mark.parametrize("remote_created", [False, True])
async def test_cancel_reload_observe_preserves_unknown_and_all_spent_read_only(tmp_path, remote_created):
    from firefly_forge.framework_knowledge import FrameworkKnowledge
    from firefly_forge.framework_knowledge_models import FrameworkRefreshCancelInput, FrameworkRefreshResumeInput
    from firefly_forge.framework_knowledge_worker import FrameworkKnowledgeWorker

    manager, remote = await host_refresh_case(tmp_path)
    remote.unknown_after_create = remote_created
    remote.unknown_before_create = not remote_created
    await FrameworkKnowledgeWorker(manager).advance("f" * 32)
    held = await manager.get_refresh("pyfly", "f" * 32)
    assert held["uncertain_sources"] == 1 and len(remote.posts) == 1
    await manager.cancel("pyfly", "f" * 32, FrameworkRefreshCancelInput(version=1, expected_revision=held["revision"]))
    reloaded = FrameworkKnowledge(
        manager.store, manager.knowledge, records=manager.records, transport=manager.transport
    )
    reloaded._authorize = manager._authorize
    remote.unknown_after_create = False
    remote.unknown_before_create = False
    cancelled = await reloaded.get_refresh("pyfly", "f" * 32)
    rows = copy.deepcopy({key: row.data for key, row in manager.records.rows.items()})
    requests = len(remote.requests)
    await reloaded.observe("pyfly", "f" * 32)
    observed = await reloaded.get_refresh("pyfly", "f" * 32)
    assert observed == cancelled and observed["state"] == "cancelled"
    assert {key: row.data for key, row in manager.records.rows.items()} == rows
    assert len(remote.posts) == 1 and len(remote.requests) == requests
    assert observed["spent_budget"] == held["spent_budget"]
    assert observed["uncertain_sources"] == 1 and "resume" not in observed["allowed_actions"]
    with pytest.raises(ValueError, match="uncertain|Observe"):
        await reloaded.resume(
            "pyfly", "f" * 32, FrameworkRefreshResumeInput(version=1, expected_revision=observed["revision"])
        )


async def test_discovery_rate_limit_retains_outcome_spent_and_refuses_early_recheck():
    from datetime import UTC, datetime, timedelta

    import httpx

    from firefly_forge.framework_knowledge import FrameworkKnowledge
    from firefly_forge.framework_knowledge_models import FrameworkCheckUpdatesInput
    from firefly_forge.framework_knowledge_upstream import OfficialRateLimited

    records = Records()
    requests = []

    def limited(request):
        requests.append(request)
        reset = int((datetime.now(UTC) + timedelta(hours=1)).timestamp())
        return httpx.Response(429, headers={"x-ratelimit-reset": str(reset)})

    manager = FrameworkKnowledge(
        Store(records), SimpleNamespace(), records=records, transport=httpx.MockTransport(limited)
    )

    async def authorize(*args, **kwargs):
        return {"user_id": "a" * 32, "global_admin": True}

    manager._authorize = authorize
    with pytest.raises(OfficialRateLimited):
        await manager.check_updates("pyfly", FrameworkCheckUpdatesInput())
    outcomes = [r.data for r in records.rows.values() if r.kind == "event" and r.data["kind"] == "discovery_outcome"]
    assert len(outcomes) == 1 and outcomes[0]["state"] == "held"
    assert outcomes[0]["spent_budget"]["requests"] == 1 and outcomes[0]["retry_at"]
    with pytest.raises(ValueError, match="rate|retry|Retry"):
        await manager.check_updates("pyfly", FrameworkCheckUpdatesInput())
    assert len(requests) == 1


async def test_worker_cancellation_preserves_uncertain_post_before_releasing_owner(tmp_path):
    import asyncio

    from firefly_forge.framework_knowledge_worker import FrameworkKnowledgeWorker

    manager, remote = await host_refresh_case(tmp_path)
    reached = asyncio.Event()

    async def pause(payload):
        reached.set()
        await asyncio.Future()

    remote.before_post = pause
    task = asyncio.create_task(FrameworkKnowledgeWorker(manager).advance("f" * 32))
    await reached.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    held = await manager.get_refresh("pyfly", "f" * 32)
    assert held["state"] == "needs_reconciliation" and held["uncertain_sources"] == 1
    retained = await manager._get("operation", "f" * 32)
    assert not retained.get("owner")


async def test_worker_writer_rejects_configuration_epoch_changed_after_outside_preparation(tmp_path):
    from firefly_forge.framework_knowledge_worker import FrameworkKnowledgeWorker
    from firefly_forge.knowledge import KnowledgeConfiguration

    manager, _ = await host_refresh_case(tmp_path)
    worker = FrameworkKnowledgeWorker(manager)
    worker.request_id = "f" * 32
    record = await manager._get("operation", "f" * 32)
    record["owner"] = worker.owner
    await manager._put("operation", "f" * 32, "pyfly", record, "pyfly")
    await worker._current()
    manager.knowledge.configure(KnowledgeConfiguration(values={"api_key": "changed-controlled"}))
    with pytest.raises(ValueError, match="connection|configuration"):
        await worker._current(verify_connection=False)


async def test_retrieval_refuses_remote_source_drift_after_the_corpus_became_ready(tmp_path):
    from firefly_forge.framework_knowledge_models import FrameworkSearchInput
    from firefly_forge.framework_knowledge_worker import FrameworkKnowledgeWorker

    manager, remote = await host_refresh_case(tmp_path)
    await FrameworkKnowledgeWorker(manager).advance("f" * 32)
    complete = await manager.get_refresh("pyfly", "f" * 32)
    assert complete["state"] == "ready"
    first = next(iter(remote.sources))
    remote.sources[first]["content_sha256"] = "f" * 64
    with pytest.raises(ValueError, match="source|immutable|citation"):
        await manager.search(
            "pyfly", FrameworkSearchInput(query="service", corpus_ref=complete["corpus"]["corpus_ref"])
        )


async def test_failed_submission_retains_elapsed_work_time_without_reset(tmp_path, monkeypatch):
    import firefly_forge.framework_knowledge_worker as worker_module

    manager, remote = await host_refresh_case(tmp_path)
    clock = [0.0]
    monkeypatch.setattr(worker_module, "time", SimpleNamespace(monotonic=lambda: clock[0]))

    async def timeout(payload):
        clock[0] = 10.0
        raise TimeoutError("Controlled dispatch duration")

    remote.before_post = timeout
    await worker_module.FrameworkKnowledgeWorker(manager).advance("f" * 32)
    held = await manager.get_refresh("pyfly", "f" * 32)
    assert held["state"] == "needs_reconciliation" and held["spent_budget"]["seconds"] >= 10
