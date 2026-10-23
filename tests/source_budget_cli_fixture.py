# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Controlled child CLI fixture: real scoped MCP calls, no paid model/account."""

import json
import os
import pathlib
import tomllib
import uuid

import httpx

args = __import__("sys").argv[1:]
brief = __import__("sys").stdin.read()
amended = '"source-budget-continuation-v1"' in brief
assert (
    ('"used": 13' in brief and '"limit": 20' in brief) if amended else ('"used": 0' in brief and '"limit": 10' in brief)
)
assert "tool_budget_exhausted" in brief and "in-flight" in brief
assert "--strict-config" in args and "--sandbox" not in args
config = tomllib.loads("\n".join(args[n + 1] for n, arg in enumerate(args) if arg == "-c"))
assert config["agents"]["enabled"] is False
server = config["mcp_servers"]["forge_coding"]
assert server["enabled_tools"] == ["list_files", "read_file", "search_text"]
assert server["omit_tools_from"] == ["deferred", "code_mode"]
headers = {
    "Authorization": "Bearer " + os.environ[server["bearer_token_env_var"]],
    "Accept": "application/json, text/event-stream",
    "MCP-Protocol-Version": "2025-11-25",
}
with httpx.Client(timeout=10, trust_env=False) as client:

    def rpc(packet):
        response = client.post(server["url"], headers=headers, json=packet)
        if "mcp-session-id" in response.headers:
            headers["MCP-Session-Id"] = response.headers["mcp-session-id"]
        return response

    def result(response):
        assert response.status_code == 200
        text = response.text
        packet = (
            json.loads(next(line[5:].strip() for line in text.splitlines() if line.startswith("data:")))
            if text.startswith("event:")
            else response.json()
        )
        assert "error" not in packet, "Unexpected scoped transport error"
        return packet["result"]

    result(
        rpc(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "public-source-recovery-fixture", "version": "1"},
                },
            }
        )
    )
    assert rpc({"jsonrpc": "2.0", "method": "notifications/initialized"}).status_code in (200, 202)
    assert sorted(
        t["name"] for t in result(rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}))["tools"]
    ) == ["list_files", "read_file", "search_text"]

    def call(tool, params, number):
        packet = result(
            rpc(
                {
                    "jsonrpc": "2.0",
                    "id": number,
                    "method": "tools/call",
                    "params": {"name": tool, "arguments": {"request_id": uuid.uuid4().hex, **params}},
                }
            )
        )
        assert packet.get("isError") is not True
        return packet.get("structuredContent") or json.loads(packet["content"][0]["text"])

    if amended:
        for number in range(10):
            value = call("read_file", {"path": "partial.py"}, number + 10)
            assert value["content"] == "VALUE = 1\n"
        refusal = call("read_file", {"path": "partial.py"}, 20)
        assert refusal["code"] == "tool_budget_exhausted" and refusal["effect_started"] is False
        assert refusal["budget"]["total"] == {"used": 23, "limit": 30, "remaining": 7}
        assert refusal["budget"]["tools"]["read_file"] == {"used": 20, "limit": 20, "remaining": 0}
        read_refusal = refusal
        assert call("list_files", {}, 21)["files"]
        assert call("search_text", {"path": "partial.py", "text": "VALUE", "max_results": 1}, 22)
        for number in range(5):
            assert call("list_files", {}, number + 30)["files"]
        refusal = call("list_files", {}, 40)
        assert refusal["code"] == "tool_budget_exhausted" and refusal["effect_started"] is False
        assert refusal["budget"]["total"] == {"used": 30, "limit": 30, "remaining": 0}
        assert refusal["budget"]["tools"]["list_files"]["remaining"] == 2
        total_refusal = refusal
    else:
        for number in range(10):
            assert call("read_file", {"path": "partial.py"}, number + 10)["content"] == "VALUE = 1\n"
        for number in range(2):
            assert call("list_files", {}, number + 21)["files"]
        assert call("search_text", {"path": "partial.py", "text": "VALUE", "max_results": 1}, 23)
        refusal = call("read_file", {"path": "partial.py"}, 24)
        assert refusal["code"] == "tool_budget_exhausted" and refusal["effect_started"] is False
        assert refusal["budget"]["total"] == {"used": 13, "limit": 30, "remaining": 17}
        assert refusal["budget"]["tools"]["read_file"] == {"used": 10, "limit": 10, "remaining": 0}
        read_refusal, total_refusal = refusal, None
    assert (
        rpc(
            {
                "jsonrpc": "2.0",
                "id": 6,
                "method": "tools/call",
                "params": {
                    "name": "run_configured_check",
                    "arguments": {"command_index": 0, "request_id": uuid.uuid4().hex},
                },
            }
        ).status_code
        == 400
    )
if amended:
    pathlib.Path("partial.py").write_text("VALUE = 2\n")
    summary = json.dumps(
        {
            "summary": "Changed public source through the exact amended source-only channel",
            "source_budget_observation": {"read_refusal": read_refusal, "shared_refusal": total_refusal},
        }
    )
else:
    summary = json.dumps(
        {
            "summary": "The original lifetime read_file ceiling was reached without a tool effect",
            "source_budget_observation": {"read_refusal": read_refusal, "shared_refusal": total_refusal},
            "feedback": {
                "id": "native-budget-held",
                "role_id": "implementer",
                "task_id": "implement",
                "attempt": 4,
                "disposition": "clarification",
                "summary": "The original lifetime read_file ceiling was reached without a tool effect",
                "request": {
                    "id": "native-read-budget-question",
                    "kind": "other",
                    "question": "May the native operator review an explicit finite read allowance?",
                    "context": "Thirteen immutable tool receipts and the original final-seven command reserve remain.",
                },
            },
        }
    )
for event in [
    {"type": "thread.started", "thread_id": "source-budget-fixture-5" if amended else "source-budget-fixture-4"},
    {"type": "turn.started"},
    {"type": "item.completed", "item": {"id": "final", "type": "agent_message", "text": summary}},
    {"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}},
]:
    print(json.dumps(event))
