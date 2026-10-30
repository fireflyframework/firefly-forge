# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Keep long owner contracts intact while respecting flycanon's retrieval limit."""

import copy
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest
from test_knowledge import manager, memory_transport, repository, retained_configuration, settings

from firefly_forge.configuration import ConfigurationStore
from firefly_forge.knowledge import KnowledgeManager, snapshot_knowledge


@pytest.mark.parametrize("agent", [False, True])
@pytest.mark.parametrize("length", [1999, 2000, 2001, 20000])
async def test_long_run_retrieval_bounds_each_remote_query_without_changing_the_brief(tmp_path, agent, length):
    seen = []
    brief = "λ" * length

    def handler(request):
        payload = json.loads(request.content)
        seen.append((request.headers["X-Workspace-Id"], payload["query"]))
        assert request.url.path == ("/api/v1/agent/search" if agent else "/api/v1/search")
        if len(payload["query"]) > 2000:
            return httpx.Response(422, json={"detail": "query exceeds the 2000-character server contract"})
        return httpx.Response(200, json={"elapsed_ms": 1, "hits": []})

    knowledge = manager(tmp_path, handler)
    knowledge.configure(
        settings(
            required=True,
            framework_workspace="frameworks",
            **({"api_key": "", "agent_token": "fixture-agent-token"} if agent else {}),
        )
    )
    run = {
        "id": "run1",
        "repository_id": "a" * 32,
        "brief": brief,
        "knowledge_snapshot": snapshot_knowledge(knowledge.private),
    }
    before = copy.deepcopy(run)
    result = await knowledge.retrieve(run)
    assert result["warnings"] == []
    assert seen == [("forge-" + "a" * 32, brief[:2000]), ("frameworks", brief[:2000])]
    assert run == before


async def test_verified_memory_bounds_its_search_without_truncating_published_memory(tmp_path):
    saved, original_handler = memory_transport()
    queries = []

    def handler(request):
        if request.url.path == "/api/v1/search":
            query = json.loads(request.content)["query"]
            queries.append(query)
            if len(query) > 2000:
                return httpx.Response(422, json={"detail": "query exceeds the 2000-character server contract"})
        return original_handler(request)

    run = {
        "id": "run1",
        "repository_id": "a" * 32,
        "status": "succeeded",
        "source_tree": "verified-tree",
        "summary": "Verified customer relationship workflow. " * 150,
        "changed_files": ["backend/crm.py"],
        "final_verification": [{"argv": ["pytest"], "exit_code": 0}],
    }

    async def get_run(identifier):
        assert identifier == run["id"]
        return copy.deepcopy(run)

    async def update(identifier, **changes):
        assert identifier == run["id"]
        run.update(copy.deepcopy(changes))
        return copy.deepcopy(run)

    @asynccontextmanager
    async def unit():
        yield

    async def validated_workspace(identifier):
        assert identifier == run["id"]
        return tmp_path

    store = SimpleNamespace(
        configuration=ConfigurationStore(tmp_path), repository=repository, get_run=get_run, update=update, _unit=unit
    )
    retained_configuration(store.configuration)
    knowledge = KnowledgeManager(
        store, SimpleNamespace(validated_workspace=validated_workspace), transport=httpx.MockTransport(handler)
    )
    knowledge.configure(settings())
    result = await knowledge.remember(copy.deepcopy(run))
    assert result["source_id"] == "memory-source"
    assert len(saved["body"]) > 2000 and queries == [saved["body"][:2000]]
    assert json.loads(saved["body"])["summary"] == run["summary"][:6000]
    assert saved["source_writes"] == saved["knowledge_writes"] == 1
    assert run["knowledge_memory_status"] == "saved"
