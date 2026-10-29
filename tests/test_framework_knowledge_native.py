# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Isolated PostgreSQL authority and persistence with controlled official/SDK HTTP."""

import asyncio
import json
import os
import subprocess
from contextlib import asynccontextmanager
from uuid import uuid4

import pytest
from framework_knowledge_fixtures import ControlledFlycanon, official_fixture
from test_bridge_completion_native import prepared

from firefly_forge.framework_knowledge import FrameworkKnowledge
from firefly_forge.framework_knowledge_models import (
    FrameworkCheckUpdatesInput,
    FrameworkRefreshCancelInput,
    FrameworkRefreshInput,
    FrameworkRefreshResumeInput,
    FrameworkSearchInput,
)
from firefly_forge.framework_knowledge_worker import FrameworkKnowledgeWorker
from firefly_forge.git_workspace import git_environment, trusted_git
from firefly_forge.identity_persistence import ForgeIdentityUserRepository
from firefly_forge.journal import Journal
from firefly_forge.knowledge import KnowledgeConfiguration, KnowledgeManager
from firefly_forge.models import RunInput
from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied, ProjectAccess
from firefly_forge.store import identifier


@asynccontextmanager
async def native_knowledge(store, tmp_path, framework="pyfly"):
    async with prepared(store, tmp_path) as (_, _, original_run, *_):
        remote = ControlledFlycanon()
        knowledge = KnowledgeManager(store, transport=remote.transport())
        knowledge.configure(
            KnowledgeConfiguration(
                values={
                    "enabled": True,
                    "endpoint": "http://127.0.0.1:18500",
                    "tenant_id": "firefly",
                    "api_key": "fictional-native-knowledge-key",
                }
            )
        )
        transport, *_ = official_fixture(framework)
        manager = FrameworkKnowledge(store, knowledge, transport=transport)
        discovery = await manager.check_updates(framework, FrameworkCheckUpdatesInput())
        body = FrameworkRefreshInput(
            version=1,
            discovery_digest=discovery["release"]["digest"],
            expected_status_revision=discovery["status"]["revision"],
            policy_digest=discovery["policy_digest"],
        )
        yield manager, remote, body, original_run


@pytest.mark.parametrize("framework,framework_version", [("pyfly", "26.10.11"), ("larafly", "26.10.4")])
async def test_native_refresh_indexes_exact_version_and_searches_cited_sources(
    store, tmp_path, framework, framework_version
):
    async with native_knowledge(store, tmp_path, framework) as (manager, remote, body, _):
        request_id = uuid4().hex
        queued = await manager.refresh(framework, request_id, body)
        assert await manager.refresh(framework, request_id, body) == queued
        assert not remote.posts
        await FrameworkKnowledgeWorker(manager).advance(request_id)
        final = await manager.get_refresh(framework, request_id)
        assert final["state"] == "ready", final
        assert final["counts"]["files"] == final["counts"]["ready_parts"] == len(remote.posts) == 5
        corpus_ref = final["corpus"]["corpus_ref"]
        result = await manager.search(framework, FrameworkSearchInput(query="service", corpus_ref=corpus_ref))
        assert result["hits"] and result["citations"]
        assert all(c["framework_version"] == framework_version and c["commit"] == "a" * 40 for c in result["citations"])
        sources = await manager.sources(framework, corpus_ref)
        assert len(sources["items"]) == 5 and sources["next_cursor"] is None
        async with store._unit():
            events = await manager.records.page("event", request_id, None, 100)
            assert sum(row.data["kind"] == "refresh_intent" for row in events) == 1
            persisted = await manager.records.by_key("operation", request_id)
            assert "fictional-native-knowledge-key" not in json.dumps(persisted.data)


async def test_native_competing_same_request_retains_one_operation_and_intent(store, tmp_path):
    async with native_knowledge(store, tmp_path) as (manager, remote, body, _):
        request_id = uuid4().hex
        first, second = await asyncio.gather(
            manager.refresh("pyfly", request_id, body), manager.refresh("pyfly", request_id, body)
        )
        assert first == second and not remote.posts
        async with store._unit():
            events = await manager.records.page("event", request_id, None, 100)
            assert sum(row.data["kind"] == "refresh_intent" for row in events) == 1


async def test_native_suspended_administrator_cannot_queue_knowledge_work(store, tmp_path):
    async with native_knowledge(store, tmp_path) as (manager, remote, body, _):
        principal = CURRENT_PRINCIPAL.get()
        assert principal is not None
        actor = ProjectAccess.actor_snapshot(principal)
        async with store._unit():
            users = ForgeIdentityUserRepository()
            user = await users.find_by_id(identifier(actor["user_id"]))
            assert user is not None
            user.enabled = False
            await users.save(user)
        request_id = uuid4().hex
        with pytest.raises(AccessDenied, match="suspended"):
            await manager.refresh("pyfly", request_id, body)
        async with store._unit():
            assert await manager.records.by_key("operation", request_id) is None
        assert not remote.posts


@pytest.mark.parametrize("remote_created", [False, True], ids=["unknown-without-receipt", "created-before-timeout"])
@pytest.mark.parametrize("framework", ["pyfly", "larafly"])
async def test_native_unknown_write_survives_reload_without_duplicate_submission(
    store, tmp_path, remote_created, framework
):
    async with native_knowledge(store, tmp_path, framework) as (manager, remote, body, _):
        request_id = uuid4().hex
        await manager.refresh(framework, request_id, body)
        remote.unknown_after_create = remote_created
        remote.unknown_before_create = not remote_created
        await FrameworkKnowledgeWorker(manager).advance(request_id)
        held = await manager.get_refresh(framework, request_id)
        assert held["uncertain_sources"] == len(remote.posts) == 1
        await manager.cancel(
            framework, request_id, FrameworkRefreshCancelInput(version=1, expected_revision=held["revision"])
        )
        reloaded = FrameworkKnowledge(store, manager.knowledge, transport=manager.transport)
        remote.unknown_after_create = remote.unknown_before_create = False
        await reloaded.observe(framework, request_id)
        observed = await reloaded.get_refresh(framework, request_id)
        assert observed["state"] == "cancelled" and len(remote.posts) == 1
        resume = FrameworkRefreshResumeInput(version=1, expected_revision=observed["revision"])
        if not remote_created:
            assert observed["uncertain_sources"] == 1 and "resume" not in observed["allowed_actions"]
            with pytest.raises(ValueError, match="uncertain|Observe"):
                await reloaded.resume(framework, request_id, resume)
        else:
            assert observed["uncertain_sources"] == 0
            await reloaded.resume(framework, request_id, resume)
            await FrameworkKnowledgeWorker(reloaded).advance(request_id)
            final = await reloaded.get_refresh(framework, request_id)
            assert final["state"] == "ready" and len(remote.posts) == 5
            assert final["spent_budget"]["requests"] > held["spent_budget"]["requests"]


@pytest.mark.parametrize("revocation", ["before", "during_get"])
async def test_native_cancelled_observation_rechecks_administrator_without_new_submission(store, tmp_path, revocation):
    import httpx

    async with native_knowledge(store, tmp_path) as (manager, remote, body, _):
        request_id = uuid4().hex
        await manager.refresh("pyfly", request_id, body)
        remote.unknown_after_create = True
        await FrameworkKnowledgeWorker(manager).advance(request_id)
        held = await manager.get_refresh("pyfly", request_id)
        await manager.cancel(
            "pyfly", request_id, FrameworkRefreshCancelInput(version=1, expected_revision=held["revision"])
        )
        principal = CURRENT_PRINCIPAL.get()
        assert principal is not None
        actor = ProjectAccess.actor_snapshot(principal)

        async def revoke():
            async with store._unit():
                users = ForgeIdentityUserRepository()
                user = await users.find_by_id(identifier(actor["user_id"]))
                assert user is not None
                user.enabled = False
                await users.save(user)

        requests = len(remote.requests)
        before = await manager._get("operation", request_id)
        if revocation == "before":
            await revoke()
        else:

            async def revoked_response(request):
                response = await remote.handle(request)
                if request.method == "GET" and request.url.path == "/api/v1/sources":
                    await revoke()
                return response

            manager.knowledge.transport = httpx.MockTransport(revoked_response)
        with pytest.raises(AccessDenied, match="suspended"):
            await manager.observe("pyfly", request_id)
        observed = await manager._get("operation", request_id)
        assert observed["public"]["state"] == "cancelled" and observed["public"]["uncertain_sources"] == 1
        assert len(remote.posts) == 1 and not observed.get("owner")
        assert all(request.method == "GET" for request in remote.requests[requests:])
        if revocation == "before":
            assert observed == before and len(remote.requests) == requests
        else:
            assert len(remote.requests) == requests + 1


async def test_native_refresh_intent_and_operation_roll_back_together(store, tmp_path, monkeypatch):
    async with native_knowledge(store, tmp_path) as (manager, remote, body, _):
        request_id = uuid4().hex
        save = manager.records.save

        async def fail_after_intent(row):
            result = await save(row)
            if row.kind == "event" and row.data["kind"] == "refresh_intent":
                raise ValueError("Controlled native knowledge rollback")
            return result

        monkeypatch.setattr(manager.records, "save", fail_after_intent)
        with pytest.raises(ValueError, match="rollback"):
            await manager.refresh("pyfly", request_id, body)
        async with store._unit():
            assert await manager.records.by_key("operation", request_id) is None
            assert not await manager.records.page("event", request_id, None, 100)
        assert not remote.posts


async def test_native_run_pin_binds_git_manifests_corpus_and_canonical_journal(store, tmp_path):
    async with native_knowledge(store, tmp_path) as (manager, _, body, original_run):
        request_id = uuid4().hex
        await manager.refresh("pyfly", request_id, body)
        await FrameworkKnowledgeWorker(manager).advance(request_id)
        assert (await manager.get_refresh("pyfly", request_id))["state"] == "ready"

        def git(*arguments):
            return subprocess.run(
                trusted_git(["git", *arguments]),
                cwd=tmp_path,
                env=git_environment({"PATH": os.defpath, "LANG": "C.UTF-8"}),
                check=True,
                capture_output=True,
                text=True,
                timeout=15,
            ).stdout.strip()

        git("init")
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname="fictional-native-product"\ndependencies=["pyfly==26.10.11"]\n'
        )
        git("add", "--", "pyproject.toml")
        git(
            "-c",
            "user.name=Native knowledge fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-m",
            "Record fictional dependency target",
        )
        commit = git("rev-parse", "HEAD")
        store.framework_knowledge = manager
        manager.knowledge.frameworks = manager
        manager.knowledge.journal = Journal(store)
        run = await store.create_run(
            RunInput(
                repository_id=original_run["repository_id"],
                project_id=original_run["project_id"],
                component_id="api",
                agent="bridge",
                brief="Pin fictional native framework references before provider use",
                framework_target={"framework_version": "26.10.11"},
            )
        )
        await manager.knowledge.journal.ensure_plan(run["id"])
        await store.update(run["id"], base_revision=commit)
        run = await store.get_run(run["id"])
        frozen = await manager.freeze_run_target(run, tmp_path, commit, ".")
        corpus = await manager.assert_run_pin(frozen)
        assert frozen["framework_knowledge_pin"]["source_commit"] == commit
        assert frozen["framework_knowledge_pin"]["release_commit"] == "a" * 40
        assert frozen["framework_knowledge_pin"]["corpus_ref"] == corpus["public"]["corpus_ref"]
        context = await manager.search_for_run(frozen, "service")
        assert {hit["kind"] for hit in context["hits"]} >= {"document", "api", "example"}
        entries = await manager.knowledge.journal.entries(run["id"])
        assert sum(entry["kind"] == "framework_knowledge_pin" for entry in entries) == 1
        assert Journal.verify_chain(entries)


def test_native_framework_http_routes_use_di_authority_and_strict_retained_operations(tmp_path, postgres_schema):
    from starlette.testclient import TestClient

    from firefly_forge.api import create_application
    from firefly_forge.framework_knowledge_persistence import FrameworkKnowledgeRecords

    token = "fictional-native-framework-api-token"
    with TestClient(
        create_application(tmp_path / "api", token, role="api"),
        base_url="http://testserver",
        headers={"Authorization": "Bearer " + token, "Origin": "http://testserver"},
        client=("127.0.0.1", 52551),
    ) as http:
        assert http.post("/api/v1/auth/login", json={"token": token}).status_code == 200
        context = http.app.state.runtime.context
        manager = context.get_bean(FrameworkKnowledge)
        assert manager.store is http.app.state.store
        assert manager.knowledge is context.get_bean(KnowledgeManager)
        assert manager.records is context.get_bean(FrameworkKnowledgeRecords)
        assert manager.store.framework_knowledge is manager and manager.knowledge.frameworks is manager
        api = http.get("/api/v1/openapi.json")
        assert api.status_code == 200, api.text
        documented = api.json()["paths"]
        assert "post" in documented["/api/v1/knowledge/frameworks/{framework}/generation-targets/{request_id}"]
        assert "get" in documented["/api/v1/knowledge/frameworks/{framework}/generation-targets/{request_id}"]
        assert "get" in documented["/api/v1/knowledge/frameworks/{framework}/generation-candidates"]
        manager.transport, *_ = official_fixture("larafly")
        configured = http.put(
            "/api/v1/knowledge/configuration",
            json={
                "values": {
                    "enabled": True,
                    "endpoint": "http://127.0.0.1:18500",
                    "tenant_id": "firefly",
                    "api_key": "fictional-native-knowledge-key",
                }
            },
        )
        assert configured.status_code == 200, configured.text
        base = "/api/v1/knowledge/frameworks"
        status = http.get(base + "/status")
        assert status.status_code == 200, status.text
        assert {item["framework"] for item in status.json()["frameworks"]} == {"pyfly", "larafly"}
        checked = http.post(base + "/larafly/check-updates", json={"version": 1, "framework_version": "latest"})
        assert checked.status_code == 200, checked.text
        checked = checked.json()
        body = {
            "version": 1,
            "discovery_digest": checked["release"]["digest"],
            "expected_status_revision": checked["status"]["revision"],
            "policy_digest": checked["policy_digest"],
        }
        request_id = uuid4().hex
        route = base + f"/larafly/refreshes/{request_id}"
        assert http.post(route, json={**body, "token": "unreviewed-field"}).status_code == 422
        submitted = http.post(route, json=body)
        assert submitted.status_code == 201, submitted.text
        assert submitted.json()["state"] == "queued"
        assert http.get(route).json() == submitted.json()
        assert http.post(route, json=body).json() == submitted.json()
        manager.knowledge.transport = ControlledFlycanon().transport()
        assert http.portal is not None
        http.portal.call(FrameworkKnowledgeWorker(manager).advance, request_id)
        ready = http.get(route)
        assert ready.status_code == 200 and ready.json()["state"] == "ready", ready.text
        corpus_ref = ready.json()["corpus"]["corpus_ref"]
        candidates = http.get(base + "/larafly/generation-candidates", params={"corpus_ref": corpus_ref})
        assert candidates.status_code == 200, candidates.text
        assert candidates.json() == {
            "version": 1,
            "framework": "larafly",
            "corpus_ref": corpus_ref,
            "items": [],
            "next_cursor": None,
        }
        assert http.get(base + "/larafly/generation-candidates").status_code == 422
        assert (
            http.get(
                base + "/larafly/generation-candidates", params={"corpus_ref": corpus_ref, "page_size": 0}
            ).status_code
            == 409
        )
        activation = base + f"/larafly/generation-targets/{uuid4().hex}"
        assert http.post(activation, json={"version": 1, "token": "unreviewed-field"}).status_code == 422
        assert http.get(activation).status_code == 404
