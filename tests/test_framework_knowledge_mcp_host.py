# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Native MCP tools use shared framework schemas and the authenticated SDK."""

from contextlib import nullcontext

import pytest
from jsonschema import Draft202012Validator

from firefly_forge import cli
from firefly_forge.client import ForgeClient
from firefly_forge.mcp_server import create_server
from firefly_forge.prompts import prompt_text

REQUEST, CORPUS = "a" * 32, "b" * 32
REFRESH = {
    "version": 1,
    "discovery_digest": "c" * 64,
    "expected_status_revision": "d" * 64,
    "policy_digest": "e" * 64,
    "make_current": True,
}
READS = {
    "get_framework_knowledge_status",
    "get_framework_knowledge_refresh",
    "list_framework_corpora",
    "list_framework_corpus_sources",
    "search_framework_knowledge",
}
WRITES = {"check_framework_updates", "refresh_framework_knowledge", "observe_framework_knowledge_refresh"}


async def test_framework_tools_register_shared_schemas_and_accurate_effect_annotations():
    tools = {item.name: item for item in await create_server().list_tools()}
    assert tools.keys() >= READS | WRITES
    for name in READS | WRITES:
        assert tools[name].description == prompt_text("mcp/" + name)
        assert tools[name].annotations.read_only_hint is (name in READS)
    for name, body in [
        ("check_framework_updates", {"version": 1, "framework_version": "26.10.11"}),
        ("refresh_framework_knowledge", REFRESH),
        ("search_framework_knowledge", {"query": "DI example", "corpus_ref": CORPUS}),
    ]:
        values = {"framework": "pyfly", "request": body}
        if name == "refresh_framework_knowledge":
            values["request_id"] = REQUEST
        validator = Draft202012Validator(tools[name].input_schema)
        assert validator.is_valid(values)
        assert not validator.is_valid({**values, "request": {**body, "api_key": "private-input"}})
        assert not validator.is_valid({**values, "framework": "other-provider"})


async def test_eight_tools_delegate_once_with_retained_identifiers_and_paging(monkeypatch):
    calls = []

    class Connection:
        def __getattr__(self, name):
            def invoke(*args, **kwargs):
                calls.append((name, args, kwargs))
                return {"observed": name}

            return invoke

    monkeypatch.setattr(cli, "client", lambda: nullcontext(Connection()))
    server = create_server()
    cases = [
        ("get_framework_knowledge_status", {}, "framework_knowledge_status", (), {}),
        (
            "check_framework_updates",
            {"framework": "pyfly", "request": {}},
            "check_framework_updates",
            ("pyfly", {}),
            {},
        ),
        (
            "refresh_framework_knowledge",
            {"framework": "pyfly", "request_id": REQUEST, "request": REFRESH},
            "refresh_framework_knowledge",
            ("pyfly", REQUEST, REFRESH),
            {},
        ),
        (
            "get_framework_knowledge_refresh",
            {"framework": "pyfly", "request_id": REQUEST},
            "framework_knowledge_refresh",
            ("pyfly", REQUEST),
            {},
        ),
        (
            "observe_framework_knowledge_refresh",
            {"framework": "pyfly", "request_id": REQUEST},
            "observe_framework_knowledge_refresh",
            ("pyfly", REQUEST),
            {},
        ),
        (
            "list_framework_corpora",
            {"framework": "pyfly", "cursor": "page2", "page_size": 29},
            "framework_corpora",
            ("pyfly",),
            {"cursor": "page2", "page_size": 29},
        ),
        (
            "list_framework_corpus_sources",
            {"framework": "larafly", "corpus_ref": CORPUS},
            "framework_corpus_sources",
            ("larafly", CORPUS),
            {"cursor": None, "page_size": 50},
        ),
        (
            "search_framework_knowledge",
            {"framework": "larafly", "request": {"query": "DI", "corpus_ref": CORPUS}},
            "search_framework_knowledge",
            ("larafly", {"query": "DI", "corpus_ref": CORPUS}),
            {},
        ),
    ]
    for name, arguments, method, args, kwargs in cases:
        await server.call_tool(name, arguments)
        assert calls[-1] == (method, args, kwargs)
    assert len(calls) == 8


@pytest.mark.parametrize(
    "name,arguments",
    [
        (
            "refresh_framework_knowledge",
            {"framework": "pyfly", "request_id": REQUEST, "request": {**REFRESH, "token": "private-canary-never-echo"}},
        ),
        ("search_framework_knowledge", {"framework": "pyfly", "request": {"query": "DI"}}),
        ("get_framework_knowledge_refresh", {"framework": "../private-canary-never-echo", "request_id": REQUEST}),
        ("list_framework_corpus_sources", {"framework": "pyfly", "corpus_ref": "../private-canary-never-echo"}),
        ("list_framework_corpora", {"framework": "pyfly", "page_size": True}),
    ],
)
async def test_invalid_mcp_inputs_refuse_before_http_and_do_not_echo_sensitive_values(monkeypatch, name, arguments):
    from mcp.server.mcpserver.exceptions import UnexpectedToolError

    with ForgeClient("https://forge.example", "fixture-operator-token") as connection:
        calls = []
        monkeypatch.setattr(connection, "request", lambda *args, **kwargs: calls.append((args, kwargs)))
        monkeypatch.setattr(cli, "client", lambda: nullcontext(connection))
        with pytest.raises(UnexpectedToolError) as caught:
            await create_server().call_tool(name, arguments)
        assert "private-canary-never-echo" not in str(caught.value)
        assert calls == []
