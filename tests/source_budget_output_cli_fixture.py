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
schema_marker = "Do not guess or claim completion."
schema = json.JSONDecoder().raw_decode(brief[brief.index(schema_marker) + len(schema_marker) :].lstrip())[0]
properties = schema["properties"]
amended = properties.get("protocol", {}).get("const") == "source-budget-continuation-v1"
attempt = properties["attempt"].get("const", 4)
assert attempt in (4, 5, 6)
marker = "Recorded operator response to the previous worker question"
start = brief.index("\n", brief.index(marker))
retained, _ = json.JSONDecoder().raw_decode(brief[start:].lstrip())
assert retained["kind"] == "historical_worker_question_answers" and retained["version"] == 1
assert retained["historical_only"] is True and retained["grants_authority"] is False
assert retained["pairs"] and retained["current_response_digest"] == retained["pairs"][-1]["response"]["digest"]
assert all(pair["feedback"]["sequence"] < pair["response"]["sequence"] for pair in retained["pairs"])
if amended:
    assert len(retained["pairs"]) >= 2
    assert any(pair["request_id"] == "native-read-budget-question" for pair in retained["pairs"])
assert (
    ((('"used": 13' in brief) if attempt == 5 else ('"used": 24' in brief)) and '"limit": 20' in brief)
    if amended
    else ('"used": 0' in brief and '"limit": 10' in brief)
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

    if attempt == 5:
        for number in range(9):
            assert call("read_file", {"path": "partial.py"}, number + 10)["content"] == "VALUE = 1\n"
        assert call("list_files", {}, 21)["files"]
        assert call("search_text", {"path": "partial.py", "text": "VALUE", "max_results": 1}, 22)
        read_refusal = total_refusal = None
    elif attempt == 6:
        assert call("read_file", {"path": "partial.py"}, 10)["content"] == "VALUE = 1\n"
        read_refusal = call("read_file", {"path": "partial.py"}, 11)
        assert read_refusal["code"] == "tool_budget_exhausted" and read_refusal["effect_started"] is False
        assert read_refusal["budget"]["tools"]["read_file"] == {"used": 20, "limit": 20, "remaining": 0}
        for number in range(5):
            assert call("list_files", {}, number + 20)["files"]
        total_refusal = call("list_files", {}, 30)
        assert total_refusal["code"] == "tool_budget_exhausted" and total_refusal["effect_started"] is False
        assert total_refusal["budget"]["total"] == {"used": 30, "limit": 30, "remaining": 0}
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
if attempt == 5:
    pathlib.Path("docs").mkdir(exist_ok=True)
    pathlib.Path("docs/CONTINUATION_STATUS.md").write_text("Retained controlled failed output; no repair claimed.\n")
    summary = json.dumps(
        {
            "summary": "Controlled output failed the feedback contract",
            "feedback": {
                "id": "invalid-five",
                "role_id": "implementer",
                "task_id": "implement",
                "attempt": 5,
                "protocol": properties["protocol"]["const"],
                "allowance_id": properties["allowance_id"]["const"],
                "allowance_digest": properties["allowance_digest"]["const"],
                "disposition": "blocked",
                "summary": "A question was not authored",
                "request": None,
            },
        }
    )
elif attempt == 6:
    pathlib.Path("partial.py").write_text("VALUE = 2\n")
    summary = json.dumps(
        {
            "summary": "Controlled explicit output activation reached final checks",
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
    {"type": "thread.started", "thread_id": "known-output-fixture-" + str(attempt)},
    {"type": "turn.started"},
    {"type": "item.completed", "item": {"id": "final", "type": "agent_message", "text": summary}},
    {"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}},
]:
    print(json.dumps(event))
