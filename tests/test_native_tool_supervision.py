# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Campaign capability and native tool failures retain their durable supervisor state."""

import uuid
from types import SimpleNamespace

import pytest


@pytest.mark.asyncio
@pytest.mark.parametrize("grant_scope", ["active", "revoked", "other_project"])
async def test_campaign_preserves_granted_external_capability(store, tmp_path, grant_scope):
    from test_mcp_registry import KEY, protocol
    from test_mcp_registry_native import project_definition

    from firefly_forge.agent_roles import ProjectRoles, stage_allowed_tools
    from firefly_forge.engine import Engine
    from firefly_forge.journal import Journal
    from firefly_forge.mcp_registry import ExternalMCPRegistry, MCPConnectionInput, MCPGrantInput, MCPReviewInput
    from firefly_forge.models import RepositoryInput, RunInput
    from firefly_forge.project_campaigns import CampaignLaunch, ProjectCampaigns
    from firefly_forge.projects import Projects
    from firefly_forge.runner_configuration import RunnerConfiguration
    from firefly_forge.search_tools import SearchTools

    registry = ExternalMCPRegistry(store.path.parent)
    registry.transport, _ = protocol()
    registry.configure("github", MCPConnectionInput(enabled=True, api_key=KEY))
    discovery = await registry.discover("github")
    registry.review("github", MCPReviewInput(digest=discovery["digest"], tools=["get_file_contents"]))
    repo = await store.add_repository(
        RepositoryInput(name="Owned review source", url="https://github.com/team/product.git")
    )
    definition = project_definition(repo["id"])
    definition.roles = ProjectRoles(allowed_tools={"implementer": ["external_mcp", "write_file"]})
    project = await Projects(store).configure(definition)
    grant = registry.grant(
        project["id"] if grant_scope != "other_project" else "a" * 32,
        "github",
        MCPGrantInput(repository_id=repo["id"], review_digest=discovery["digest"], tools=["get_file_contents"]),
        repository=repo,
    )
    if grant_scope == "revoked":
        registry.revoke(project["id"], grant["id"])
    store.runtime_roles = SimpleNamespace(role="api", executes_jobs=False)
    store.runners = RunnerConfiguration(store, SimpleNamespace(), SearchTools(tmp_path / "search"), SimpleNamespace())
    direct = await store.create_run(
        RunInput(
            repository_id=repo["id"],
            project_id=project["id"],
            component_id="stock",
            brief="Read owned source",
            agent="agentic",
        )
    )
    expected = grant_scope == "active"
    assert ("external_mcp" in stage_allowed_tools(direct["role_snapshot"], "implement", "agentic")) == expected

    async def source_base(repository):
        return "a" * 40

    manager = ProjectCampaigns(
        store, Engine(store, tmp_path / "workspaces"), Journal(store), source_resolver=source_base
    )
    campaign = await manager.launch(
        project["id"], CampaignLaunch(request_id=uuid.uuid4().hex, brief="Read owned source", agent="agentic")
    )
    child = await store.get_run(campaign["children"][0]["run_id"])
    assert child["mcp_snapshot"]["grants"] == direct["mcp_snapshot"]["grants"]
    assert ("external_mcp" in stage_allowed_tools(child["role_snapshot"], "implement", "agentic")) == expected

    if expected:

        async def revoke_during_preflight(repository):
            registry.revoke(project["id"], grant["id"])
            return "a" * 40

        manager.source_resolver = revoke_during_preflight
        with pytest.raises(ValueError, match="settings changed during source and runner preflight"):
            await manager.launch(
                project["id"], CampaignLaunch(request_id=uuid.uuid4().hex, brief="Read owned source", agent="agentic")
            )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind,expected",
    [("unknown", "needs_reconciliation"), ("quota", "paused"), ("rate_limit", "paused"), ("authentication", "paused")],
)
async def test_native_tool_failure_retains_supervisor_disposition(store, tmp_path, kind, expected):
    import httpx
    from pydantic_ai.messages import ModelResponse, ToolCallPart
    from pydantic_ai.models.function import FunctionModel
    from test_role_execution import fixture_run

    from firefly_forge.agent_roles import ProjectRoles
    from firefly_forge.agentic_runtime import execute_agentic
    from firefly_forge.engine import Engine
    from firefly_forge.journal import Journal
    from firefly_forge.models import RunInput
    from firefly_forge.providers import AgentResult, ConfiguredProvider
    from firefly_forge.sandbox_models import SandboxInput, capture_sandbox
    from firefly_forge.search_tools import SearchTools
    from firefly_forge.tool_broker import ToolInvocationBroker
    from firefly_forge.workspace_tools import tool_catalog

    initial = await fixture_run(store, tmp_path)
    calls = []

    def remote(request):
        calls.append(request)
        if kind == "unknown":
            raise httpx.ReadTimeout("Owned fixture lost response", request=request)
        status = {"quota": 432, "rate_limit": 429, "authentication": 401}[kind]
        return httpx.Response(status, json={"fixture": "known operational rejection"})

    def model(messages, info):
        return ModelResponse(parts=[ToolCallPart("internet_search", {"query": "pyfly source", "max_results": 1})])

    async with httpx.AsyncClient(transport=httpx.MockTransport(remote)) as client:
        search = SearchTools(tmp_path / "search", client=client)
        search.configure({"provider": "tavily", "api_key": "fixture-owned-tavily-key"})
        journal = Journal(store)
        broker = ToolInvocationBroker(store, journal, store.path.parent)
        run = await store.create_run(
            RunInput(
                repository_id=initial["repository_id"],
                brief="Inspect source with approved search",
                agent="agentic",
                frontend="none",
                skills=[],
                roles=ProjectRoles(allowed_tools={"implementer": ["internet_search", "write_file"]}),
            ),
            prepared_runner={
                "sandbox_snapshot": capture_sandbox(SandboxInput(internet_search=True)),
                "search_snapshot": search.snapshot(),
                "tool_policy_snapshot": broker.snapshot(),
                "runner_config_revision": None,
            },
            prepared_tools=tool_catalog({"internet_search": True}),
        )

        class NativeWorker(ConfiguredProvider):
            async def execute(self, workspace, brief, backend, frontend, emit):
                summary, usage = await execute_agentic(
                    workspace,
                    brief,
                    backend,
                    frontend,
                    emit,
                    model=FunctionModel(model),
                    runtime_context=self.runtime_context,
                )
                return AgentResult(summary, "fixture", usage)

        engine = Engine(store, tmp_path / "native-workspaces", lambda _: NativeWorker(), journal=journal)
        engine.search, engine.tool_broker = search, broker
        await engine.execute_run(run["id"])
        final = await store.get_run(run["id"])
        receipts = await broker.invocations(run["id"])
        assert len(calls) == 1
        assert receipts[0]["status"] == ("unknown" if kind == "unknown" else "rejected")
        assert final["status"] == expected, final.get("error")

        assert not final.get("artifacts") and not final.get("source_tree")
        entries = await journal.entries(run["id"])
        assert journal.verify_chain(entries)
        assert "fixture-owned-tavily-key" not in str(entries)
        if kind != "unknown":
            assert final["pause"]["kind"] == kind
            assert final["pause"]["automatic"] is False
