# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Native Koinon retrieval uses exact mapped scope and controlled HTTP only."""

import json
from types import SimpleNamespace
from uuid import UUID

import httpx
import pytest

from firefly_forge.configuration import ConfigurationStore
from firefly_forge.identity import Principal
from firefly_forge.knowledge import KnowledgeConfiguration, KnowledgeManager, snapshot_knowledge
from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied

REPOSITORY = "a" * 32
ACTOR = Principal(UUID(int=1), "fixture", "reader")


class Access:
    def __init__(self):
        self.allowed = True
        self.calls = []

    async def require_repository(self, actor, repository, permission):
        self.calls.append((actor, repository, permission))
        if not self.allowed:
            raise AccessDenied("Current fixture grant was removed")


def settings(**changes):
    return KnowledgeConfiguration(
        values={
            "provider": "koinon",
            "version": 2,
            "enabled": True,
            "required": True,
            "endpoint": "https://koinon.example/api/v1",
            "tenant_id": "tenant",
            "access_token": "private-machine-token",
            "top_k": 25,
            "project_scopes": {
                REPOSITORY: {"workspace_id": "workspace", "project_id": "project", "collection_id": "collection"}
            },
            **changes,
        }
    )


def entry(identifier, kind, *, project="project", workspace="workspace", parent=None):
    return {
        "id": identifier,
        "tenant_id": "tenant",
        "kind": kind,
        "name": identifier,
        "parent_id": parent,
        "workspace_id": workspace,
        "project_id": project,
        "status": "ready",
        "created_at": "2026-10-30T00:00:00Z",
        "metadata": {},
    }


def context(*, allowed=True):
    return {
        "subject": "machine",
        "tenants": [{"id": "tenant", "name": "Tenant"}],
        "workspaces": [entry("workspace", "workspace", project=None)],
        "workspace_permissions": {
            "workspace": ["catalog:read", "knowledge:search", "query:execute"] if allowed else []
        },
    }


@pytest.fixture
def manager(tmp_path):
    access = Access()
    state = {"requests": [], "context_allowed": True, "hit_project": "project", "status": 200}

    async def repository(identifier):
        assert identifier == REPOSITORY
        return {"id": identifier}

    def handler(request):
        state["requests"].append(request)
        assert request.headers["Authorization"] == "Bearer private-machine-token"
        assert "Cookie" not in request.headers
        path = request.url.path
        if path == "/api/v1/context":
            value = context(allowed=state["context_allowed"])
        elif path == "/api/v1/capabilities":
            value = {"catalog": {"ready": True}, "providers": [], "features": []}
        elif path.endswith("/catalog/dataset"):
            value = entry("dataset", "dataset", parent="project")
        elif path.endswith("/datasets/dataset/schema"):
            value = {
                "tables": [
                    {
                        "id": "table",
                        "name": "Inventory",
                        "snapshot_id": "snapshot",
                        "columns": [{"id": str(i), "name": "column" + str(i), "data_type": "text"} for i in range(300)],
                    }
                ]
            }
        elif path.endswith("/documents/document/chunks"):
            offset = int(request.url.params["offset"])
            value = {
                "document_id": "document",
                "source_id": state.get("chunk_source", "source"),
                "native_source_id": "native-source",
                "source_version_id": state.get("source_version", "version"),
                "extraction_id": "extraction",
                "items": [
                    {
                        "id": "chunk",
                        "index": offset,
                        "total_chunks": 5,
                        "text": "Full retained chunk",
                        "char_start": 0,
                        "char_end": 19,
                    }
                ],
                "offset": offset,
                "limit": 3,
                "total": 5,
                "next_offset": 3 if offset == 0 else None,
            }
        elif path.endswith("/catalog/project"):
            value = entry("project", "project", parent="workspace")
        elif path.endswith("/catalog/collection"):
            value = entry("collection", "collection", parent="project")
        elif path.endswith("/catalog/document"):
            value = entry("document", "document", project=state["hit_project"], parent="source")
        elif path.endswith("/catalog/source"):
            value = entry("source", "source", parent="collection")
        elif path.endswith("/search"):
            state["question"] = json.loads(request.content)
            value = {
                "hits": [
                    {
                        "id": "chunk",
                        "title": "Retained source",
                        "text": "λ" * 3000,
                        "asset_id": "document",
                        "source_id": state.get("hit_source", "source"),
                        "citations": state.get("citations", []),
                    }
                ]
            }
            if state["status"] != 200:
                return httpx.Response(state["status"], json={"detail": "private-machine-token"})
        else:
            raise AssertionError("Unadmitted route " + path)
        return httpx.Response(200, json=value, headers={"Set-Cookie": "unwanted=session"})

    store = SimpleNamespace(configuration=ConfigurationStore(tmp_path), repository=repository)
    instance = KnowledgeManager(store, transport=httpx.MockTransport(handler), access=access)
    token = CURRENT_PRINCIPAL.set(ACTOR)
    try:
        yield instance, state, access
    finally:
        CURRENT_PRINCIPAL.reset(token)


def test_new_knowledge_configuration_defaults_to_koinon(tmp_path):
    store = SimpleNamespace(configuration=ConfigurationStore(tmp_path))
    assert KnowledgeManager(store).public()["provider"] == "koinon"


async def test_manager_native_search_preserves_full_query_text_and_scoped_citations(manager):
    knowledge, state, access = manager
    public = knowledge.configure(settings())
    assert public["access_token_configured"] is True and "private-machine-token" not in json.dumps(public)
    brief = "Ω" * 7000
    result = await knowledge.search(REPOSITORY, brief)
    assert state["question"] == {
        "question": brief,
        "workspace_id": "workspace",
        "project_id": "project",
        "collection_id": "collection",
        "limit": 25,
        "answer": False,
    }
    assert result["hits"][0]["content"] == "λ" * 3000
    assert result["citations"][0]["asset_id"] == "document"
    assert result["citations"][0]["source_version_id"] is None
    assert result["citations"][0]["provenance"] == "not_recorded"
    assert len(access.calls) >= 2 and {item[2] for item in access.calls} == {"read"}
    assert all(request.url.host == "koinon.example" for request in state["requests"])


@pytest.mark.parametrize("change", ["scope", "current_grant", "remote_grant"])
async def test_foreign_or_revoked_scope_never_exposes_retrieved_content(manager, change):
    knowledge, state, access = manager
    knowledge.configure(settings())
    if change == "scope":
        state["hit_project"] = "foreign"
    elif change == "current_grant":
        access.allowed = False
    else:
        state["context_allowed"] = False
    with pytest.raises((ValueError, AccessDenied)):
        await knowledge.search(REPOSITORY, "Scoped retrieval")
    if change != "scope":
        assert not any(request.url.path.endswith("/search") for request in state["requests"])


async def test_upstream_failure_is_safe_and_never_retried(manager):
    knowledge, state, _ = manager
    knowledge.configure(settings())
    state["status"] = 503
    with pytest.raises(ValueError) as error:
        await knowledge.search(REPOSITORY, "Scoped retrieval")
    assert "private-machine-token" not in str(error.value)
    assert len([r for r in state["requests"] if r.url.path.endswith("/search")]) == 1


def test_existing_legacy_revision_is_read_without_rewriting_it(tmp_path):
    private = ConfigurationStore(tmp_path)
    saved = {
        "enabled": True,
        "required": True,
        "endpoint": "https://retained.example",
        "tenant_id": "retained",
        "api_key": "retained-key",
    }
    private._write(private.directory / "knowledge.json", {"revision": "c" * 32, "settings": saved})
    original = (private.directory / "knowledge.json").read_bytes()
    knowledge = KnowledgeManager(SimpleNamespace(configuration=private))
    frozen = snapshot_knowledge(private)
    assert knowledge.settings({"knowledge_snapshot": frozen})["api_key"] == "retained-key"
    assert "provider" not in knowledge.settings({"knowledge_snapshot": frozen})
    assert (private.directory / "knowledge.json").read_bytes() == original


async def test_readiness_checks_mapped_catalog_without_claiming_retrieval(manager):
    from firefly_forge.knowledge import KnowledgeTest

    knowledge, state, _ = manager
    knowledge.configure(settings())
    value = await knowledge.test(KnowledgeTest(repository_id=REPOSITORY))
    assert value["readiness"]["state"] == "connected"
    assert "retrieval" in value["readiness"]["detail"].lower()
    assert not any(request.url.path.endswith("/search") for request in state["requests"])


async def test_native_schema_requires_mapped_dataset_and_preserves_snapshot(manager):
    knowledge, state, _ = manager
    scope = settings().values["project_scopes"]
    scope[REPOSITORY]["dataset_ids"] = ["dataset"]
    knowledge.configure(settings(project_scopes=scope))
    response = await knowledge.schema(REPOSITORY, "dataset")
    assert response["schema"]["tables"][0]["snapshot_id"] == "snapshot"
    assert len(response["schema"]["tables"][0]["columns"]) == 300
    before = len(state["requests"])
    with pytest.raises(ValueError, match="mapped dataset"):
        await knowledge.schema(REPOSITORY, "foreign")
    assert len(state["requests"]) == before


async def test_chunk_pagination_retains_version_identity_and_never_mixes_versions(manager):
    knowledge, state, _ = manager
    knowledge.configure(settings())
    first = await knowledge.chunks(REPOSITORY, "document", offset=0, limit=3)
    assert first["page"]["next_offset"] == 3
    assert first["page"]["source_version_id"] == "version"
    assert first["page"]["items"][0]["text"] == "Full retained chunk"
    state["source_version"] = "changed"
    with pytest.raises(ValueError, match="version"):
        await knowledge.chunks(
            REPOSITORY, "document", offset=3, limit=3, expected_identity=first["source_identity_sha256"]
        )


async def test_remote_io_refuses_an_ambient_native_unit_before_transport(manager):
    from pyfly.data.transaction.context import TransactionState, bind_state, reset_state
    from pyfly.data.transaction.unit_of_work import UnitOfWork

    knowledge, state, _ = manager
    knowledge.configure(settings())
    unit = UnitOfWork(SimpleNamespace(), "fixture", object())
    token = bind_state(TransactionState(units=(("fixture", unit),)))
    try:
        with pytest.raises(ValueError, match="transaction"):
            await knowledge.search(REPOSITORY, "Scoped retrieval")
    finally:
        reset_state(token)
    assert state["requests"] == []


def test_new_enabled_connection_requires_an_explicit_project_mapping(manager):
    knowledge, _, _ = manager
    with pytest.raises(ValueError, match="mapping"):
        knowledge.configure(settings(project_scopes={}))


def test_legacy_cutover_requires_migration_and_preserves_the_original_file(tmp_path):
    private = ConfigurationStore(tmp_path)
    private._write(
        private.directory / "knowledge.json",
        {"revision": "d" * 32, "settings": {"enabled": False, "endpoint": "https://retained.example"}},
    )
    before = (private.directory / "knowledge.json").read_bytes()
    knowledge = KnowledgeManager(SimpleNamespace(configuration=private))
    with pytest.raises(ValueError, match="migration"):
        knowledge.configure(settings())
    assert (private.directory / "knowledge.json").read_bytes() == before


def test_default_cli_does_not_bootstrap_a_new_legacy_service(tmp_path):
    from click.testing import CliRunner

    from firefly_forge.cli import main

    service = tmp_path / "service"
    result = CliRunner().invoke(
        main, ["knowledge", "bootstrap", "--data-dir", str(tmp_path / "factory"), "--service-dir", str(service)]
    )
    assert result.exit_code != 0 and "Koinon" in result.output
    assert not service.exists()


@pytest.mark.parametrize("field", ["source", "citation"])
async def test_foreign_citation_identity_is_not_exposed_as_current_provenance(manager, field):
    knowledge, state, _ = manager
    knowledge.configure(settings())
    if field == "source":
        state["hit_source"] = "foreign-source"
    else:
        state["citations"] = [{"document_id": "foreign-document", "chunk_id": "chunk"}]
    with pytest.raises(AccessDenied, match="citation|source"):
        await knowledge.search(REPOSITORY, "Cited reference")


async def test_native_citation_page_and_section_are_preserved_without_invented_version(manager):
    knowledge, state, _ = manager
    knowledge.configure(settings())
    state["citations"] = [{"document_id": "document", "source_id": "source", "page": 4, "section": "Inventory"}]
    result = await knowledge.search(REPOSITORY, "Cited reference")
    citation = result["hits"][0]["native_citations"][0]
    assert citation["page"] == 4 and citation["section"] == "Inventory"
    assert result["hits"][0]["availability"] == "unverified"
    assert result["hits"][0]["source_version_id"] is None


async def test_chunk_page_refuses_a_foreign_source_even_when_document_id_matches(manager):
    knowledge, state, _ = manager
    knowledge.configure(settings())
    state["chunk_source"] = "foreign-source"
    with pytest.raises(AccessDenied, match="source"):
        await knowledge.chunks(REPOSITORY, "document", offset=0, limit=3)
