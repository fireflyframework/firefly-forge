# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Native identity reads and membership decisions use existing scoped authority."""

import asyncio
import contextlib
import json

import httpx
import pytest
import test_identity_write_clients
from click.testing import CliRunner
from mcp.server.mcpserver.exceptions import UnexpectedToolError

from firefly_forge import cli
from firefly_forge.client import ForgeClient
from firefly_forge.mcp_server import create_server

native_identity_writes = test_identity_write_clients.native_identity_writes

PROJECT, USER = "1" * 32, "2" * 32


def invoke(interface, connection, name, arguments):
    if interface == "sdk":
        args = {
            "identity_configuration": (),
            "identity_session": (),
            "project_members": (arguments.get("project_id"),),
            "set_project_membership": (arguments.get("project_id"), arguments.get("user_id"), arguments.get("request")),
        }[name]
        return getattr(connection, name)(*args)
    if interface == "mcp":
        tool = {
            "identity_configuration": "get_identity_configuration",
            "identity_session": "get_identity_session",
            "project_members": "list_project_members",
        }.get(name, name)
        result = asyncio.run(create_server().call_tool(tool, arguments))
        assert not result.is_error, str(result)
        return result.structured_content["result"] if name == "project_members" else result.structured_content
    commands = {
        "identity_configuration": ["identity", "configuration"],
        "identity_session": ["identity", "session"],
        "project_members": ["project", "members", arguments.get("project_id")],
        "set_project_membership": ["project", "member", arguments.get("project_id"), arguments.get("user_id")],
    }
    command = commands[name]
    if name == "set_project_membership":
        role = arguments["request"]["role"]
        command += ["--role", role] if role else ["--revoke"]
    result = CliRunner().invoke(cli.main, command)
    if result.exit_code:
        raise result.exception or AssertionError(result.output)
    return json.loads(result.output)


@pytest.mark.parametrize("interface", ["sdk", "cli", "mcp"])
def test_four_bindings_use_native_paths_and_explicit_membership_body(interface, monkeypatch):
    seen = []

    def transport(request):
        seen.append((request.method, request.url.path, json.loads(request.content) if request.content else None))
        return httpx.Response(
            200,
            json=[{"role": "viewer"}]
            if request.url.path.endswith("/members") and request.method == "GET"
            else {"safe": True},
        )

    with ForgeClient("https://forge.invalid", "owned-token") as connection:
        connection.http.close()
        connection.http = httpx.Client(
            base_url="https://forge.invalid/api/v1/", transport=httpx.MockTransport(transport)
        )
        monkeypatch.setattr(cli, "client", lambda: contextlib.nullcontext(connection))
        for name, arguments in [
            ("identity_configuration", {}),
            ("identity_session", {}),
            ("project_members", {"project_id": PROJECT}),
            ("set_project_membership", {"project_id": PROJECT, "user_id": USER, "request": {"role": None}}),
        ]:
            invoke(interface, connection, name, arguments)
    assert seen == [
        ("GET", "/api/v1/auth/configuration", None),
        ("GET", "/api/v1/auth/session", None),
        ("GET", f"/api/v1/projects/{PROJECT}/members", None),
        ("PUT", f"/api/v1/projects/{PROJECT}/members/{USER}", {"role": None}),
    ]


def test_membership_invalid_decisions_and_paths_are_redacted_before_dispatch(monkeypatch):
    with ForgeClient("https://forge.invalid", "owned-token") as connection:
        calls = []
        monkeypatch.setattr(connection, "request", lambda *a, **k: calls.append((a, k)))
        for body in [
            {},
            {"role": True},
            {"role": "owned-secret-canary"},
            {"role": "owner", "token": "owned-secret-canary"},
        ]:
            with pytest.raises(ValueError) as failure:
                connection.set_project_membership(PROJECT, USER, body)
            assert "owned-secret-canary" not in str(failure.value)
        for project, user in [("../auth", USER), (PROJECT, "not-an-id")]:
            with pytest.raises(ValueError):
                connection.set_project_membership(project, user, {"role": "viewer"})
        assert not calls


def test_cli_requires_one_explicit_membership_decision(monkeypatch):
    def unexpected():
        raise AssertionError("Incomplete decision must not acquire a client")

    monkeypatch.setattr(cli, "client", unexpected)
    for decision in [[], ["--revoke", "--role", "owner"]]:
        result = CliRunner().invoke(cli.main, ["project", "member", PROJECT, USER, *decision])
        assert result.exit_code != 0 and not isinstance(result.exception, AssertionError)


@pytest.mark.parametrize("interface", ["cli", "mcp"])
def test_oauth_revision_is_optional_additive_and_strict(interface, monkeypatch):
    with ForgeClient("https://forge.invalid", "owned-token") as connection:
        seen = []

        def record(method, path, **kwargs):
            seen.append(kwargs["json"])
            return {"revision": 2}

        monkeypatch.setattr(connection, "request", record)
        monkeypatch.setattr(cli, "client", lambda: contextlib.nullcontext(connection))
        if interface == "cli":
            for options in [[], ["--expected-revision", "1"]]:
                result = CliRunner().invoke(
                    cli.main,
                    [
                        "connections",
                        "oauth",
                        "configure",
                        "owned",
                        "--provider",
                        "figma",
                        "--client-id",
                        "client",
                        "--public-client",
                        *options,
                    ],
                )
                assert result.exit_code == 0, result.output
        else:
            for revision in [{}, {"expected_revision": 1}]:
                result = asyncio.run(
                    create_server().call_tool(
                        "configure_connection_oauth",
                        {
                            "name": "owned",
                            "provider": "figma",
                            "client_id": "client",
                            "public_client": True,
                            **revision,
                        },
                    )
                )
                assert not result.is_error
            for value in [True, "1", -1]:
                with pytest.raises(UnexpectedToolError):
                    asyncio.run(
                        create_server().call_tool(
                            "configure_connection_oauth",
                            {
                                "name": "owned",
                                "provider": "figma",
                                "client_id": "client",
                                "public_client": True,
                                "expected_revision": value,
                            },
                        )
                    )
        assert [body["expected_revision"] for body in seen] == [None, 1]


@pytest.mark.parametrize("interface", ["sdk", "cli", "mcp"])
def test_native_membership_current_owner_last_owner_foreign_scope_and_revocation(
    interface, native_identity_writes, tmp_path
):
    from sqlalchemy import select
    from test_project_access import seed

    from firefly_forge.access_persistence import ForgeAccessEventEntity

    http, connection, _, token, calls, _, _ = native_identity_writes
    admin = invoke(interface, connection, "identity_session", {})
    assert admin["global_admin"] is True and admin["user"]["authentication_method"] == "oidc_bearer"
    _, projects = http.portal.call(seed, http.app.state.store, tmp_path, 2)
    project, foreign = [row["id"] for row in projects]
    http.headers["Authorization"] = "Bearer " + token("forge-api", roles=["forge-worker"], sub="owned-owner")
    owner = invoke(interface, connection, "identity_session", {})
    assert owner["global_admin"] is False
    owner_id = owner["user"]["id"]
    http.headers["Authorization"] = "Bearer " + token("forge-api")
    invoke(
        interface,
        connection,
        "set_project_membership",
        {"project_id": project, "user_id": owner_id, "request": {"role": "owner"}},
    )
    http.headers["Authorization"] = "Bearer " + token("forge-api", roles=["forge-worker"], sub="owned-owner")
    members = invoke(interface, connection, "project_members", {"project_id": project})
    assert members[0]["user_id"] == owner_id and members[0]["active"] is True and members[0]["revision"] == 1
    assert set(members[0]) == {
        "user_id",
        "display_name",
        "email",
        "contact_email",
        "email_verified",
        "role",
        "active",
        "revision",
    }
    for name, arguments in [
        ("project_members", {"project_id": foreign}),
        ("set_project_membership", {"project_id": foreign, "user_id": owner_id, "request": {"role": "viewer"}}),
    ]:
        with pytest.raises((RuntimeError, UnexpectedToolError)) as failure:
            invoke(interface, connection, name, arguments)
        assert "HTTP 403" in str(failure.value) + str(failure.value.__cause__)
    with pytest.raises((RuntimeError, UnexpectedToolError)) as failure:
        invoke(
            interface,
            connection,
            "set_project_membership",
            {"project_id": project, "user_id": owner_id, "request": {"role": None}},
        )
    assert "last owner" in str(failure.value) + str(failure.value.__cause__)
    # A second owner must exist before the original membership can be removed.
    invoke(
        interface,
        connection,
        "set_project_membership",
        {"project_id": project, "user_id": admin["user"]["id"], "request": {"role": "owner"}},
    )
    invoke(
        interface,
        connection,
        "set_project_membership",
        {"project_id": project, "user_id": owner_id, "request": {"role": None}},
    )
    with pytest.raises((RuntimeError, UnexpectedToolError)) as failure:
        invoke(interface, connection, "project_members", {"project_id": project})
    assert "HTTP 403" in str(failure.value) + str(failure.value.__cause__)
    with pytest.raises((RuntimeError, UnexpectedToolError)) as failure:
        invoke(
            interface,
            connection,
            "set_project_membership",
            {"project_id": project, "user_id": admin["user"]["id"], "request": {"role": "viewer"}},
        )
    assert "HTTP 403" in str(failure.value) + str(failure.value.__cause__)
    current = invoke(interface, connection, "identity_session", {})
    assert current["memberships"] == []
    http.headers["Authorization"] = "Bearer " + token("forge-api")
    members = invoke(interface, connection, "project_members", {"project_id": project})
    removed = next(row for row in members if row["user_id"] == owner_id)
    assert removed["active"] is False and removed["revision"] == 2

    async def events():
        async with http.app.state.store._unit() as unit:
            return [row.data for row in await unit.scalars(select(ForgeAccessEventEntity))]

    history = http.portal.call(events)
    assert len(history) == 3 and all(row["kind"] == "membership_changed" for row in history)
    assert history[-1]["actor_id"] == owner_id and history[-1]["role"] is None
    assert not calls  # No external user administration or mail operation.


@pytest.mark.parametrize("interface", ["sdk", "cli", "mcp"])
def test_native_public_configuration_and_signed_cookie_session_are_safe_current_reads(
    interface, native_identity_writes
):
    from urllib.parse import parse_qs, urlsplit

    from firefly_forge.identity import IdentityService

    http, connection, administration, token, calls, _, _ = native_identity_writes
    del http.headers["Authorization"]
    configuration = invoke(interface, connection, "identity_configuration", {})
    assert configuration["mode"] == "oidc" and configuration["session_path"] == "/api/v1/auth/session"
    assert "owned-studio-secret" not in json.dumps(configuration)
    with pytest.raises((RuntimeError, UnexpectedToolError)) as failure:
        invoke(interface, connection, "identity_session", {})
    assert "HTTP 401" in str(failure.value) + str(failure.value.__cause__)
    identity = http.app.state.runtime.context.get_bean(IdentityService)
    assert identity is administration.identity
    original = identity.client
    nonce = {}

    async def transport(request):
        if request.url.path.endswith("/token"):
            return httpx.Response(
                200,
                json={
                    "id_token": token("forge-ui", nonce=nonce["value"]),
                    "access_token": token("forge-api"),
                    "refresh_token": "owned-refresh-canary",
                    "expires_in": 300,
                    "token_type": "Bearer",
                },
            )
        return await original.send(original.build_request(request.method, request.url, content=request.content))

    async def prepare():
        identity.client = httpx.AsyncClient(transport=httpx.MockTransport(transport), trust_env=False)

    http.portal.call(prepare)
    try:
        http.headers["X-XSRF-TOKEN"] = http.cookies["XSRF-TOKEN"]
        login = http.post("auth/login", headers={"Origin": "https://forge.example"}, json={})
        assert login.status_code == 200, login.text
        params = parse_qs(urlsplit(login.json()["authorization_url"]).query)
        nonce["value"] = params["nonce"][0]
        callback = http.get(
            "auth/callback",
            params={"code": "owned-code", "state": params["state"][0], "iss": configuration["issuer"]},
            follow_redirects=False,
        )
        assert callback.status_code == 303, callback.text
        assert http.cookies.get("forge_session")
        result = invoke(interface, connection, "identity_session", {})
        assert (
            result["authenticated"]
            and result["global_admin"]
            and result["user"]["authentication_method"] == "oidc_session"
        )
        assert set(result) == {"authenticated", "user", "global_admin", "memberships", "roles"}
        for private in [http.cookies["forge_session"], "owned-refresh-canary", "owned-studio-secret"]:
            assert private not in json.dumps(result)

        async def suspend():
            async with identity.store._unit():
                user = await identity.users.find_by_id(__import__("uuid").UUID(result["user"]["id"]))
                user.enabled = False
                await identity.users.save(user)

        http.portal.call(suspend)
        with pytest.raises((RuntimeError, UnexpectedToolError)) as failure:
            invoke(interface, connection, "identity_session", {})
        assert "HTTP 401" in str(failure.value) + str(failure.value.__cause__)
        assert not calls
    finally:
        http.portal.call(identity.client.aclose)
        identity.client = original


@pytest.mark.parametrize("interface", ["sdk", "cli", "mcp"])
def test_native_oauth_optional_compare_and_swap_never_replaces_stale_configuration(
    interface, tmp_path, postgres_schema, monkeypatch
):
    import secrets

    from starlette.testclient import TestClient
    from test_oauth_provider_policy_api import BASE, PROVIDER, registration

    from firefly_forge.api import create_application

    operator = secrets.token_urlsafe(32)
    monkeypatch.setenv("FORGE_OAUTH_LOCAL_DEVELOPMENT", "1")
    monkeypatch.setenv("FORGE_OIDC_ALLOW_LOOPBACK_HTTP", "true")
    monkeypatch.setenv("FORGE_BROWSER_ORIGIN", "https://127.0.0.1:49200")
    monkeypatch.setenv("FORGE_ALLOWED_HOSTS", "127.0.0.1")

    class NativeHTTP(TestClient):
        def request(self, *args, **kwargs):
            kwargs.pop("timeout", None)
            return super().request(*args, **kwargs)

    with NativeHTTP(
        create_application(tmp_path, operator, role="api"),
        base_url="https://127.0.0.1:49200/api/v1/",
        headers={"Authorization": "Bearer " + operator},
        client=("127.0.0.1", 50000),
    ) as http:
        connection = ForgeClient("https://127.0.0.1:49200", operator)
        connection.http.close()
        connection.http = http
        monkeypatch.setattr(cli, "client", lambda: contextlib.nullcontext(connection))
        assert http.put(BASE.removeprefix("/api/v1/") + "/" + PROVIDER, json=registration()).status_code == 200

        def configure(client_id, revision=None):
            body = {"provider": PROVIDER, "client_id": client_id, "public_client": True}
            if revision is not None:
                body["expected_revision"] = revision
            if interface == "sdk":
                return connection.configure_connection_oauth("owned", body)
            if interface == "mcp":
                result = asyncio.run(create_server().call_tool("configure_connection_oauth", {"name": "owned", **body}))
                assert not result.is_error
                return result.structured_content
            command = [
                "connections",
                "oauth",
                "configure",
                "owned",
                "--provider",
                PROVIDER,
                "--client-id",
                client_id,
                "--public-client",
            ]
            if revision is not None:
                command += ["--expected-revision", str(revision)]
            result = CliRunner().invoke(cli.main, command)
            if result.exit_code:
                raise result.exception or AssertionError(result.output)
            return json.loads(result.output)

        assert configure("original")["revision"] == 1
        assert configure("current", 1)["revision"] == 2
        with pytest.raises((RuntimeError, UnexpectedToolError)) as failure:
            configure("stale", 1)
        assert "HTTP 409" in str(failure.value) + str(failure.value.__cause__)
        retained = connection.connection_oauth("owned")
        assert retained["revision"] == 2 and retained["client_id"] == "current"
        assert configure("legacy-no-revision")["revision"] == 3


@pytest.mark.parametrize("interface", ["sdk", "cli", "mcp"])
def test_membership_lost_response_is_not_retried(interface, monkeypatch):
    seen = []

    def lost(request):
        seen.append(request.url.path)
        raise httpx.ReadTimeout("Owned uncertain response", request=request)

    with ForgeClient("https://forge.invalid", "owned-token") as connection:
        connection.http.close()
        connection.http = httpx.Client(base_url="https://forge.invalid/api/v1/", transport=httpx.MockTransport(lost))
        monkeypatch.setattr(cli, "client", lambda: contextlib.nullcontext(connection))
        with pytest.raises((httpx.ReadTimeout, UnexpectedToolError)):
            invoke(
                interface,
                connection,
                "set_project_membership",
                {"project_id": PROJECT, "user_id": USER, "request": {"role": "viewer"}},
            )
    assert seen == [f"/api/v1/projects/{PROJECT}/members/{USER}"]


async def test_mcp_membership_has_native_required_strict_body_and_read_annotations(monkeypatch):
    from firefly_forge.access_controllers import MembershipInput
    from firefly_forge.connection_oauth_models import OAuthConnectionInput

    tools = {tool.name: tool for tool in await create_server().list_tools()}
    assert len(tools) == 188
    for name in ["get_identity_configuration", "get_identity_session", "list_project_members"]:
        assert tools[name].annotations.read_only_hint is True
    membership = tools["set_project_membership"]
    assert membership.annotations.read_only_hint is False and membership.annotations.destructive_hint is True
    assert membership.input_schema["properties"]["request"] == MembershipInput.model_json_schema()
    assert membership.input_schema["required"] == ["project_id", "user_id", "request"]
    assert (
        tools["configure_connection_oauth"].input_schema["properties"]["expected_revision"]
        == OAuthConnectionInput.model_json_schema()["properties"]["expected_revision"]
    )
    with ForgeClient("https://forge.invalid", "owned-token") as connection:
        seen = []
        monkeypatch.setattr(connection, "request", lambda *a, **k: seen.append((a, k)))
        monkeypatch.setattr(cli, "client", lambda: contextlib.nullcontext(connection))
        for body in [{"role": True}, {"role": "owned-private-canary"}, {"role": None, "token": "owned-private-canary"}]:
            with pytest.raises(UnexpectedToolError) as failure:
                await create_server().call_tool(
                    "set_project_membership", {"project_id": PROJECT, "user_id": USER, "request": body}
                )
            assert "owned-private-canary" not in str(failure.value.__cause__)
        assert not seen
