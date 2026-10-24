# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""MCP registration and exact authenticated SDK reads for paged diffs."""

import base64
import hashlib
from contextlib import nullcontext

import pytest
from jsonschema import Draft202012Validator
from mcp.server.mcpserver.exceptions import UnexpectedToolError

from firefly_forge import cli
from firefly_forge.client import ForgeClient
from firefly_forge.mcp_server import create_server
from firefly_forge.prompts import prompt_catalog

RUN, REQUEST, MANIFEST = "1" * 32, "2" * 32, "a" * 64
NAMES = {"get_run_diff_index", "get_run_diff_file"}


def page(name):
    common = {"version": 1, "artifact_id": "3" * 32, "manifest_sha256": MANIFEST}
    if name.endswith("index"):
        return {**common, "page": 0, "next_page": 1, "files": []}
    data = b"first window\n"
    return {
        **common,
        "file_index": 0,
        "offset": 0,
        "byte_count": len(data),
        "total_bytes": 99,
        "next_offset": len(data),
        "content_base64": base64.b64encode(data).decode(),
        "content_sha256": hashlib.sha256(data).hexdigest(),
        "starts_mid_line": False,
        "ends_mid_line": False,
        "utf8_complete": True,
    }


async def test_paged_tools_have_packaged_descriptions_and_read_schemas():
    tools = {t.name: t for t in await create_server().list_tools()}
    assert tools.keys() >= NAMES
    for name in NAMES:
        t = tools[name]
        assert t.description == prompt_catalog().text("mcp/" + name)
        assert t.annotations.read_only_hint and t.annotations.idempotent_hint
        assert not t.annotations.destructive_hint
        args = {"run_id": RUN, "expected_manifest": MANIFEST}
        if name.endswith("file"):
            args["file_index"] = 0
        v = Draft202012Validator(t.input_schema)
        assert v.is_valid(args)
        assert v.is_valid({**args, "request_id": REQUEST})
        assert not v.is_valid({**args, "expected_manifest": "not-a-digest"})


@pytest.mark.parametrize("name", sorted(NAMES))
@pytest.mark.parametrize("request_id", [None, REQUEST])
@pytest.mark.parametrize("changed", [False, True])
async def test_one_exact_sdk_get_and_no_follow_or_retry(monkeypatch, name, request_id, changed):
    calls, value = [], page(name)
    if changed:
        value["manifest_sha256"] = "c" * 64
    with ForgeClient("https://forge.example", "owned-operator-token-32-characters") as conn:

        def request(*args, **kwargs):
            calls.append((args, kwargs))
            return value

        monkeypatch.setattr(conn, "request", request)
        monkeypatch.setattr(cli, "client", lambda: nullcontext(conn))
        args = {"run_id": RUN, "expected_manifest": MANIFEST, "request_id": request_id}
        params = {"expected_manifest": MANIFEST}
        path = f"runs/{RUN}"
        if request_id:
            path += f"/git-deliveries/requests/{REQUEST}"
        if name.endswith("file"):
            args["file_index"] = 0
            path += "/diff/files/0"
            params.update(offset=0, limit=32768)
        else:
            path += "/diff" + ("/index" if request_id is None else "")
            params["page"] = 0
        if changed:
            with pytest.raises(UnexpectedToolError):
                await create_server().call_tool(name, args)
        else:
            result = await create_server().call_tool(name, args)
            assert result.structured_content == value
            assert not result.is_error
        assert calls == [(("GET", path), {"params": params})]


@pytest.mark.parametrize(
    "name,extra",
    [
        ("get_run_diff_index", {"page": -1}),
        ("get_run_diff_index", {"page": True}),
        ("get_run_diff_index", {"page": "0"}),
        ("get_run_diff_index", {"request_id": "invalid"}),
        ("get_run_diff_file", {"file_index": -1}),
        ("get_run_diff_file", {"file_index": True}),
        ("get_run_diff_file", {"offset": -1}),
        ("get_run_diff_file", {"limit": 1048577}),
        ("get_run_diff_file", {"limit": True}),
        ("get_run_diff_file", {"limit": "32768"}),
    ],
)
async def test_bad_page_arguments_refused_before_http(monkeypatch, name, extra):
    calls = []
    with ForgeClient("https://forge.example", "owned-operator-token-32-characters") as conn:
        monkeypatch.setattr(conn, "request", lambda *a, **kw: calls.append((a, kw)))
        monkeypatch.setattr(cli, "client", lambda: nullcontext(conn))
        args = {"run_id": RUN, "expected_manifest": MANIFEST}
        if name.endswith("file"):
            args["file_index"] = 0
        with pytest.raises(UnexpectedToolError):
            await create_server().call_tool(name, {**args, **extra})
        assert calls == []


@pytest.mark.parametrize("request_id", [None, REQUEST])
async def test_initialized_mcp_session_preserves_typed_pages_and_authenticated_gets(monkeypatch, request_id):
    import httpx
    from mcp import Client

    calls = []

    def transport(request):
        calls.append(request)
        assert request.method == "GET" and not request.content
        assert request.headers["Authorization"] == "Bearer retained-caller-token"
        name = "get_run_diff_file" if "/files/" in request.url.path else "get_run_diff_index"
        return httpx.Response(200, json=page(name))

    with ForgeClient("https://factory.invalid", "retained-caller-token") as conn:
        conn.http.close()
        conn.http = httpx.Client(
            base_url="https://factory.invalid/api/v1/",
            headers={"Authorization": "Bearer retained-caller-token"},
            transport=httpx.MockTransport(transport),
        )
        monkeypatch.setattr(cli, "client", lambda: nullcontext(conn))
        async with Client(create_server(), cache=None, read_timeout_seconds=5) as session:
            tools = await session.list_tools()
            assert {tool.name for tool in tools.tools} >= NAMES
            for name in sorted(NAMES):
                args = {"run_id": RUN, "expected_manifest": MANIFEST, "request_id": request_id}
                if name.endswith("file"):
                    args["file_index"] = 0
                result = await session.call_tool(name, args)
                assert not result.is_error
                assert result.structured_content == page(name)
    prefix = f"/api/v1/runs/{RUN}"
    if request_id:
        prefix += f"/git-deliveries/requests/{REQUEST}"
    assert [request.url.path for request in calls] == [
        prefix + "/diff/files/0",
        prefix + "/diff" + ("/index" if request_id is None else ""),
    ]
    assert len(calls) == 2
