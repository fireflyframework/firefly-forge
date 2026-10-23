# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Actual MCP registration, shared schemas and deferred SDK validation for Git delivery."""

from contextlib import nullcontext

import pytest
from jsonschema import Draft202012Validator

from firefly_forge import cli
from firefly_forge.client import ForgeClient
from firefly_forge.mcp_server import create_server
from firefly_forge.prompts import prompt_catalog

NAMES = {
    "get_git_delivery_policy",
    "configure_git_delivery_policy",
    "get_git_deliveries",
    "request_git_delivery",
    "get_git_delivery_request",
    "get_git_delivery_observations",
}
PROJECT, RUN, REQUEST = "1" * 32, "2" * 32, "3" * 32
POLICY = {
    "version": 1,
    "request_id": REQUEST,
    "expected_previous_digest": None,
    "policy": {
        "enabled": False,
        "every_units": 1,
        "on_pause": True,
        "remote": "local",
        "max_checkpoints": 50,
        "version": 1,
    },
}
CHECKPOINT = {"version": 1, "request_id": REQUEST, "expected_head": "a" * 40, "expected_source_tree": "b" * 40}


async def test_git_delivery_mcp_descriptions_schemas_and_read_annotations():
    tools = {item.name: item for item in await create_server().list_tools()}
    assert tools.keys() >= NAMES
    for name in NAMES:
        tool = tools[name]
        assert tool.description == prompt_catalog().text("mcp/" + name)
        assert tool.annotations.read_only_hint is (name.startswith("get_"))
    for name, body, scope in [
        ("configure_git_delivery_policy", POLICY, {"project_id": PROJECT}),
        ("request_git_delivery", CHECKPOINT, {"run_id": RUN}),
    ]:
        validator = Draft202012Validator(tools[name].input_schema)
        assert validator.is_valid({**scope, "request": body})
        assert not validator.is_valid({**scope, "request": {**body, "unreviewed_option": True}})
        assert not validator.is_valid({**scope, "request": {**body, "version": 2}})


async def test_all_six_operator_tools_delegate_once_to_the_authenticated_sdk(monkeypatch):
    calls = []

    class Connection:
        def __getattr__(self, name):
            def invoke(*args):
                calls.append((name, args))
                return {"observed": name}

            return invoke

    monkeypatch.setattr(cli, "client", lambda: nullcontext(Connection()))
    server = create_server()
    cases = [
        ("get_git_delivery_policy", {"project_id": PROJECT}, "git_delivery_policy", (PROJECT,)),
        (
            "configure_git_delivery_policy",
            {"project_id": PROJECT, "request": POLICY},
            "configure_git_deliveries",
            (PROJECT, POLICY),
        ),
        ("get_git_deliveries", {"run_id": RUN}, "git_deliveries", (RUN,)),
        ("request_git_delivery", {"run_id": RUN, "request": CHECKPOINT}, "request_git_delivery", (RUN, CHECKPOINT)),
        ("get_git_delivery_request", {"run_id": RUN, "request_id": REQUEST}, "git_delivery_request", (RUN, REQUEST)),
        (
            "get_git_delivery_observations",
            {"run_id": RUN, "request_id": REQUEST},
            "git_delivery_observations",
            (RUN, REQUEST),
        ),
    ]
    for name, arguments, method, expected in cases:
        await server.call_tool(name, arguments)
        assert calls[-1] == (method, expected)
    assert len(calls) == 6


@pytest.mark.parametrize(
    "name,arguments",
    [
        (
            "configure_git_delivery_policy",
            {"project_id": PROJECT, "request": {**POLICY, "access_token": "private-input-never-echo"}},
        ),
        (
            "request_git_delivery",
            {"run_id": RUN, "request": {**CHECKPOINT, "access_token": "private-input-never-echo"}},
        ),
    ],
)
async def test_mcp_write_validation_refuses_credential_fields_before_http(monkeypatch, name, arguments):
    from mcp.server.mcpserver.exceptions import UnexpectedToolError

    with ForgeClient("https://forge.example", "owned-operator-token-32-characters") as connection:
        calls = []
        monkeypatch.setattr(connection, "request", lambda *a, **kw: calls.append((a, kw)))
        monkeypatch.setattr(cli, "client", lambda: nullcontext(connection))
        with pytest.raises(UnexpectedToolError) as caught:
            await create_server().call_tool(name, arguments)
        assert name in str(caught.value)
        assert "private-input-never-echo" not in str(caught.value)
        assert calls == []


@pytest.mark.parametrize(
    "name,arguments",
    [
        ("get_git_delivery_policy", {"project_id": "../private-scope-canary"}),
        ("configure_git_delivery_policy", {"project_id": "../private-scope-canary", "request": POLICY}),
        ("get_git_deliveries", {"run_id": "../private-scope-canary"}),
        ("request_git_delivery", {"run_id": "../private-scope-canary", "request": CHECKPOINT}),
        ("get_git_delivery_request", {"run_id": RUN, "request_id": "../private-scope-canary"}),
        ("get_git_delivery_observations", {"run_id": RUN, "request_id": "../private-scope-canary"}),
    ],
)
async def test_every_git_operator_tool_refuses_bad_scope_before_http(monkeypatch, name, arguments):
    from mcp.server.mcpserver.exceptions import UnexpectedToolError

    with ForgeClient("https://forge.example", "owned-operator-token-32-characters") as connection:
        calls = []
        monkeypatch.setattr(connection, "request", lambda *a, **kw: calls.append((a, kw)))
        monkeypatch.setattr(cli, "client", lambda: nullcontext(connection))
        with pytest.raises(UnexpectedToolError) as caught:
            await create_server().call_tool(name, arguments)
        assert "private-scope-canary" not in str(caught.value)
        assert calls == []
