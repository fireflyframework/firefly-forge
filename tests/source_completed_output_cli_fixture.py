# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Controlled child CLI fixture: real scoped MCP calls, no paid model/account."""

import json
import os
import pathlib
import tomllib
import uuid

import httpx

args = __import__("sys").argv[1:]
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
    for number, (tool, params) in enumerate(
        [
            ("list_files", {}),
            ("read_file", {"path": "partial.py"}),
            ("search_text", {"path": "partial.py", "text": "VALUE", "max_results": 1}),
        ],
        3,
    ):
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
        data = packet.get("structuredContent") or json.loads(packet["content"][0]["text"])
        assert data
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
pathlib.Path("partial.py").write_text("VALUE = 2\n")
for event in [
    {"type": "thread.started", "thread_id": "source-recovery-fixture"},
    {"type": "turn.started"},
    {
        "type": "item.completed",
        "item": {
            "id": "final",
            "type": "agent_message",
            "text": "Changed public source through the retained source-only channel",
        },
    },
    {"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}},
]:
    print(json.dumps(event))
