# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Selected read clients must preserve native API permissions and response semantics."""

import asyncio
import contextlib
import json

import httpx
import pytest
from click.testing import CliRunner
from mcp.server.mcpserver.exceptions import UnexpectedToolError

from firefly_forge import cli
from firefly_forge.client import ForgeClient
from firefly_forge.mcp_server import create_server

IDENTIFIER = "a" * 32
READS = [
    ("agent_role_lifecycle", (), "agent-roles/lifecycle", ["roles", "lifecycle"], "get_agent_role_lifecycle", {}),
    (
        "repository_role_lifecycle",
        (IDENTIFIER,),
        f"repositories/{IDENTIFIER}/agent-roles/lifecycle",
        ["roles", "repository-lifecycle", IDENTIFIER],
        "get_repository_agent_role_lifecycle",
        {"repository_id": IDENTIFIER},
    ),
    ("identity_users", (), "identity/users", ["identity", "users"], "list_identity_users", {}),
    (
        "project_eligible_users",
        (IDENTIFIER, "Stock %_"),
        f"projects/{IDENTIFIER}/eligible-users",
        ["project", "eligible-users", IDENTIFIER, "--query", "Stock %_"],
        "find_project_eligible_users",
        {"project_id": IDENTIFIER, "query": "Stock %_"},
    ),
    (
        "identity_administration",
        (),
        "identity/administration",
        ["identity", "administration"],
        "get_identity_administration",
        {},
    ),
    ("identity_operations", (), "identity/operations", ["identity", "operations"], "list_identity_operations", {}),
]


@pytest.mark.parametrize("method,args,path,command,tool,arguments", READS)
def test_sdk_cli_and_mcp_use_the_same_authenticated_get(method, args, path, command, tool, arguments, monkeypatch):
    payload = (
        [{"id": "owned-result", "state": "unknown", "remote_replayed": False}]
        if method in {"identity_users", "project_eligible_users", "identity_operations"}
        else {"permissions": {"edit_registry": False}, "configured": False}
    )
    calls = []

    def handle(request):
        calls.append((request.method, request.url.path))
        assert request.method == "GET" and request.url.path == "/api/v1/" + path
        assert request.headers["Authorization"] == "Bearer owned-client-token"
        if method == "project_eligible_users":
            assert request.url.params["query"] == "Stock %_"
        assert not request.content
        return httpx.Response(200, json=payload)

    with ForgeClient("https://forge.invalid", "owned-client-token") as connection:
        connection.http.close()
        connection.http = httpx.Client(
            base_url="https://forge.invalid/api/v1/",
            headers={"Authorization": "Bearer owned-client-token"},
            transport=httpx.MockTransport(handle),
        )
        monkeypatch.setattr(cli, "client", lambda: contextlib.nullcontext(connection))
        assert getattr(connection, method)(*args) == payload
        invoked = CliRunner().invoke(cli.main, command)
        assert invoked.exit_code == 0, invoked.output
        assert json.loads(invoked.output) == payload
        result = asyncio.run(create_server().call_tool(tool, arguments))
        assert not result.is_error, str(result)
        assert result.structured_content == ({"result": payload} if isinstance(payload, list) else payload)
        assert len(calls) == 3


@pytest.mark.parametrize(
    "method,args",
    [
        ("repository_role_lifecycle", ("../private",)),
        ("project_eligible_users", ("../private", "Stock")),
        ("project_eligible_users", (IDENTIFIER, "  ")),
        ("project_eligible_users", (IDENTIFIER, "ab")),
        ("project_eligible_users", (IDENTIFIER, "x" * 121)),
    ],
)
def test_invalid_scope_or_query_never_dispatches(method, args, monkeypatch):
    with ForgeClient("https://forge.invalid", "owned-token") as connection:
        calls = []
        monkeypatch.setattr(connection, "request", lambda *a, **k: calls.append((a, k)))
        with pytest.raises(ValueError):
            getattr(connection, method)(*args)
        assert calls == []


async def test_read_mcp_contracts_are_additive_bounded_and_read_only():
    tools = {tool.name: tool for tool in await create_server().list_tools()}
    for *_, name, args in READS:
        assert name in tools
        assert tools[name].annotations.read_only_hint is True
        assert set(tools[name].input_schema["properties"]) == set(args)
    query = tools["find_project_eligible_users"].input_schema["properties"]["query"]
    assert query["minLength"] == 3 and query["maxLength"] == 120
    assert set(tools["find_project_eligible_users"].input_schema["required"]) == {"project_id", "query"}


def test_native_signed_identity_scopes_and_all_six_read_bindings(tmp_path, postgres_schema, monkeypatch):
    import secrets

    from starlette.testclient import TestClient
    from test_identity import fixture, request
    from test_project_access import seed

    from firefly_forge.api import OperatorAuth, create_application
    from firefly_forge.project_access import ProjectAccess
    from firefly_forge.user_administration import KeycloakAdministrationConfiguration, UserAdministration

    monkeypatch.setenv("FORGE_ALLOWED_HOSTS", "forge.example")
    calls = []

    class NativeHTTP(TestClient):
        def request(self, method, url, *args, **kwargs):
            kwargs.pop("timeout", None)
            calls.append((method, str(url)))
            return super().request(method, url, *args, **kwargs)

    with NativeHTTP(
        create_application(tmp_path / "factory", secrets.token_urlsafe(32), role="api"),
        base_url="https://forge.example/api/v1/",
        client=("127.0.0.1", 51000),
    ) as http:
        store = http.app.state.store

        async def setup():
            identity, _, _, external, token = await fixture(store)
            (tmp_path / "fixture").mkdir()
            repo, projects = await seed(store, tmp_path / "fixture")
            signed = token(
                "forge-api",
                roles=["forge-worker"],
                sub="owned-viewer",
                name="Stock %_ owner",
                email="viewer@example.test",
            )
            actor = await identity.authenticate(request(bearer="Bearer " + signed))
            assert actor and not actor.api_roles.intersection({"forge-admin"})
            access = ProjectAccess(store)
            administrator = await identity.authenticate(request(bearer="Bearer " + token("forge-api")))
            await access.set_membership(projects[0]["id"], administrator, "owner")
            await access.set_membership(projects[0]["id"], actor, "owner")
            return identity, external, token, repo, projects[0], signed, actor

        identity, external, token, repo, project, viewer, actor = http.portal.call(setup)
        http.app.state.runtime.context.get_bean(OperatorAuth).identity = identity
        administration = http.app.state.runtime.context.get_bean(UserAdministration)
        administration.identity = identity
        private = secrets.token_urlsafe(32)
        administration.config = KeycloakAdministrationConfiguration(client_id="owned-read-admin", client_secret=private)
        connection = ForgeClient("https://forge.example", token("forge-api"))
        connection.http.close()
        connection.http = http
        monkeypatch.setattr(cli, "client", lambda: contextlib.nullcontext(connection))
        try:
            http.headers["Authorization"] = "Bearer " + token("forge-api")
            calls.clear()
            results = {}
            for method, args, _path, command, tool, arguments in READS:
                selected = repo["id"] if method == "repository_role_lifecycle" else project["id"]
                args = tuple(selected if a == IDENTIFIER else a for a in args)
                command = [selected if a == IDENTIFIER else a for a in command]
                arguments = {k: selected if v == IDENTIFIER else v for k, v in arguments.items()}
                result = getattr(connection, method)(*args)
                results[method] = result
                invoked = CliRunner().invoke(cli.main, command)
                assert invoked.exit_code == 0, invoked.output
                assert json.loads(invoked.output) == result
                received = asyncio.run(create_server().call_tool(tool, arguments))
                assert not received.is_error, str(received)
                assert received.structured_content == ({"result": result} if isinstance(result, list) else result)
            assert all(method == "GET" for method, _ in calls) and len(calls) == 18
            assert results["identity_administration"]["secret_configured"]
            assert not results["identity_administration"]["provisioning_verified"]
            assert private not in json.dumps(results)
            assert results["project_eligible_users"] == [
                row for row in results["identity_users"] if row["id"] == actor.id.hex
            ]
            assert set(results["project_eligible_users"][0]) == {
                "id",
                "display_name",
                "enabled",
                "email",
                "contact_email",
                "email_verified",
            }
            assert results["identity_operations"] == []
            http.headers["Authorization"] = "Bearer " + viewer
            for method in ["identity_users", "identity_administration", "identity_operations"]:
                with pytest.raises(RuntimeError, match="HTTP 403"):
                    getattr(connection, method)()
            denied_cli = CliRunner().invoke(cli.main, ["identity", "users"])
            assert denied_cli.exit_code != 0 and "HTTP 403" in str(denied_cli.exception)
            with pytest.raises(UnexpectedToolError) as denied_mcp:
                asyncio.run(create_server().call_tool("list_identity_users", {}))
            assert isinstance(denied_mcp.value.__cause__, RuntimeError)
            assert "HTTP 403" in str(denied_mcp.value.__cause__)
            global_roles = connection.agent_role_lifecycle()
            scoped = connection.repository_role_lifecycle(repo["id"])
            assert global_roles["permissions"] == {"edit_registry": False, "edit_project": False}
            assert scoped["permissions"] == {"edit_registry": False, "edit_project": True}
            assert connection.project_eligible_users(project["id"], "Stock %_")[0]["id"] == actor.id.hex

            async def revoke():
                await ProjectAccess(store).set_membership(project["id"], actor, "viewer")

            http.portal.call(revoke)
            assert connection.repository_role_lifecycle(repo["id"])["permissions"] == {
                "edit_registry": False,
                "edit_project": False,
            }
            with pytest.raises(RuntimeError, match="HTTP 403"):
                connection.project_eligible_users(project["id"], "Stock %_")
            with pytest.raises(UnexpectedToolError) as denied_search:
                asyncio.run(
                    create_server().call_tool(
                        "find_project_eligible_users", {"project_id": project["id"], "query": "Stock %_"}
                    )
                )
            assert isinstance(denied_search.value.__cause__, RuntimeError)
            assert "HTTP 403" in str(denied_search.value.__cause__)
            http.headers["Authorization"] = "Bearer " + token("wrong-audience")
            with pytest.raises(RuntimeError, match="HTTP 401"):
                connection.agent_role_lifecycle()
            http.headers["Authorization"] = "Bearer owned-invalid-token"
            for method, args, *_ in READS:
                selected = repo["id"] if method == "repository_role_lifecycle" else project["id"]
                args = tuple(selected if a == IDENTIFIER else a for a in args)
                with pytest.raises(RuntimeError, match="HTTP 401"):
                    getattr(connection, method)(*args)
            assert all(path.endswith("/certs") for path in external)
            assert all(method == "GET" for method, _ in calls)
        finally:
            http.portal.call(identity.client.aclose)
