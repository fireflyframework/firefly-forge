# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
import asyncio
import contextlib
import json

import httpx
import pytest
from click.testing import CliRunner
from starlette.testclient import TestClient

from firefly_forge import cli
from firefly_forge.api import create_application
from firefly_forge.client import ForgeClient
from firefly_forge.mcp_server import create_server
from firefly_forge.search_tools import SearchTools


def test_native_tavily_configuration_search_and_quota_sdk_cli_mcp_parity(tmp_path, postgres_schema, monkeypatch):
    token = "search-provider-fixture-owner-token-32chars"
    app = create_application(tmp_path / "factory", token)
    key = "fixture-tavily-private-key"
    calls = []
    status = 200

    def transport(request):
        calls.append(request)
        assert request.method == "POST" and request.url.host == "api.tavily.com"
        assert request.headers["Authorization"] == "Bearer " + key
        assert json.loads(request.content)["include_domains"] == ["fireflyframework.org"]
        if status != 200:
            return httpx.Response(status, text="Do not reflect " + key)
        return httpx.Response(
            200,
            json={
                "results": [
                    {"url": "https://fireflyframework.org/pyfly/", "title": "pyfly", "content": "DI and web modules."}
                ]
            },
        )

    class InProcessClient(TestClient):
        def request(self, *args, **kwargs):
            kwargs.pop("timeout", None)
            return super().request(*args, **kwargs)

    with InProcessClient(
        app,
        base_url="http://testserver/api/v1/",
        headers={"Authorization": "Bearer " + token},
        client=("127.0.0.1", 50000),
    ) as http:
        service = app.state.runtime.context.get_bean(SearchTools)
        transport_client = httpx.AsyncClient(transport=httpx.MockTransport(transport))
        service.client = transport_client
        connection = ForgeClient("http://testserver", token)
        connection.http.close()
        connection.http = http
        monkeypatch.setattr(cli, "client", lambda: contextlib.nullcontext(connection))
        route = "search/configuration"
        assert (
            http.put(
                route, headers={"Authorization": "Bearer invalid"}, json={"provider": "tavily", "api_key": key}
            ).status_code
            == 401
        )
        assert http.put(route, json={"provider": "unknown", "api_key": key}).status_code == 422
        assert (
            http.put(
                route, json={"provider": "tavily", "api_key": key, "include_domains": ["http://localhost"]}
            ).status_code
            == 422
        )
        keyfile = tmp_path / "search.key"
        keyfile.write_text(key)
        keyfile.chmod(0o600)
        configured = CliRunner().invoke(
            cli.main,
            [
                "search",
                "configure",
                "--provider",
                "tavily",
                "--key-file",
                str(keyfile),
                "--domain",
                "fireflyframework.org",
            ],
        )
        assert configured.exit_code == 0, configured.output
        assert key not in configured.output
        assert connection.search_configuration()["include_domains"] == ["fireflyframework.org"]
        assert connection.search_configuration()["provider"] == "tavily"
        response = connection.internet_search("pyfly documentation", 1)
        assert response["results"][0]["snippet"] == "DI and web modules."
        assert connection.search_citation(response["results"][0]["citation_id"])["title"] == "pyfly"
        assert connection.test_search()["provider"] == "tavily"
        server = create_server()
        tools = {tool.name: tool for tool in asyncio.run(server.list_tools())}
        properties = tools["configure_internet_search"].input_schema["properties"]
        assert "api_key" not in properties and "api_key_file" in properties
        assert "tavily" in str(properties["provider"])
        assert "include_domains" in properties
        configured_mcp = asyncio.run(
            server.call_tool(
                "configure_internet_search", {"provider": "tavily", "include_domains": ["fireflyframework.org"]}
            )
        )
        assert not configured_mcp.is_error and key not in str(configured_mcp)
        result_mcp = asyncio.run(
            server.call_tool("search_internet", {"query": "pyfly documentation", "max_results": 1})
        )
        assert not result_mcp.is_error and "DI and web modules" in str(result_mcp)
        assert len(calls) == 3
        schema = http.get("openapi.json").json()
        assert schema["components"]["schemas"]["SearchConfiguration"]["properties"]["api_key"]["writeOnly"] is True
        status = 432
        rejected = http.post("search/query", json={"query": "pyfly documentation"})
        assert rejected.status_code == 409
        assert rejected.json()["search_error"] == {"provider": "tavily", "kind": "quota", "status": 432}
        assert "quota" in rejected.json()["detail"] and key not in rejected.text
        assert len(calls) == 4
        asyncio.run(transport_client.aclose())


@pytest.mark.asyncio
async def test_authenticated_project_member_cannot_access_factory_search_controls(
    tmp_path, postgres_schema, monkeypatch
):
    from uuid import uuid4

    from firefly_forge.identity import IdentityService, Principal

    member = Principal(uuid4(), "https://identity.example", "project-member")

    async def authenticate(_self, _request):
        return member

    monkeypatch.setattr(IdentityService, "authenticate", authenticate)
    app = create_application(tmp_path / "member-factory", "private-test-bootstrap-token-32chars")
    async with app.router.lifespan_context(app):
        service = app.state.runtime.context.get_bean(SearchTools)
        before = service.configuration()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1") as client:
            assert (await client.get("/api/v1/search/configuration")).status_code == 403
            assert (
                await client.put(
                    "/api/v1/search/configuration", json={"provider": "tavily", "api_key": "forbidden-key"}
                )
            ).status_code == 403
            assert (await client.post("/api/v1/search/query", json={"query": "pyfly"})).status_code == 403
            assert (await client.post("/api/v1/search/test")).status_code == 403
            assert service.configuration() == before


@pytest.mark.asyncio
async def test_runner_freezes_tavily_provider_domains_and_private_revision(store, tmp_path):
    from firefly_forge.browser_runner import BrowserRunner
    from firefly_forge.journal import Journal
    from firefly_forge.models import RepositoryInput
    from firefly_forge.runner_configuration import RunnerConfiguration
    from firefly_forge.sandbox import SandboxExecutor
    from firefly_forge.sandbox_models import SandboxInput

    calls = []

    def transport(request):
        calls.append(request)
        assert request.url.host == "api.tavily.com"
        assert request.headers["Authorization"] == "Bearer frozen-tavily-key"
        assert json.loads(request.content)["include_domains"] == ["github.com"]
        return httpx.Response(200, json={"results": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        search = SearchTools(tmp_path / "search", client=client)
        search.configure({"provider": "tavily", "api_key": "frozen-tavily-key", "include_domains": ["github.com"]})
        executor = SandboxExecutor(store, Journal(store))
        runners = RunnerConfiguration(store, executor, search, BrowserRunner(executor, tmp_path / "browser"))
        repo = await store.add_repository(RepositoryInput(name="product", url="https://github.com/team/product.git"))
        frozen = (await runners.capture_run(repo["id"], SandboxInput(internet_search=True)))["search_snapshot"]
        search.configure({"provider": "brave", "api_key": "new-brave-key", "include_domains": []})
        assert frozen["provider"] == "tavily" and frozen["include_domains"] == ["github.com"]
        assert "frozen-tavily-key" not in json.dumps(frozen)
        await search.search("framework docs", snapshot=frozen)
        assert len(calls) == 1
