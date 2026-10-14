# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
import importlib

import pytest
from test_tool_broker import seed

from firefly_forge.learning import LearningLedger
from firefly_forge.tool_broker import ToolReconciliationRequired


@pytest.mark.asyncio
@pytest.mark.parametrize("required_failure", [False, True])
async def test_required_knowledge_is_consulted_before_web_and_failure_never_dispatches(
    store, tmp_path, required_failure
):
    _, broker, run, source = await seed(store, tmp_path)
    module = importlib.import_module("firefly_forge.knowledge_first")
    events = []

    class Knowledge:
        async def search(self, repository_id, query, *, run):
            events.append("knowledge")
            if required_failure:
                raise ValueError("Required knowledge retrieval failed in its configured workspace")
            return {"hits": [], "citations": [], "warnings": [], "context": ""}

    class Search:
        async def search(self, query, count, *, snapshot, on_dispatch):
            on_dispatch()
            events.append("provider")
            return {
                "results": [
                    {
                        "citation_id": "web-" + "a" * 24,
                        "url": "https://github.com/fireflyframework",
                        "snippet": "Untrusted source",
                    }
                ]
            }

    ledger = LearningLedger(store, broker.journal)
    service = module.KnowledgeFirst(store, broker.journal, ledger, broker, Search())
    service.knowledge = Knowledge()
    if required_failure:
        with pytest.raises(ToolReconciliationRequired):
            await service.search(run["id"], "implement", "pyfly documentation", 1, source=source, workspace=tmp_path)
        assert events == ["knowledge"]
        packet = (await broker.invocations(run["id"]))[0]
        assert packet["observation"]["web_provider_dispatched"] is False
        assert packet["observation"]["classification"] == "required_service_failure"
    else:
        first = await service.search(
            run["id"], "implement", "pyfly documentation", 1, source=source, workspace=tmp_path
        )
        second = await service.search(
            run["id"], "implement", "pyfly documentation", 1, source=source, workspace=tmp_path
        )
        assert first == second and events == ["knowledge", "provider"]
        assert first["knowledge_decision"]["reason"] == "no_candidate"
        assert first["knowledge_decision"]["web_provider_dispatched"]
        assert first["knowledge_decision"]["observed_at"]
        assert len(await broker.invocations(run["id"])) == 1
    assert broker.journal.verify_chain(await broker.journal.entries(run["id"]))


@pytest.mark.asyncio
async def test_lookup_cannot_borrow_a_prepared_tool_grant_without_captured_knowledge_access(store, tmp_path):
    from firefly_forge.agent_roles import ProjectRoles
    from firefly_forge.models import RunInput
    from firefly_forge.workspace_tools import tool_catalog

    _, broker, earlier, source = await seed(store, tmp_path)
    run = await store.create_run(
        RunInput(
            repository_id=earlier["repository_id"],
            brief="Lookup scoped knowledge",
            agent="agentic",
            roles=ProjectRoles(allowed_tools={"implementer": ["knowledge_lookup"]}),
        ),
        prepared_tools=tool_catalog({"knowledge_lookup": True}),
    )
    await broker.journal.intent(run["id"], "implement")
    await store.update(run["id"], status="generating")
    calls = []

    async def callback():
        calls.append(1)
        return {"results": []}

    with pytest.raises(ValueError, match="captured enabled knowledge"):
        await broker.invoke(
            run["id"],
            "implement",
            "knowledge_lookup",
            "b" * 32,
            {"query": "Owned procedure", "max_results": 1},
            callback,
            source=source,
            configuration={
                "knowledge_snapshot": run["knowledge_snapshot"],
                "learning_snapshot": run["learning_snapshot"],
            },
        )
    assert calls == [] and await broker.invocations(run["id"]) == []
