# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""SCM delegation clients preserve scope, explicit authority requests and exact recovery."""

import asyncio
import contextlib
import copy
import json

import httpx
import pytest
from click.testing import CliRunner

from firefly_forge import cli
from firefly_forge.client import ForgeClient
from firefly_forge.mcp_server import create_server

PROJECT, REPOSITORY, REQUEST, CURSOR = (character * 32 for character in "abcd")
BASE = f"projects/{PROJECT}/repositories/{REPOSITORY}/scm-actor-bindings"


def binding():
    return {
        "version": 1,
        "request_id": REQUEST,
        "expected_previous_digest": None,
        "verified_receipt_id": "e" * 32,
        "expected_verified_reference_digest": "f" * 64,
        "native_user_id": "1" * 32,
        "component_ids": ["api"],
        "allowed_actions": ["respond"],
        "active": True,
        "acknowledge_webhook_asserted_identity": True,
    }


@pytest.mark.parametrize("interface", ["sdk", "cli", "mcp"])
def test_clients_keep_exact_scope_request_and_explicit_get_recovery(monkeypatch, tmp_path, interface):
    calls = []
    body = binding()
    receipt = {"request": body, "digest": "2" * 64}

    def transport(request):
        calls.append((request.method, request.url.path, dict(request.url.params), request.content))
        if request.method == "POST" or "/requests/" in request.url.path:
            return httpx.Response(200, json=receipt)
        return httpx.Response(200, json={"version": 1, "items": [], "next_cursor": None})

    with ForgeClient("https://forge.invalid", "fictional-local-token") as connection:
        connection.http.close()
        connection.http = httpx.Client(
            base_url="https://forge.invalid/api/v1/", transport=httpx.MockTransport(transport)
        )
        monkeypatch.setattr(cli, "client", lambda: contextlib.nullcontext(connection))
        if interface == "sdk":
            candidates = connection.scm_actor_binding_candidates(PROJECT, REPOSITORY, CURSOR)
            saved = connection.configure_scm_actor_binding(PROJECT, REPOSITORY, body)
            exact = connection.scm_actor_binding(PROJECT, REPOSITORY, REQUEST)
            page = connection.scm_actor_bindings(PROJECT, REPOSITORY, CURSOR)
        elif interface == "cli":
            document = tmp_path / "delegation.json"
            document.write_text(json.dumps(body))

            def invoke(*args):
                result = CliRunner().invoke(cli.main, ["project", "scm-actors", *args])
                assert result.exit_code == 0, result.output
                return json.loads(result.output)

            candidates = invoke("candidates", PROJECT, REPOSITORY, "--cursor", CURSOR)
            saved = invoke("configure", PROJECT, REPOSITORY, str(document))
            exact = invoke("get", PROJECT, REPOSITORY, REQUEST)
            page = invoke("list", PROJECT, REPOSITORY, "--cursor", CURSOR)
        else:
            server = create_server()

            async def invoke(name, **args):
                result = await server.call_tool(name, {"project_id": PROJECT, "repository_id": REPOSITORY, **args})
                assert not result.is_error, str(result)
                return result.structured_content

            candidates = asyncio.run(invoke("list_scm_actor_binding_candidates", cursor=CURSOR))
            saved = asyncio.run(invoke("configure_scm_actor_binding", request=body))
            exact = asyncio.run(invoke("get_scm_actor_binding", request_id=REQUEST))
            page = asyncio.run(invoke("list_scm_actor_bindings", cursor=CURSOR))
        assert saved == exact == receipt
        assert candidates["items"] == page["items"] == []
        assert [(method, path, params) for method, path, params, _ in calls] == [
            ("GET", "/api/v1/" + BASE + "/candidates", {"cursor": CURSOR}),
            ("POST", "/api/v1/" + BASE, {}),
            ("GET", "/api/v1/" + BASE + "/requests/" + REQUEST, {}),
            ("GET", "/api/v1/" + BASE, {"cursor": CURSOR}),
        ]
        assert json.loads(calls[1][3]) == body


@pytest.mark.parametrize("outcome", ["lost", 403, 409, 422])
def test_sdk_does_not_retry_or_replace_an_uncertain_or_refused_write(outcome):
    calls = []

    def transport(request):
        calls.append(request)
        if outcome == "lost":
            raise httpx.ReadTimeout("Fictional lost delegation response")
        return httpx.Response(outcome, json={"detail": "Delegation not confirmed"})

    with ForgeClient("https://forge.invalid", "fictional-local-token") as connection:
        connection.http.close()
        connection.http = httpx.Client(
            base_url="https://forge.invalid/api/v1/", transport=httpx.MockTransport(transport)
        )
        with pytest.raises((httpx.ReadTimeout, RuntimeError)):
            connection.configure_scm_actor_binding(PROJECT, REPOSITORY, binding())
    assert len(calls) == 1 and calls[0].method == "POST"
    assert json.loads(calls[0].content)["request_id"] == REQUEST


@pytest.mark.parametrize(
    "change", [{"version": True}, {"active": 1}, {"native_user_id": "do-not-reflect-this"}, {"allowed_actions": ["*"]}]
)
def test_sdk_refuses_invalid_authority_input_without_dispatch_or_reflection(change, monkeypatch):
    calls = []
    body = {**binding(), **change}
    with ForgeClient("https://forge.invalid", "fictional-local-token") as connection:
        monkeypatch.setattr(connection, "request", lambda *a, **k: calls.append((a, k)))
        with pytest.raises(ValueError) as failure:
            connection.configure_scm_actor_binding(PROJECT, REPOSITORY, body)
        assert "do-not-reflect-this" not in str(failure.value)
    assert calls == []


def test_sdk_revalidates_a_constructed_model_before_dispatch(monkeypatch):
    from firefly_forge.scm_authority_models import SCMActorBindingInputV1

    calls = []
    request = SCMActorBindingInputV1.model_construct(**{**binding(), "native_user_id": "do-not-reflect-this"})
    with ForgeClient("https://forge.invalid", "fictional-local-token") as connection:
        monkeypatch.setattr(connection, "request", lambda *a, **k: calls.append((a, k)))
        with pytest.raises(ValueError) as failure:
            connection.configure_scm_actor_binding(PROJECT, REPOSITORY, request)
        assert "do-not-reflect-this" not in str(failure.value)
    assert calls == []


@pytest.mark.parametrize(
    "method,args",
    [
        ("scm_actor_binding_candidates", (PROJECT, "../other", None)),
        ("scm_actor_bindings", (True, REPOSITORY, None)),
        ("scm_actor_bindings", (PROJECT, REPOSITORY, "not-a-cursor")),
        ("scm_actor_binding", (PROJECT, REPOSITORY, "../request")),
    ],
)
def test_read_clients_refuse_invalid_route_or_cursor_before_transport(method, args, monkeypatch):
    calls = []
    with ForgeClient("https://forge.invalid", "fictional-local-token") as connection:
        monkeypatch.setattr(connection, "request", lambda *a, **k: calls.append((a, k)))
        with pytest.raises(ValueError):
            getattr(connection, method)(*args)
    assert calls == []


async def test_mcp_exposes_strict_nested_schema_and_safe_authority_annotations(monkeypatch):
    from jsonschema import Draft202012Validator
    from mcp.server.mcpserver.exceptions import UnexpectedToolError

    tools = {tool.name: tool for tool in await create_server().list_tools()}
    write = tools["configure_scm_actor_binding"]
    schema = write.input_schema
    Draft202012Validator.check_schema(schema)
    args = {"project_id": PROJECT, "repository_id": REPOSITORY, "request": binding()}
    Draft202012Validator(schema).validate(args)
    assert write.annotations.read_only_hint is False
    assert write.annotations.destructive_hint is True
    assert write.annotations.idempotent_hint is True
    assert write.annotations.open_world_hint is True
    for name in ["list_scm_actor_binding_candidates", "list_scm_actor_bindings", "get_scm_actor_binding"]:
        assert tools[name].annotations.read_only_hint is True
        assert tools[name].annotations.open_world_hint is False
    calls = []
    with ForgeClient("https://forge.invalid", "fictional-local-token") as connection:
        monkeypatch.setattr(connection, "request", lambda *a, **k: calls.append((a, k)))
        monkeypatch.setattr(cli, "client", lambda: contextlib.nullcontext(connection))
        invalid = copy.deepcopy(args)
        invalid["request"]["native_user_id"] = "do-not-reflect-this"
        with pytest.raises(UnexpectedToolError) as failure:
            await create_server().call_tool("configure_scm_actor_binding", invalid)
        assert "do-not-reflect-this" not in str(failure.value)
        assert "do-not-reflect-this" not in str(failure.value.__cause__)
    assert calls == []
