# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
import sys

import httpx
import pytest
from test_role_execution import fixture_run

from firefly_forge.agent_roles import ProjectRoles
from firefly_forge.engine import Engine
from firefly_forge.journal import Journal
from firefly_forge.models import Command, RunInput
from firefly_forge.providers import AgentResult, ConfiguredProvider
from firefly_forge.sandbox_models import SandboxInput, capture_sandbox
from firefly_forge.search_tools import SearchTools
from firefly_forge.tool_broker import ToolInvocationBroker, ToolReconciliationRequired
from firefly_forge.workspace_tools import WorkspaceTools, tool_catalog


@pytest.mark.asyncio
@pytest.mark.parametrize("unknown", [False, True])
async def test_engine_search_has_actual_source_wal_and_unknown_outcome_cannot_deliver(store, tmp_path, unknown):
    initial = await fixture_run(store, tmp_path)
    calls = []

    def transport(request):
        calls.append(request)
        if unknown:
            raise httpx.ReadTimeout("Provider response was lost", request=request)
        return httpx.Response(
            200, json={"results": [{"url": "https://github.com/fireflyframework", "content": "Framework source"}]}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        search = SearchTools(tmp_path / "search", client=client)
        search.configure({"provider": "tavily", "api_key": "fixture-tavily-secret"})
        journal = Journal(store)
        broker = ToolInvocationBroker(store, journal, store.path.parent)
        run = await store.create_run(
            RunInput(
                repository_id=initial["repository_id"],
                brief="Set stock to five",
                agent="bridge",
                frontend="none",
                skills=[],
                roles=ProjectRoles(allowed_tools={"implementer": ["internet_search", "write_file"]}),
                commands=[Command(argv=[sys.executable, "-c", "from product import stock; assert stock == 5"])],
            ),
            prepared_runner={
                "sandbox_snapshot": capture_sandbox(SandboxInput(internet_search=True)),
                "search_snapshot": search.snapshot(),
                "tool_policy_snapshot": broker.snapshot(),
                "runner_config_revision": None,
            },
            prepared_tools=tool_catalog({"internet_search": True}),
        )

        class Worker(ConfiguredProvider):
            async def execute(self, workspace, *args):
                try:
                    await self.runtime_context.internet_search("pyfly source", 1)
                except ToolReconciliationRequired:
                    assert unknown
                await WorkspaceTools(workspace).write_file("product.py", "stock = 5\n")
                return AgentResult("Worker completed its code change", "fixture")

        engine = Engine(store, tmp_path / "workspaces", lambda _: Worker(), journal=journal)
        engine.search, engine.tool_broker = search, broker
        await engine.execute_run(run["id"])
        final = await store.get_run(run["id"])
        assert final["status"] == ("needs_reconciliation" if unknown else "succeeded"), final.get("error")
        records = await broker.invocations(run["id"])
        assert len(records) == 1 and len(calls) == 1
        admission = records[0]["admission"]
        assert admission["source"]["base_revision"] == final["base_revision"]
        assert len(admission["source"]["head"]) == 40 and len(admission["source"]["source_tree"]) == 40
        assert admission["repository_id"] == initial["repository_id"]
        assert admission["configuration"]["revision"] == run["search_snapshot"]["revision"]
        entries = await journal.entries(run["id"])
        assert journal.verify_chain(entries)
        assert "fixture-tavily-secret" not in str(entries)
        if unknown:
            assert not final.get("artifacts") and not final.get("source_tree")
        else:
            assert final["final_verification"][0]["exit_code"] == 0
