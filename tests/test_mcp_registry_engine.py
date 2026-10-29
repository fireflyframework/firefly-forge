# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
import json
import sys

import httpx2
import pytest
from test_mcp_registry import KEY, protocol
from test_mcp_registry_native import project_definition
from test_role_execution import fixture_run

import firefly_forge.engine as engine_module
from firefly_forge.agent_roles import ProjectRoles
from firefly_forge.engine import Engine
from firefly_forge.journal import Journal
from firefly_forge.mcp_registry import ExternalMCPRegistry, MCPConnectionInput, MCPGrantInput, MCPReviewInput
from firefly_forge.models import Command, RepositoryInput, RunInput
from firefly_forge.process import checked
from firefly_forge.projects import Projects
from firefly_forge.providers import AgentResult, ConfiguredProvider
from firefly_forge.tool_broker import ToolInvocationBroker, ToolReconciliationRequired
from firefly_forge.work_duration_models import WorkDurationPolicyV2
from firefly_forge.workspace_tools import WorkspaceTools, tool_catalog


@pytest.mark.asyncio
@pytest.mark.parametrize("unknown", [False, True])
async def test_actual_engine_routes_reviewed_mcp_and_unknown_response_prevents_consolidation(
    store, tmp_path, monkeypatch, unknown
):
    initial = await fixture_run(store, tmp_path)
    local = await store.repository(initial["repository_id"])
    repo = await store.add_repository(
        RepositoryInput(name="Owned remote binding", url="https://github.com/team/product.git")
    )
    project = await Projects(store).configure(project_definition(repo["id"]))
    original_clone = engine_module.clone

    async def owned_clone(repository, workspace, branch, **kwargs):
        assert repository["id"] == repo["id"]
        result = await original_clone(local, workspace, branch, **kwargs)
        await checked(["git", "remote", "set-url", "origin", repo["url"]], workspace)
        return result

    monkeypatch.setattr(engine_module, "clone", owned_clone)
    registry = ExternalMCPRegistry(store.path.parent)
    original_call = registry.call
    selected_work_policies = []

    async def scoped_call(*args, **kwargs):
        selected_work_policies.append(kwargs.get("work_duration"))
        return await original_call(*args, **kwargs)

    monkeypatch.setattr(registry, "call", scoped_call)
    registry.transport, calls = protocol()
    registry.configure("github", MCPConnectionInput(enabled=True, api_key=KEY))
    discovery = await registry.discover("github")
    registry.review("github", MCPReviewInput(digest=discovery["digest"], tools=["get_file_contents"]))
    registry.grant(
        project["id"],
        "github",
        MCPGrantInput(repository_id=repo["id"], review_digest=discovery["digest"], tools=["get_file_contents"]),
        repository=repo,
    )
    base_transport = registry.transport

    async def lost_response(request):
        if request.method == "POST" and json.loads(request.content).get("method") == "tools/call":
            calls.append(json.loads(request.content))
            raise httpx2.ReadTimeout("Lost owned provider receipt", request=request)
        return await base_transport.handle_async_request(request)

    if unknown:
        registry.transport = httpx2.MockTransport(lost_response)
    run = await store.create_run(
        RunInput(
            repository_id=repo["id"],
            project_id=project["id"],
            component_id="stock",
            brief="Set stock to five with the reviewed repository reference",
            agent="bridge",
            skills=[],
            roles=ProjectRoles(allowed_tools={"implementer": ["external_mcp", "write_file"]}),
            commands=[Command(argv=[sys.executable, "-c", "from product import stock; assert stock == 5"])],
        ),
        prepared_tools=tool_catalog({"external_mcp": True}),
    )
    broker = ToolInvocationBroker(store, Journal(store), store.path.parent)
    broker.external_mcp = registry
    broker.credential_registry = registry.credentials

    class Worker(ConfiguredProvider):
        async def execute(self, workspace, *args):
            assert self.runtime_context.external_mcp is not None
            try:
                result = await self.runtime_context.external_mcp(
                    "github", "get_file_contents", {"owner": "team", "repo": "product", "path": "src/stock.py"}
                )
                assert result["untrusted"]
            except ToolReconciliationRequired:
                assert unknown
            await WorkspaceTools(workspace).write_file("product.py", "stock = 5\n")
            return AgentResult("Worker source is updated", "fixture")

    engine = Engine(store, tmp_path / "workspaces", lambda _: Worker(), journal=broker.journal)
    engine.external_mcp = registry
    engine.tool_broker = broker
    engine.credential_registry = registry.credentials
    await engine.execute_run(run["id"])
    final = await store.get_run(run["id"])
    assert selected_work_policies == [WorkDurationPolicyV2()]
    assert final["status"] == ("needs_reconciliation" if unknown else "succeeded"), final.get("error")
    records = await broker.invocations(run["id"])
    assert len(records) == 1 and records[0]["status"] == ("unknown" if unknown else "succeeded")
    assert records[0]["admission"]["source"]["base_revision"] == final["base_revision"]
    assert records[0]["admission"]["configuration"]["digest"] == run["mcp_snapshot"]["digest"]
    events = await broker.journal.entries(run["id"])
    assert Journal.verify_chain(events) and KEY not in json.dumps(events)
    assert len([item for item in calls if item["method"] == "tools/call"]) == 1
    if unknown:
        assert not final.get("artifacts") and not final.get("source_tree")
    else:
        assert final["final_verification"][0]["exit_code"] == 0
