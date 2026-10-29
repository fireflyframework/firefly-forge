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
            # Each runtime inspection retains a distinct diff artifact for the same source tree.
            assert a["invocation_id"] != b["invocation_id"]
            assert (
                a["results"]
                == b["results"]
                == [
                    {
                        "citation_id": "learning-" + outcome["id"],
                        "statement": outcome["envelope"]["statement"],
                        "source_tree": outcome["envelope"]["context"]["source_tree"],
                        "run_id": origin["id"],
                    }
                ]
            )
            assert {key: value for key, value in a["knowledge_decision"].items() if key != "observed_at"} == {
                key: value for key, value in b["knowledge_decision"].items() if key != "observed_at"
            }
            for result in (a, b):
                decision = result["knowledge_decision"]
                assert result["untrusted"] and result["origin"] == "verified_local_outcome"
                assert decision["eligible"] and decision["reason"] == "verified_exact_match"
                assert decision["candidate_id"] == outcome["id"] and decision["observed_at"]
                assert not decision["web_provider_dispatched"] and not decision["refresh_required"]
            observations.extend((a, b))
            await WorkspaceTools(workspace).write_file("procedure_note.py", "CHECKED=True\n")
            return AgentResult("Used the observed local procedure and independently checked stock", "fixture")

    engine = Engine(store, tmp_path / "reused-workspaces", lambda _: Worker(), journal=journal)
    engine.knowledge_first = first
    engine.knowledge = manager
    engine.tool_broker = broker
    await store.acquire_ownership()
    await engine.execute_run(run["id"])
    final = await store.get_run(run["id"])
    assert final["status"] == "succeeded", final.get("error")
    assert final["final_verification"][0]["exit_code"] == 0
    assert len(observations) == 2 and len(knowledge_calls) == 6
    assert [request.headers["X-Workspace-Id"] for request in knowledge_calls] == [
        "forge-" + origin["repository_id"],
        "frameworks",
    ] * 3
    records = await broker.invocations(run["id"])
    assert len(records) == 2
    assert {record["correlation_id"]: record["result"] for record in records} == {
        result["invocation_id"]: result for result in observations
    }
    for record in records:
        assert record["status"] == "succeeded" and record["admission"]["tool"] == "knowledge_lookup"
        assert record["observation"]["classification"] == "verified_outcome"
        assert not record["observation"]["web_provider_dispatched"]
        assert record["admission"]["source"]["source_tree"] == outcome["envelope"]["context"]["source_tree"]
    sources = [record["admission"]["source"] for record in records]
    assert sources[0]["diff_artifact"]["artifact_id"] != sources[1]["diff_artifact"]["artifact_id"]
    for key in ("base_revision", "head", "source_tree"):
        assert sources[0][key] == sources[1][key]
    for key in ("sha256", "byte_count", "file_count"):
        assert sources[0]["diff_artifact"][key] == sources[1]["diff_artifact"][key]
    entries = await journal.entries(run["id"])
    assert Journal.verify_chain(entries)
    for result in observations:
        retained = [entry for entry in entries if entry["idempotency_key"] == result["invocation_id"]]
        assert [entry["kind"] for entry in retained] == ["tool_intent", "tool_receipt"]
        assert retained[-1]["payload"]["result"] == result
    assert "fixture-required-knowledge-key" not in json.dumps(entries)
