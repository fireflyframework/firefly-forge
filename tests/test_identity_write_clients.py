# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Identity clients preserve existing native custody rather than inventing effects."""

import asyncio
import contextlib
import json
from uuid import uuid4

import httpx
import pytest
from click.testing import CliRunner
from mcp.server.mcpserver.exceptions import UnexpectedToolError

from firefly_forge import cli
from firefly_forge.client import ForgeClient
from firefly_forge.mcp_server import create_server

USER, OPERATION, REQUEST = "1" * 32, "2" * 32, "3" * 32
WRITES = [
    (
        "set_identity_user_enabled",
        (USER, False),
        "PUT",
        f"identity/users/{USER}",
        {"enabled": False},
        ["set-enabled", USER, "--disable"],
        {"user_id": USER, "enabled": False},
    ),
    (
        "provision_identity_user",
        ({"request_id": REQUEST, "username": "fictional-owner"},),
        "POST",
        "identity/users",
        {"request_id": REQUEST, "username": "fictional-owner", "email": None, "first_name": None, "last_name": None},
        ["provision"],
        {"request": {"request_id": REQUEST, "username": "fictional-owner"}},
    ),
    (
        "invite_identity_user",
        (USER, {"request_id": REQUEST}),
        "POST",
        f"identity/users/{USER}/invite",
        {"request_id": REQUEST},
        ["invite", USER, "--request-id", REQUEST],
        {"user_id": USER, "request_id": REQUEST},
    ),
    (
        "reconcile_identity_operation",
        (OPERATION, {"outcome": "not_created", "note": "Inspected exact intent"}),
        "POST",
        f"identity/operations/{OPERATION}/reconcile",
        {"outcome": "not_created", "subject": None, "note": "Inspected exact intent"},
        ["reconcile", OPERATION, "--outcome", "not_created", "--note", "Inspected exact intent"],
        {"operation_id": OPERATION, "request": {"outcome": "not_created", "note": "Inspected exact intent"}},
    ),
    (
        "recover_identity_operation",
        (OPERATION, {"request_id": REQUEST, "owner_stopped": True, "note": "Owner stopped"}),
        "POST",
        f"identity/operations/{OPERATION}/recover",
        {"request_id": REQUEST, "owner_stopped": True, "note": "Owner stopped"},
        ["recover", OPERATION, "--request-id", REQUEST, "--owner-stopped", "--note", "Owner stopped"],
        {"operation_id": OPERATION, "request": {"request_id": REQUEST, "owner_stopped": True, "note": "Owner stopped"}},
    ),
]


@pytest.mark.parametrize("name,args,verb,path,body,command,arguments", WRITES)
def test_five_writes_dispatch_once_with_same_scoped_body_across_clients(
    name, args, verb, path, body, command, arguments, tmp_path, monkeypatch
):
    seen = []

    def handle(request):
        seen.append((request.method, request.url.path, json.loads(request.content)))
        assert request.headers["Authorization"] == "Bearer owned-write-token"
        return httpx.Response(200, json={"id": OPERATION, "status": "needs_reconciliation", "remote_replayed": False})

    with ForgeClient("https://forge.invalid", "owned-write-token") as connection:
        connection.http.close()
        connection.http = httpx.Client(
            base_url="https://forge.invalid/api/v1/",
            headers={"Authorization": "Bearer owned-write-token"},
            transport=httpx.MockTransport(handle),
        )
        monkeypatch.setattr(cli, "client", lambda: contextlib.nullcontext(connection))
        assert getattr(connection, name)(*args)["status"] == "needs_reconciliation"
        if name == "provision_identity_user":
            document = tmp_path / "user.json"
            document.write_text(json.dumps(args[0]))
            command = [*command, str(document)]
        result = CliRunner().invoke(cli.main, ["identity", *command])
        assert result.exit_code == 0, result.output
        assert json.loads(result.output)["remote_replayed"] is False
        result = asyncio.run(create_server().call_tool(name, arguments))
        assert not result.is_error and result.structured_content["status"] == "needs_reconciliation"
    assert seen == [(verb, "/api/v1/" + path, body)] * 3


@pytest.mark.parametrize("enabled", [0, 1, "false", "true", None])
def test_enabled_write_rejects_coercion_before_any_dispatch(enabled, monkeypatch):
    with ForgeClient("https://forge.invalid", "owned-token") as connection:
        seen = []
        monkeypatch.setattr(connection, "request", lambda *a, **k: seen.append((a, k)))
        with pytest.raises(ValueError):
            connection.set_identity_user_enabled(USER, enabled)
        assert not seen


def test_forbidden_provision_fields_and_malformed_ids_never_reflect_or_dispatch(monkeypatch):
    secret = "fictional-private-password-canary"
    with ForgeClient("https://forge.invalid", "owned-token") as connection:
        seen = []
        monkeypatch.setattr(connection, "request", lambda *a, **k: seen.append((a, k)))
        for extra in [{"password": secret}, {"roles": ["forge-admin"]}, {"email_verified": True}]:
            with pytest.raises(ValueError) as failure:
                connection.provision_identity_user({"request_id": REQUEST, "username": "owner", **extra})
            assert secret not in str(failure.value)
        for value in ["../another-user", "not-an-id"]:
            with pytest.raises(ValueError):
                connection.invite_identity_user(value, {"request_id": REQUEST})
        with pytest.raises(ValueError):
            connection.recover_identity_operation(
                OPERATION, {"request_id": REQUEST, "owner_stopped": 1, "note": "Unconfirmed"}
            )
        assert not seen


def test_cli_requires_explicit_enabled_and_owner_stopped_decisions(monkeypatch):
    def unexpected_client():
        raise AssertionError("An incomplete decision must never acquire a client")

    monkeypatch.setattr(cli, "client", unexpected_client)
    for command in [
        ["identity", "set-enabled", USER],
        ["identity", "recover", OPERATION, "--request-id", REQUEST, "--note", "Unknown owner"],
    ]:
        result = CliRunner().invoke(cli.main, command)
        assert result.exit_code != 0
        assert not isinstance(result.exception, AssertionError)


async def test_mutation_tools_have_existing_dto_schemas_and_no_extra_outcome_claims():
    tools = {tool.name: tool for tool in await create_server().list_tools()}
    for name, *_ in WRITES:
        assert name in tools
        assert tools[name].annotations.read_only_hint is False
        assert tools[name].annotations.destructive_hint is True
    provision = tools["provision_identity_user"].input_schema["properties"]["request"]
    assert provision["additionalProperties"] is False
    assert set(provision["properties"]) == {"request_id", "username", "email", "first_name", "last_name"}
    recover = tools["recover_identity_operation"].input_schema["properties"]["request"]
    assert recover["properties"]["owner_stopped"]["const"] is True
    assert set(recover["properties"]) == {"request_id", "owner_stopped", "note"}


@pytest.mark.parametrize("value", [1, "false", "owned-private-boolean-canary"])
async def test_mcp_enabled_value_is_not_coerced_or_reflected(value, monkeypatch):
    with ForgeClient("https://forge.invalid", "owned-token") as connection:
        seen = []
        monkeypatch.setattr(connection, "request", lambda *a, **k: seen.append((a, k)))
        monkeypatch.setattr(cli, "client", lambda: contextlib.nullcontext(connection))
        with pytest.raises(UnexpectedToolError) as failure:
            await create_server().call_tool("set_identity_user_enabled", {"user_id": USER, "enabled": value})
        assert "owned-private-boolean-canary" not in str(failure.value.__cause__)
        assert not seen


@pytest.fixture
def native_identity_writes(tmp_path, postgres_schema, monkeypatch):
    import secrets

    from cryptography.fernet import Fernet
    from starlette.testclient import TestClient
    from test_identity import fixture

    from firefly_forge.api import create_application
    from firefly_forge.identity import IdentityService
    from firefly_forge.user_administration import KeycloakAdministrationConfiguration, UserAdministration

    monkeypatch.setenv("FORGE_ALLOWED_HOSTS", "forge.example")
    for key, value in {
        "FORGE_AUTH_MODE": "oidc",
        "FORGE_OIDC_ISSUER": "https://id.example/realms/forge",
        "FORGE_OIDC_CLIENT_ID": "forge-ui",
        "FORGE_OIDC_CLIENT_SECRET": "owned-studio-secret",
        "FORGE_OIDC_API_AUDIENCE": "forge-api",
        "FORGE_OIDC_REDIRECT_URI": "https://forge.example/api/v1/auth/callback",
        "FORGE_IDENTITY_ENCRYPTION_KEY": Fernet.generate_key().decode(),
    }.items():
        monkeypatch.setenv(key, value)
    calls, accounts, settings = [], {}, {"uncertain_create": False, "uncertain_invite": False}

    class NativeHTTP(TestClient):
        def request(self, method, url, *args, **kwargs):
            kwargs.pop("timeout", None)
            return super().request(method, url, *args, **kwargs)

    with NativeHTTP(
        create_application(tmp_path / "native", secrets.token_urlsafe(32), role="api"),
        base_url="https://forge.example/api/v1/",
        client=("127.0.0.1", 52000),
    ) as http:
        context = http.app.state.runtime.context
        administration = context.get_bean(UserAdministration)
        identity = context.get_bean(IdentityService)
        assert administration.identity is identity and administration.store is http.app.state.store

        async def setup():
            prepared, _, _, external, token = await fixture(http.app.state.store)
            await identity.client.aclose()
            identity.client = prepared.client
            assert identity.config.mode == "oidc" and identity.registration is not None
            await administration.client.aclose()
            administration.config = KeycloakAdministrationConfiguration(
                client_id="owned-user-admin", client_secret="owned-private-admin-secret"
            )

            async def provider(request):
                calls.append((request.method, request.url.path))
                if request.url.path.endswith("/token"):
                    assert b"grant_type=client_credentials" in request.content
                    return httpx.Response(
                        200,
                        json={
                            "access_token": token(
                                "realm-management",
                                sub="owned-service",
                                azp="owned-user-admin",
                                resource_access={"realm-management": {"roles": ["view-users", "manage-users"]}},
                            )
                        },
                    )
                if request.method == "POST" and request.url.path.endswith("/users"):
                    from firefly_forge.user_administration_persistence import ForgeIdentityAdminOperationRepository

                    async with http.app.state.store._unit():
                        rows = await ForgeIdentityAdminOperationRepository().latest()
                        assert any(row.status == "executing" for row in rows)
                    subject = str(uuid4())
                    accounts[subject] = {**json.loads(request.content), "id": subject}
                    if settings["uncertain_create"]:
                        raise httpx.ReadTimeout("unknown provider admission", request=request)
                    return httpx.Response(
                        201, headers={"Location": "https://id.example/admin/realms/forge/users/" + subject}
                    )
                if request.method == "GET" and "/users/" in request.url.path:
                    return httpx.Response(200, json=accounts[request.url.path.rsplit("/", 1)[1]])
                if request.method == "PUT" and request.url.path.endswith("/execute-actions-email"):
                    assert json.loads(request.content) == ["UPDATE_PASSWORD", "VERIFY_EMAIL"]
                    if settings["uncertain_invite"]:
                        raise httpx.ReadTimeout("unknown mail admission", request=request)
                    return httpx.Response(204)
                raise AssertionError("Unapproved external fixture request")

            administration.client = httpx.AsyncClient(transport=httpx.MockTransport(provider), trust_env=False)
            return token, external

        token, external = http.portal.call(setup)
        connection = ForgeClient("https://forge.example", token("forge-api"))
        connection.http.close()
        connection.http = http
        http.headers["Authorization"] = "Bearer " + token("forge-api")
        monkeypatch.setattr(cli, "client", lambda: contextlib.nullcontext(connection))
        yield http, connection, administration, token, calls, accounts, settings
        assert all(path.endswith("/certs") for path in external)


def write(interface, connection, name, arguments, tmp_path):
    user = arguments.get("user_id")
    operation = arguments.get("operation_id")
    body = arguments.get("request", {})
    if interface == "sdk":
        args = {
            "set_identity_user_enabled": (user, arguments.get("enabled")),
            "provision_identity_user": (body,),
            "invite_identity_user": (user, {"request_id": arguments.get("request_id")}),
            "reconcile_identity_operation": (operation, body),
            "recover_identity_operation": (operation, body),
        }[name]
        return getattr(connection, name)(*args)
    if interface == "mcp":
        result = asyncio.run(create_server().call_tool(name, arguments))
        assert not result.is_error, str(result)
        return result.structured_content
    commands = {
        "set_identity_user_enabled": ["set-enabled", user, "--enable" if arguments.get("enabled") else "--disable"],
        "invite_identity_user": ["invite", user, "--request-id", arguments.get("request_id")],
        "reconcile_identity_operation": [
            "reconcile",
            operation,
            "--outcome",
            body.get("outcome"),
            "--note",
            body.get("note"),
        ],
        "recover_identity_operation": [
            "recover",
            operation,
            "--request-id",
            body.get("request_id"),
            "--owner-stopped",
            "--note",
            body.get("note"),
        ],
    }
    if name == "provision_identity_user":
        path = tmp_path / "owned-user.json"
        path.write_text(json.dumps(body))
        command = ["provision", str(path)]
    else:
        command = commands[name]
        if name == "reconcile_identity_operation" and body.get("subject"):
            command.extend(["--subject", body["subject"]])
    result = CliRunner().invoke(cli.main, ["identity", *command])
    if result.exit_code:
        raise result.exception or AssertionError(result.output)
    return json.loads(result.output)


@pytest.mark.parametrize("name,args,verb,path,body,command,arguments", WRITES)
def test_factory_transport_loss_never_retries_an_identity_effect(
    name, args, verb, path, body, command, arguments, tmp_path, monkeypatch
):
    seen = []

    def lost_response(request):
        seen.append((request.method, request.url.path))
        raise httpx.ReadTimeout("Owned factory response unavailable", request=request)

    with ForgeClient("https://forge.invalid", "owned-token") as connection:
        connection.http.close()
        connection.http = httpx.Client(
            base_url="https://forge.invalid/api/v1/", transport=httpx.MockTransport(lost_response)
        )
        monkeypatch.setattr(cli, "client", lambda: contextlib.nullcontext(connection))
        for interface in ["sdk", "cli", "mcp"]:
            with pytest.raises((httpx.ReadTimeout, UnexpectedToolError)):
                write(interface, connection, name, arguments, tmp_path)
    assert seen == [(verb, "/api/v1/" + path)] * 3


@pytest.mark.parametrize("interface", ["sdk", "cli", "mcp"])
def test_native_five_write_bindings_keep_intents_receipts_and_unknown_effects(
    interface, native_identity_writes, tmp_path
):
    from firefly_forge.user_administration import UserProvisionInput

    http, connection, administration, token, calls, accounts, settings = native_identity_writes
    create = {"request": {"request_id": uuid4().hex, "username": "fictional-owner", "email": "owner@example.test"}}
    created = write(interface, connection, "provision_identity_user", create, tmp_path)
    assert created["status"] == "completed" and created["project_memberships_granted"] is False
    assert created["invitation_status"] == "not_requested"
    assert write(interface, connection, "provision_identity_user", create, tmp_path) == created
    assert sum(method == "POST" and path.endswith("/users") for method, path in calls) == 1
    with pytest.raises((ValueError, RuntimeError, UnexpectedToolError)):
        write(
            interface,
            connection,
            "provision_identity_user",
            {"request": {**create["request"], "username": "changed-request"}},
            tmp_path,
        )
    assert sum(method == "POST" and path.endswith("/users") for method, path in calls) == 1
    invite = {"user_id": created["user_id"], "request_id": uuid4().hex}
    invited = write(interface, connection, "invite_identity_user", invite, tmp_path)
    assert invited["invitation_status"] == "provider_accepted" and invited["mail_delivery_verified"] is False
    assert write(interface, connection, "invite_identity_user", invite, tmp_path) == invited
    assert sum(method == "PUT" for method, _ in calls) == 1
    suspended = write(
        interface, connection, "set_identity_user_enabled", {"user_id": created["user_id"], "enabled": False}, tmp_path
    )
    assert suspended["enabled"] is False and suspended["previous_enabled"] is True
    enabled = write(
        interface, connection, "set_identity_user_enabled", {"user_id": created["user_id"], "enabled": True}, tmp_path
    )
    assert enabled["enabled"] is True

    settings["uncertain_create"] = True
    unknown = {"request": {"request_id": uuid4().hex, "username": "uncertain-owner"}}
    receipt = write(interface, connection, "provision_identity_user", unknown, tmp_path)
    assert receipt["status"] == "needs_reconciliation"
    count = len(calls)
    assert write(interface, connection, "provision_identity_user", unknown, tmp_path) == receipt
    assert len(calls) == count
    subject = next(subject for subject, account in accounts.items() if account["username"] == "uncertain-owner")
    reconciled = write(
        interface,
        connection,
        "reconcile_identity_operation",
        {
            "operation_id": receipt["id"],
            "request": {"outcome": "created", "subject": subject, "note": "Inspected exact provider marker"},
        },
        tmp_path,
    )
    assert (
        reconciled["status"] == "completed"
        and reconciled["provider_outcome_evidence"] == "exact_provider_request_marker"
    )
    assert sum(method == "POST" and path.endswith("/users") for method, path in calls) == 2

    async def interrupted_intent():
        from test_identity import request

        actor = await administration.identity.authenticate(request(bearer="Bearer " + token("forge-api")))
        row, claimed = await administration._claim(
            "provision", UserProvisionInput(request_id=uuid4().hex, username="stopped-owner").model_dump(), actor
        )
        assert claimed
        return row.id.hex

    operation_id = http.portal.call(interrupted_intent)
    recovery = {
        "operation_id": operation_id,
        "request": {"request_id": uuid4().hex, "owner_stopped": True, "note": "Original owned execution stopped"},
    }
    count = len(calls)
    recovered = write(interface, connection, "recover_identity_operation", recovery, tmp_path)
    assert recovered["status"] == "needs_reconciliation"
    assert write(interface, connection, "recover_identity_operation", recovery, tmp_path) == recovered
    closed = write(
        interface,
        connection,
        "reconcile_identity_operation",
        {"operation_id": operation_id, "request": {"outcome": "not_created", "note": "Inspected no provider account"}},
        tmp_path,
    )
    assert closed["status"] == "closed_not_created" and closed["provider_outcome_evidence"] == "operator_attestation"
    assert len(calls) == count
    receipts = connection.identity_operations()
    assert all(row["journal_valid"] for row in receipts)
    assert "owned-private-admin-secret" not in json.dumps(receipts)
    assert (
        sum(
            event["kind"] == "owner_stopped_confirmation"
            for row in receipts
            if row["id"] == operation_id
            for event in row["journal"]
        )
        == 1
    )


@pytest.mark.parametrize("interface", ["sdk", "cli", "mcp"])
def test_native_nonadministrator_and_suspended_actor_cannot_dispatch_identity_writes(
    interface, native_identity_writes, tmp_path
):
    http, connection, administration, token, calls, _, _ = native_identity_writes
    http.headers["Authorization"] = "Bearer " + token("forge-api", roles=["forge-worker"], sub="viewer")
    for name, _, _, _, _, _, arguments in WRITES:
        with pytest.raises((RuntimeError, UnexpectedToolError)) as denied:
            write(interface, connection, name, arguments, tmp_path)
        assert "HTTP 403" in str(denied.value) or "HTTP 403" in str(denied.value.__cause__)
    assert not calls

    async def suspend():
        from test_identity import request

        actor = await administration.identity.authenticate(request(bearer="Bearer " + token("forge-api")))
        async with administration.store._unit():
            user = await administration.identity.users.find_by_id(actor.id)
            user.enabled = False
            await administration.identity.users.save(user)

    http.portal.call(suspend)
    http.headers["Authorization"] = "Bearer " + token("forge-api")
    for name, _, _, _, _, _, arguments in WRITES:
        with pytest.raises((RuntimeError, UnexpectedToolError)) as denied:
            write(interface, connection, name, arguments, tmp_path)
        assert "HTTP 401" in str(denied.value) or "HTTP 401" in str(denied.value.__cause__)
    assert not calls


@pytest.mark.parametrize("interface", ["sdk", "cli", "mcp"])
def test_native_unknown_invite_and_forbidden_claims_never_resubmit_or_leak(interface, native_identity_writes, tmp_path):
    _, connection, _, _, calls, _, settings = native_identity_writes
    created = write(
        interface,
        connection,
        "provision_identity_user",
        {"request": {"request_id": uuid4().hex, "username": "invite-owner", "email": "owner@example.test"}},
        tmp_path,
    )
    settings["uncertain_invite"] = True
    invite = {"user_id": created["user_id"], "request_id": uuid4().hex}
    uncertain = write(interface, connection, "invite_identity_user", invite, tmp_path)
    assert uncertain["status"] == "needs_reconciliation" and uncertain["mail_delivery_verified"] is False
    count = len(calls)
    assert write(interface, connection, "invite_identity_user", invite, tmp_path) == uncertain
    assert len(calls) == count
    reconciled = write(
        interface,
        connection,
        "reconcile_identity_operation",
        {
            "operation_id": uncertain["id"],
            "request": {"outcome": "provider_accepted", "note": "Inspected exact invitation"},
        },
        tmp_path,
    )
    assert reconciled["invitation_status"] == "operator_attested_accepted"
    assert reconciled["provider_outcome_evidence"] == "operator_attestation"
    assert reconciled["mail_delivery_verified"] is False and len(calls) == count
    for request in [
        {"request_id": uuid4().hex, "username": "never-created", "password": "owned-private-admin-secret"},
        {"request_id": uuid4().hex, "username": "never-created", "provider_created": True},
        {"request_id": uuid4().hex, "username": "owned-private-admin-secret"},
    ]:
        with pytest.raises((ValueError, RuntimeError, UnexpectedToolError)) as failure:
            write(interface, connection, "provision_identity_user", {"request": request}, tmp_path)
        assert "owned-private-admin-secret" not in str(failure.value)
        assert "owned-private-admin-secret" not in str(failure.value.__cause__)
        assert len(calls) == count
