# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Lifetime read limits are explicit definite refusals, never unknown tool effects."""

import time
from types import SimpleNamespace

import httpx
import pytest

from firefly_forge import tool_broker
from firefly_forge.coding_check_transport import CodingCheckServer


def rows():
    return [
        SimpleNamespace(tool=name, status="succeeded")
        for name in ["read_file"] * 10 + ["list_files"] * 2 + ["search_text"]
    ]


def test_budget_snapshot_keeps_all_lifetime_rows_and_remaining_readers():
    from firefly_forge.source_tool_budget import source_budget_snapshot

    before = rows()
    state = source_budget_snapshot(before, {"calls_limit": 30, "per_tool_limit": 10})
    assert state["scope"] == "run_lifetime" and state["total"] == {"used": 13, "limit": 30, "remaining": 17}
    assert state["tools"]["read_file"] == {"used": 10, "limit": 10, "remaining": 0}
    assert state["tools"]["list_files"]["remaining"] == 8
    assert state["tools"]["search_text"]["remaining"] == 9
    assert len(before) == 13 and all(r.status == "succeeded" for r in before)


async def test_real_scoped_mcp_delivers_definite_refusal_with_no_secret_or_retry():
    state = {
        "version": 1,
        "scope": "run_lifetime",
        "total": {"used": 13, "limit": 30, "remaining": 17},
        "tools": {
            "read_file": {"used": 10, "limit": 10, "remaining": 0},
            "list_files": {"used": 2, "limit": 10, "remaining": 8},
            "search_text": {"used": 1, "limit": 10, "remaining": 9},
        },
    }
    calls = []

    async def callback(request):
        calls.append(request)
        cls = getattr(tool_broker, "ToolBudgetRefusal", None)
        if cls is None:
            raise ValueError("The frozen run tool-call budget is exhausted")
        raise cls("read_file", state)

    server = CodingCheckServer(None, source_callback=callback, expires_at_ms=int(time.time() * 1000) + 10000)
    app = server.application(19433)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://127.0.0.1:19433") as client,
    ):
        r = await client.post(
            "/mcp",
            headers={
                "authorization": "Bearer " + server.token,
                "accept": "application/json, text/event-stream",
                "mcp-protocol-version": "2025-11-25",
            },
            json={
                "jsonrpc": "2.0",
                "id": 71,
                "method": "tools/call",
                "params": {"name": "read_file", "arguments": {"request_id": "a" * 32, "path": "public.py"}},
            },
        )
    assert r.status_code == 200 and len(calls) == 1
    result = r.json()["result"]
    assert not result.get("isError"), result
    body = result["structuredContent"]
    assert body["status"] == "refused" and body["code"] == "tool_budget_exhausted"
    assert body["effect_started"] is False and body["retryable"] is False
    assert body["budget"] == state and body["requested_tool"] == "read_file"
    assert "public.py" not in str(body) and "original native receipt" not in str(body)


@pytest.mark.parametrize("change", ["remaining", "missing", "not_exhausted", "bool"])
def test_typed_refusal_rejects_inconsistent_or_incomplete_safe_facts(change):
    from firefly_forge.source_tool_budget import ToolBudgetRefusal, source_budget_snapshot

    state = source_budget_snapshot(rows(), {"calls_limit": 30, "per_tool_limit": 10})
    if change == "remaining":
        state["total"]["remaining"] = 30
    if change == "missing":
        del state["tools"]["list_files"]
    if change == "not_exhausted":
        state["tools"]["read_file"] = {"used": 9, "limit": 10, "remaining": 1}
    if change == "bool":
        state["tools"]["read_file"]["used"] = True
    with pytest.raises(ValueError):
        ToolBudgetRefusal("read_file", state)
