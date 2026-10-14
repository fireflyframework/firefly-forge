# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
import json
import sys

import httpx
import pytest
from test_learning_ledger import completed

from firefly_forge.agent_roles import ProjectRoles
from firefly_forge.engine import Engine
from firefly_forge.journal import Journal
from firefly_forge.knowledge import KnowledgeConfiguration, KnowledgeManager
from firefly_forge.knowledge_first import KnowledgeFirst
from firefly_forge.learning import LearningLedger
from firefly_forge.models import Command, RunInput
from firefly_forge.process import checked
from firefly_forge.providers import AgentResult, ConfiguredProvider
from firefly_forge.search_tools import SearchTools
from firefly_forge.tool_broker import ToolInvocationBroker
from firefly_forge.workspace_tools import WorkspaceTools, tool_catalog


@pytest.mark.asyncio
async def test_actual_engine_reuses_exact_receipted_local_outcome_without_web_key(store, tmp_path):
    original_engine, origin = await completed(store, tmp_path)
    ledger = LearningLedger(store, original_engine.journal)
    ledger.engine = original_engine
    outcome = await ledger.capture_outcome(origin["id"])
    source = tmp_path / "source"
    await checked(
        ["git", "fetch", str(original_engine.workspaces / origin["id"]), origin["checkpoint_revision"]], source
    )
    await checked(["git", "merge", "--ff-only", "FETCH_HEAD"], source)
    knowledge_calls = []

    def knowledge_response(request):
        knowledge_calls.append(request)
        assert request.headers["X-API-Key"] == "fixture-required-knowledge-key"
        assert request.headers["X-Workspace-Id"] in {"frameworks", "forge-" + origin["repository_id"]}
        assert request.url.path == "/api/v1/search"
        return httpx.Response(200, json={"elapsed_ms": 1, "hits": []})

    manager = KnowledgeManager(store, transport=httpx.MockTransport(knowledge_response))
    manager.configure(
        KnowledgeConfiguration(
            values={
                "enabled": True,
                "required": True,
                "auto_memory": False,
                "endpoint": "http://127.0.0.1:18500",
                "tenant_id": "owned",
                "framework_workspace": "frameworks",
                "api_key": "fixture-required-knowledge-key",
            }
        )
    )
    run = await store.create_run(
        RunInput(
            repository_id=origin["repository_id"],
            brief=origin["brief"],
            agent="bridge",
            frontend="none",
            skills=[],
            roles=ProjectRoles(allowed_tools={"implementer": ["knowledge_lookup", "write_file"]}),
            commands=[Command(argv=[sys.executable, "-c", "from product import stock; assert stock == 5"])],
        ),
        prepared_tools=tool_catalog({"knowledge_lookup": True}),
    )
    journal = Journal(store)
    broker = ToolInvocationBroker(store, journal, store.path.parent)
    web = SearchTools(tmp_path / "unconfigured-search")
    first = KnowledgeFirst(store, journal, ledger, broker, web)
    first.knowledge = manager
    observations = []

    class Worker(ConfiguredProvider):
        async def execute(self, workspace, *args):
            context = self.runtime_context
            assert context.internet_search is None and context.knowledge_lookup is not None
            a = await context.knowledge_lookup(origin["brief"], False, None)
            b = await context.knowledge_lookup(origin["brief"], False, None)
            assert a == b
            assert a["origin"] == "verified_local_outcome" and a["knowledge_decision"]["candidate_id"] == outcome["id"]
            assert not a["knowledge_decision"]["web_provider_dispatched"]
            observations.append(a)
            await WorkspaceTools(workspace).write_file("procedure_note.py", "CHECKED=True\n")
            return AgentResult("Used the observed local procedure and independently checked stock", "fixture")

    engine = Engine(store, tmp_path / "reused-workspaces", lambda _: Worker(), journal=journal)
    engine.knowledge_first = first
    engine.knowledge = manager
    engine.tool_broker = broker
    await engine.execute_run(run["id"])
    final = await store.get_run(run["id"])
    assert final["status"] == "succeeded", final.get("error")
    assert final["final_verification"][0]["exit_code"] == 0
    assert len(observations) == 1 and len(knowledge_calls) == 4
    records = await broker.invocations(run["id"])
    assert len(records) == 1 and records[0]["admission"]["tool"] == "knowledge_lookup"
    assert records[0]["observation"]["classification"] == "verified_outcome"
    assert not records[0]["observation"]["web_provider_dispatched"]
    assert "fixture-required-knowledge-key" not in json.dumps(await journal.entries(run["id"]))
