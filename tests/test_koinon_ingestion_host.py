# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Versioned native writes use controlled HTTP and real journal code, never PostgreSQL."""

import base64
import hashlib
import json

import httpx
import pytest
from test_koinon_knowledge_host import context, entry
from test_koinon_sql_host import sql_manager as sql_manager


def source_body(**changes):
    from firefly_forge.knowledge_ingestion import KoinonSourceInput

    return KoinonSourceInput.model_validate(
        {
            "request_id": "e" * 32,
            "filename": "reference.md",
            "content_base64": base64.b64encode(b"Verified source reference").decode(),
            **changes,
        }
    )


@pytest.fixture
async def writes(sql_manager):
    knowledge, store, journal, state, access = sql_manager
    state.update(source_posts=0, memory_posts=0, lost=False)

    async def handler(request):
        path = request.url.path
        if path.endswith("/context"):
            value = context()
            if state.get("remote_allowed", True):
                value["workspace_permissions"]["workspace"] += ["catalog:write", "ingest:write"]
        elif path.endswith("/sources/upload"):
            state["source_posts"] += 1
            assert state.get("project_upload") or any(
                row.kind == "koinon_source_intent" for row in journal.records.rows
            )
            assert store.run.data["active_task"] == "implement"
            assert b"Verified source reference" in request.content
            if state["lost"]:
                raise httpx.ReadTimeout("Controlled lost write reply", request=request)
            source = entry("source", "source", parent="collection")
            source["metadata"] = {"sha256": hashlib.sha256(b"Verified source reference").hexdigest()}
            source["name"] = "reference.md"
            value = {"source": source, "assets": [entry("document", "document", parent="source")]}
            if state.get("revoke_on_reply"):
                access.allowed = False
        elif path.endswith("/catalog"):
            value = {
                "items": [entry("document", "document", parent="source")],
                "page": 1,
                "page_size": 25,
                "pages": 1,
                "total": 1,
            }
        elif "/catalog/" in path:
            identifier = path.rsplit("/", 1)[1]
            parents = {"project": "workspace", "collection": "project", "source": "collection", "document": "source"}
            value = entry(identifier, "source" if identifier == "source" else identifier, parent=parents[identifier])
            if identifier == "source":
                value["name"] = "reference.md"
                value["metadata"] = {"sha256": hashlib.sha256(b"Verified source reference").hexdigest()}
        elif path.endswith("/documents/document/chunks"):
            value = {
                "document_id": "document",
                "source_id": "source",
                "native_source_id": "native",
                "source_version_id": "version",
                "extraction_id": "extraction",
                "items": [],
                "offset": 0,
                "limit": 1,
                "total": 0,
                "next_offset": None,
            }
        else:
            raise AssertionError("Unexpected native write route " + path)
        return httpx.Response(200, json=value)

    knowledge.transport = httpx.MockTransport(handler)
    return knowledge, store, journal, state, access


async def test_source_wal_precedes_one_native_upload_and_retains_version_identity(writes):
    knowledge, store, journal, state, _ = writes
    response = await knowledge.ingest_source(store.run.id.hex, source_body())
    assert response["status"] == "submitted"
    assert response["receipt"]["source_id"] == "source"
    assert state["source_posts"] == 1
    again = await knowledge.ingest_source(store.run.id.hex, source_body())
    assert again["receipt"] == response["receipt"] and state["source_posts"] == 1
    assert store.run.data["active_task"] == "implement"
    assert any(row.kind == "koinon_source_receipt" for row in journal.records.rows)


async def test_lost_source_reply_never_reposts_and_explicit_readback_records_content_not_creation(writes):
    knowledge, store, _, state, _ = writes
    state["lost"] = True
    with pytest.raises(ValueError, match="uncertain"):
        await knowledge.ingest_source(store.run.id.hex, source_body())
    with pytest.raises(ValueError, match="uncertain"):
        await knowledge.ingest_source(store.run.id.hex, source_body())
    assert state["source_posts"] == 1
    result = await knowledge.observe_ingestion(store.run.id.hex, "source", "e" * 32, "source")
    assert result["receipt"]["source_id"] == "source"
    assert result["receipt"]["creation_causality"] == "not_proved"
    assert result["receipt"]["source_versions"][0]["source_version_id"] == "version"
    assert state["source_posts"] == 1


async def test_source_request_identity_cannot_change_content_after_first_receipt(writes):
    knowledge, store, _, state, _ = writes
    await knowledge.ingest_source(store.run.id.hex, source_body())
    with pytest.raises(ValueError, match="identity"):
        await knowledge.ingest_source(
            store.run.id.hex, source_body(content_base64=base64.b64encode(b"Different source").decode())
        )
    assert state["source_posts"] == 1


def native_review(state, *, destination="collection"):
    from firefly_forge.journal import digest

    body = state["memory_body"]["content"]
    body = {"summary": None, "domain": "other", "tags": [], **body}
    evidence = {
        "kind": "external_proposal",
        "text": body["body"],
        "sha256": hashlib.sha256(body["body"].encode()).hexdigest(),
        "submitted_by": "machine",
        "credential_id": "credential",
        "source_receipt_ids": [],
        "recorded_at": "2026-10-30T00:00:00Z",
        "authority": "unverified",
        "provider": None,
        "model": None,
    }
    from koinon_sdk.curated import ExternalProposalEvidence

    evidence = ExternalProposalEvidence.model_validate(evidence).model_dump(mode="json")
    state["expected_content_digest"] = digest(body)
    result = {
        "id": "review",
        "status": "preview",
        "expires_at": "2030-01-01T00:00:00Z",
        "review": {
            "id": "review",
            "revision": 1,
            "tenant_id": "tenant",
            "owner_subject": "machine",
            "action": "create_published",
            "destination": {
                "id": destination,
                "workspace_id": "workspace",
                "project_id": "project",
                "name": "References",
                "ancestry_digest": "a" * 64,
            },
            "target_item_id": "item",
            "before": None,
            "after": body,
            "evidence": [evidence],
            "authorization_digest": "b" * 64,
            "status": "preview",
            "created_at": "2026-10-30T00:00:00Z",
            "expires_at": "2030-01-01T00:00:00Z",
            "receipt": None,
        },
    }
    if state.get("committed"):
        view = result["review"]
        receipt = {
            "operation_id": "operation",
            "intent_id": "review",
            "intent_revision": 1,
            "authorization_digest": "b" * 64,
            "item_id": "item",
            "native_item_id": "native-item",
            "native_version_id": "native-version",
            "version": 1,
            "serving_version": 1,
            "action": "create_published",
            "content_sha256": "f" * 64 if state.get("bad_receipt") else digest(body),
            "evidence_sha256": digest([evidence]),
            "committed_at": "2026-10-30T00:00:00Z",
            "retrieval_state": "ready",
            "reason": None,
        }
        view.update(status="committed", receipt=receipt)
        result["status"] = "committed"
    result["review"]["owner_subject"] = state.get("owner", "machine")
    return result


@pytest.fixture
async def memory_writes(writes):
    from types import SimpleNamespace

    knowledge, store, journal, state, access = writes

    async def workspace(run_id):
        assert run_id == store.run.id.hex
        return store.path

    knowledge.engine = SimpleNamespace(
        validated_workspace=workspace, enforce_ownership=True, assert_ownership=store.assert_ownership
    )
    store.run.data.update(
        status="succeeded",
        summary="Verified inventory reservations",
        changed_files=["inventory.py"],
        final_verification=[{"argv": ["pytest"], "exit_code": 0}],
    )
    old = knowledge.transport

    async def handle(request):
        if request.url.path.endswith("/external-knowledge-reviews"):
            state["memory_posts"] += 1
            state["memory_body"] = json.loads(request.content)
            assert "origin" not in state["memory_body"]
            assert request.headers["Idempotency-Key"]
            assert any(row.kind == "koinon_memory_intent" for row in journal.records.rows)
            if state.get("memory_lost"):
                raise httpx.ReadTimeout("Controlled lost memory reply", request=request)
            return httpx.Response(200, json=native_review(state))
        if request.url.path.endswith("/knowledge-items/item/versions/1"):
            review = native_review(state)["review"]
            receipt = review["receipt"]
            return httpx.Response(
                200,
                json={
                    "id": "item",
                    "tenant_id": "tenant",
                    "workspace_id": "workspace",
                    "project_id": "project",
                    "destination_id": "collection",
                    "head_version": 1,
                    "serving_version": 1,
                    "selected_version": {
                        "item_id": "item",
                        "version": 1,
                        "version_id": "native-version",
                        "publication": "published",
                        "serving": True,
                        "content": review["after"],
                        "evidence": review["evidence"],
                        "content_sha256": receipt["content_sha256"],
                        "evidence_sha256": receipt["evidence_sha256"],
                        "operation_id": "operation",
                        "author_subject": "machine",
                        "committed_at": "2026-10-30T00:00:00Z",
                    },
                },
            )
        if request.url.path.endswith("/external-knowledge-reviews/review"):
            return httpx.Response(200, json=native_review(state, destination=state.get("destination", "collection")))
        assert not request.url.path.endswith("/approve") and not request.url.path.endswith("/confirm")
        return await old.handle_async_request(request)

    knowledge.transport = httpx.MockTransport(handle)
    return knowledge, store, journal, state, access


async def test_verified_memory_stages_external_proposal_and_never_auto_confirms(memory_writes):
    knowledge, store, _, state, _ = memory_writes
    result = await knowledge.remember(await store.get_run(store.run.id.hex), automatic=True)
    assert result["status"] == "review_required"
    assert result["receipt"]["origin"] == "external_proposal"
    assert result["receipt"]["authority"] == "unverified"
    assert "knowledge_memory" not in store.run.data
    assert store.run.data["knowledge_memory_status"] == "review_required"
    again = await knowledge.remember(await store.get_run(store.run.id.hex), automatic=True)
    assert again["receipt"] == result["receipt"] and state["memory_posts"] == 1


async def test_unknown_memory_stage_never_retries_and_readback_requires_exact_native_binding(memory_writes):
    knowledge, store, journal, state, _ = memory_writes
    state["memory_lost"] = True
    with pytest.raises(ValueError, match="uncertain"):
        await knowledge.remember(await store.get_run(store.run.id.hex), automatic=True)
    with pytest.raises(ValueError, match="uncertain"):
        await knowledge.remember(await store.get_run(store.run.id.hex), automatic=True)
    intent = next(row.data for row in journal.records.rows if row.kind == "koinon_memory_intent")
    state["destination"] = "foreign-collection"
    with pytest.raises(Exception, match="destination"):
        await knowledge.observe_ingestion(store.run.id.hex, "memory", intent["payload"]["request_id"], "review")
    state["destination"] = "collection"
    observed = await knowledge.observe_ingestion(store.run.id.hex, "memory", intent["payload"]["request_id"], "review")
    assert observed["receipt"]["content_sha256"] == state["expected_content_digest"]
    assert observed["receipt"]["commit_receipt"] is None
    assert state["memory_posts"] == 1


async def test_memory_readback_refuses_an_unrelated_evidence_body(memory_writes):
    knowledge, store, _, state, _ = memory_writes
    result = await knowledge.remember(await store.get_run(store.run.id.hex), automatic=True)
    old = knowledge.transport

    async def tamper(request):
        response = await old.handle_async_request(request)
        if request.url.path.endswith("/external-knowledge-reviews/review"):
            value = json.loads(response.content)
            value["review"]["evidence"][0]["sha256"] = "f" * 64
            return httpx.Response(200, json=value)
        return response

    knowledge.transport = httpx.MockTransport(tamper)
    with pytest.raises(Exception, match="origin|evidence"):
        await knowledge.observe_ingestion(store.run.id.hex, "memory", result["request_id"], "review")


async def test_native_project_document_admission_is_durable_before_upload_and_deduplicates_fresh_ids(writes):
    import copy
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    from firefly_forge.knowledge_uploads import KnowledgeUploads

    knowledge, store, _, state, _ = writes

    class Records:
        def __init__(self):
            self.rows = []

        async def find_by_id(self, identifier, **kwargs):
            return next((row for row in self.rows if row.id == identifier), None)

        async def find_digest(self, repository, digest):
            return next((row for row in self.rows if row.repository_id == repository and row.digest == digest), None)

        async def save(self, row):
            if row not in self.rows:
                self.rows.append(row)
            return row

    records = Records()

    async def execute(*args, **kwargs):
        return None

    @asynccontextmanager
    async def unit():
        before = copy.deepcopy(records.rows)
        try:
            yield SimpleNamespace(execute=execute)
        except BaseException:
            records.rows = before
            raise

    store._unit = unit
    old = knowledge.transport

    async def handle(request):
        if request.url.path.endswith("/sources/upload"):
            assert records.rows and records.rows[0].data["status"] == "submitting"
        return await old.handle_async_request(request)

    knowledge.transport = httpx.MockTransport(handle)
    state["project_upload"] = True
    uploads = KnowledgeUploads(store, knowledge, records=records)
    result = await uploads.native_upload("a" * 32, source_body())
    assert result["status"] == "submitted" and result["provider"] == "koinon"
    duplicate = await uploads.native_upload("a" * 32, source_body(request_id="d" * 32))
    assert duplicate["id"] == result["id"] and state["source_posts"] == 1
    observed = await uploads.reconcile("a" * 32, result["id"], SimpleNamespace(source_id="source"))
    assert observed["native_receipt"]["source_versions"][0]["source_version_id"] == "version"
    assert observed["native_receipt"]["creation_causality"] == "not_proved"
    assert state["source_posts"] == 1


async def test_existing_verified_index_entrypoint_uses_native_wal_and_real_confined_file_read(
    writes, tmp_path, monkeypatch
):
    from types import SimpleNamespace

    from firefly_forge import knowledge_ingestion as module

    knowledge, store, _, state, _ = writes
    workspace = tmp_path / "verified-source"
    workspace.mkdir()
    (workspace / "reference.md").write_bytes(b"Verified source reference")

    async def verified(identifier):
        assert identifier == store.run.id.hex
        return workspace

    async def tracked(argv, cwd):
        assert argv == ["git", "ls-tree", "-r", "--name-only", store.run.data["source_tree"]]
        assert cwd == workspace
        return "reference.md"

    monkeypatch.setattr(module, "checked", tracked)
    knowledge.engine = SimpleNamespace(
        validated_workspace=verified, enforce_ownership=True, assert_ownership=store.assert_ownership
    )
    result = await knowledge.index(store.run.id.hex, ["reference.md"])
    assert result["provider"] == "koinon" and result["sources"][0]["source_id"] == "source"
    assert result["sources"][0]["source_versions"][0]["source_version_id"] == "version"
    again = await knowledge.index(store.run.id.hex, ["reference.md"])
    assert again["sources"] == result["sources"] and state["source_posts"] == 1
    with pytest.raises(ValueError, match="tracked"):
        await knowledge.index(store.run.id.hex, ["foreign.md"])
    assert state["source_posts"] == 1


@pytest.mark.parametrize("change", ["owner", "receipt"])
async def test_native_memory_observation_refuses_foreign_owner_or_wrong_committed_content(memory_writes, change):
    knowledge, store, _, state, _ = memory_writes
    staged = await knowledge.remember(await store.get_run(store.run.id.hex), automatic=True)
    if change == "owner":
        state["owner"] = "foreign-machine"
    else:
        state.update(committed=True, bad_receipt=True)
    with pytest.raises(Exception, match="owner|content|receipt"):
        await knowledge.observe_ingestion(store.run.id.hex, "memory", staged["request_id"], "review")
    assert "knowledge_memory" not in store.run.data


async def test_actual_committed_memory_readback_retains_native_version_and_external_origin(memory_writes):
    knowledge, store, _, state, _ = memory_writes
    staged = await knowledge.remember(await store.get_run(store.run.id.hex), automatic=True)
    state["committed"] = True
    observed = await knowledge.observe_ingestion(store.run.id.hex, "memory", staged["request_id"], "review")
    assert observed["receipt"]["commit_receipt"]["native_version_id"] == "native-version"
    assert store.run.data["knowledge_memory"]["native_version_id"] == "native-version"
    assert store.run.data["knowledge_memory"]["origin"] == "external_proposal"
    assert store.run.data["knowledge_memory"]["authority"] == "unverified"
    assert state["memory_posts"] == 1


@pytest.mark.parametrize("grant", ["local", "remote"])
async def test_native_source_requires_both_current_factory_and_workspace_write_grants(writes, grant):
    from firefly_forge.project_access import AccessDenied

    knowledge, store, journal, state, access = writes
    if grant == "local":
        access.allowed = False
    else:
        state["remote_allowed"] = False
    with pytest.raises(AccessDenied):
        await knowledge.ingest_source(store.run.id.hex, source_body())
    assert state["source_posts"] == 0
    assert not any(row.kind == "koinon_source_intent" for row in journal.records.rows)


async def test_late_source_permission_loss_retains_actual_wal_receipt_and_withholds_response(writes):
    from firefly_forge.project_access import AccessDenied

    knowledge, store, journal, state, access = writes
    state["revoke_on_reply"] = True
    with pytest.raises(AccessDenied):
        await knowledge.ingest_source(store.run.id.hex, source_body())
    assert any(row.kind == "koinon_source_receipt" for row in journal.records.rows)
    assert state["source_posts"] == 1
    access.allowed = True
    result = await knowledge.ingest_source(store.run.id.hex, source_body())
    assert result["receipt"]["source_id"] == "source" and state["source_posts"] == 1


async def test_native_source_refuses_a_valid_foreign_run_chain_even_with_matching_request_id(writes):
    from uuid import UUID

    from firefly_forge.journal import Journal, digest

    knowledge, store, journal, state, _ = writes
    await knowledge.ingest_source(store.run.id.hex, source_body())
    previous = "0" * 64
    for row in journal.records.rows:
        row.run_id = UUID(int=999)
        row.data.update(run_id=row.run_id.hex, previous_hash=previous)
        row.previous_hash = previous
        row.digest = digest({key: value for key, value in row.data.items() if key != "digest"})
        row.data["digest"] = row.digest
        previous = row.digest
    assert Journal.verify_chain([row.data for row in journal.records.rows])
    with pytest.raises(ValueError, match="custody"):
        await knowledge.ingest_source(store.run.id.hex, source_body())
    assert state["source_posts"] == 1


async def test_changed_verified_source_stages_a_fresh_memory_proposal_without_relabeling_prior_version(memory_writes):
    knowledge, store, _, state, _ = memory_writes
    staged = await knowledge.remember(await store.get_run(store.run.id.hex), automatic=True)
    state["committed"] = True
    await knowledge.observe_ingestion(store.run.id.hex, "memory", staged["request_id"], "review")
    original = dict(store.run.data["knowledge_memory"])
    store.run.data["source_tree"] = "f" * 40
    state["committed"] = False
    fresh = await knowledge.remember(await store.get_run(store.run.id.hex), automatic=True)
    assert fresh["status"] == "review_required" and fresh["request_id"] != staged["request_id"]
    assert state["memory_posts"] == 2
    assert store.run.data["knowledge_memory"] == original


async def test_recorded_memory_operation_cannot_be_rebound_to_another_remote_review(memory_writes):
    knowledge, store, _, _, _ = memory_writes
    staged = await knowledge.remember(await store.get_run(store.run.id.hex), automatic=True)
    with pytest.raises(ValueError, match="original|saved"):
        await knowledge.observe_ingestion(store.run.id.hex, "memory", staged["request_id"], "other-review")


async def test_legacy_learning_cache_is_explicitly_held_until_versioned_native_receipt_migration(writes):
    from types import SimpleNamespace

    from firefly_forge.learning_projection import LearningProjection

    knowledge, store, journal, state, _ = writes
    projection = LearningProjection(store, journal, SimpleNamespace(), knowledge)

    async def record(*args, **kwargs):
        return SimpleNamespace(id=store.run.id, run_id=store.run.id, state="verified", kind="local_outcome")

    async def row(*args, **kwargs):
        return SimpleNamespace(state="pending")

    projection._record, projection._projection = record, row
    with pytest.raises(ValueError, match="cache|receipt"):
        await projection.submit("a" * 32, request_id="b" * 32)
    assert state["source_posts"] == 0 and state["memory_posts"] == 0
