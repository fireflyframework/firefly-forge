# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
import asyncio
import contextlib
import json

import pytest
from click.testing import CliRunner
from starlette.testclient import TestClient
from test_mcp_registry import KEY, protocol

from firefly_forge import cli
from firefly_forge.agent_roles import ProjectRoles
from firefly_forge.api import create_application
from firefly_forge.client import ForgeClient
from firefly_forge.engine import Engine
from firefly_forge.journal import Journal
from firefly_forge.mcp_registry import ExternalMCPRegistry, MCPConnectionInput, MCPGrantInput, MCPReviewInput
from firefly_forge.mcp_server import create_server
from firefly_forge.models import RepositoryInput, RunInput
from firefly_forge.project_models import ProjectDefinition
from firefly_forge.projects import Projects
from firefly_forge.tool_broker import ToolInvocationBroker
from firefly_forge.workspace_tools import tool_catalog


def project_definition(repository_id):
    return ProjectDefinition(
        name="Owned product",
        description="Inspect versioned stock service source",
        topology="monorepo",
        owners=["Fixture owner"],
        components=[
            {
                "id": "stock",
                "name": "Stock API",
                "repository_id": repository_id,
                "responsibility": "Own stock validation",
            }
        ],
    )


@pytest.mark.asyncio
async def test_native_run_freezes_grant_and_broker_records_scope_without_key(store, tmp_path):
    service = ExternalMCPRegistry(store.path.parent)
    service.transport, calls = protocol()
    service.configure("github", MCPConnectionInput(enabled=True, api_key=KEY))
    discovered = await service.discover("github")
    service.review("github", MCPReviewInput(digest=discovered["digest"], tools=["get_file_contents"]))
    repo = await store.add_repository(RepositoryInput(name="Stock", url="https://github.com/team/product.git"))
    project = await Projects(store).configure(project_definition(repo["id"]))
    service.grant(
        project["id"],
        "github",
        MCPGrantInput(repository_id=repo["id"], review_digest=discovered["digest"], tools=["get_file_contents"]),
        repository=repo,
    )
    run = await store.create_run(
        RunInput(
            repository_id=repo["id"],
            project_id=project["id"],
            component_id="stock",
            brief="Inspect actual source",
            agent="agentic",
            roles=ProjectRoles(allowed_tools={"implementer": ["external_mcp"]}),
        ),
        prepared_tools=tool_catalog({"external_mcp": True}),
    )
    snapshot = run["mcp_snapshot"]
    broker = ToolInvocationBroker(store, Journal(store), store.path.parent)
    broker.external_mcp = service
    broker.credential_registry = service.credentials
    await store.acquire_ownership()
    await broker.journal.intent(run["id"], "implement")
    await store.update(run["id"], status="developing", attempt=1)
    args = {
        "connector": "github",
        "tool": "get_file_contents",
        "arguments": {"owner": "team", "repo": "product", "path": "src/stock.py"},
    }

    async def invoke_remote():
        return await service.call(
            snapshot, "github", args["tool"], args["arguments"], roles=["implementer"], stage="implement"
        )

    result = await broker.invoke(
        run["id"],
        "implement",
        "external_mcp:github:get_file_contents",
        "1" * 32,
        args,
        invoke_remote,
        source={"base_revision": "a" * 40, "head": "a" * 40, "source_tree": "b" * 40},
        configuration=snapshot,
    )
    assert result["invocation_id"] == "1" * 32 and result["untrusted"]
    assert (
        await broker.invoke(
            run["id"],
            "implement",
            "external_mcp:github:get_file_contents",
            "1" * 32,
            args,
            invoke_remote,
            source={"base_revision": "a" * 40, "head": "a" * 40, "source_tree": "b" * 40},
            configuration=snapshot,
        )
        == result
    )
    receipts = await broker.invocations(run["id"])
    assert receipts[0]["admission"]["project_id"] == project["id"]
    assert receipts[0]["admission"]["configuration"]["digest"] == snapshot["digest"]
    events = await broker.journal.entries(run["id"])
    assert Journal.verify_chain(events) and KEY not in json.dumps(events)
    assert len([item for item in calls if item["method"] == "tools/call"]) == 1
    service.revoke(project["id"], snapshot["grants"][0]["id"])
    with pytest.raises(ValueError, match="revoked"):
        await broker.invoke(
            run["id"],
            "implement",
            "external_mcp:github:get_file_contents",
            "2" * 32,
            args,
            invoke_remote,
            source={"base_revision": "a" * 40, "head": "a" * 40, "source_tree": "b" * 40},
            configuration=snapshot,
        )
    assert len(await broker.invocations(run["id"])) == 1


def test_native_mcp_registry_sdk_cli_and_own_mcp_share_typed_admin_contract(tmp_path, postgres_schema, monkeypatch):
    async def idle(_self):
        await asyncio.Event().wait()

    monkeypatch.setattr(Engine, "_poll", idle)
    token = "fixture-owned-mcp-admin-token-32characters"

    class InProcess(TestClient):
        def request(self, *args, **kwargs):
            kwargs.pop("timeout", None)
            return super().request(*args, **kwargs)

    with InProcess(
        create_application(tmp_path, token),
        base_url="http://testserver/api/v1/",
        headers={"Authorization": "Bearer " + token},
        client=("127.0.0.1", 50000),
    ) as http:
        connection = ForgeClient("http://testserver", token)
        connection.http.close()
        connection.http = http
        monkeypatch.setattr(cli, "client", lambda: contextlib.nullcontext(connection))
        catalog = connection.external_mcp_catalog()
        assert all(not item["enabled"] for item in catalog)
        assert http.get("tools/mcp", headers={"Authorization": "Bearer invalid"}).status_code == 401
        assert (
            connection.configure_external_mcp("github", {"enabled": True, "api_key": KEY})["readiness"]
            == "discovery_required"
        )
        assert KEY not in json.dumps(connection.external_mcp_catalog())
        viewed = CliRunner().invoke(cli.main, ["tools", "mcp", "catalog"])
        assert viewed.exit_code == 0 and json.loads(viewed.output) == connection.external_mcp_catalog()
        server = create_server()
        result = asyncio.run(server.call_tool("get_external_mcp_catalog", {}))
        assert not result.is_error and KEY not in str(result)
        schemas = http.get("openapi.json").json()
        assert "/api/v1/tools/mcp/projects/{project}/connectors/{connector}/grants" in schemas["paths"]
        assert (
            http.put("tools/mcp/github", json={"enabled": True, "endpoint": "https://attacker.invalid/mcp"}).status_code
            == 422
        )


@pytest.mark.asyncio
async def test_external_credentials_and_grants_require_factory_admin_even_for_project_owner(store, tmp_path):
    from uuid import uuid4

    from starlette.requests import Request

    from firefly_forge.access_routes import AccessRoutes
    from firefly_forge.identity import Principal
    from firefly_forge.project_access import AccessDenied, ProjectAccess

    repo = await store.add_repository(RepositoryInput(name="Stock", url="https://github.com/team/product.git"))
    project = await Projects(store).configure(project_definition(repo["id"]))
    principal = Principal(uuid4(), "https://identity.example/realms/forge", uuid4().hex)
    access = ProjectAccess(store)
    await access.set_membership(project["id"], principal, "owner")
    await access.require_project(principal, project["id"], "members")
    routes = AccessRoutes(access, store)

    async def receive():
        return {"type": "http.request", "body": b"{}", "more_body": False}

    for method, path in [
        ("GET", "tools/mcp"),
        ("PUT", "tools/mcp/github"),
        ("POST", "tools/mcp/github/discover"),
        ("POST", f"tools/mcp/projects/{project['id']}/connectors/github/grants"),
    ]:
        request = Request(
            {"type": "http", "method": method, "path": "/api/v1/" + path, "headers": [], "query_string": b""},
            receive=receive,
        )
        with pytest.raises(AccessDenied, match="administrator"):
            await routes.authorize(principal, request)
