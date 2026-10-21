# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Actual MCP wire compatibility and packaged native description ownership."""

import ast
import hashlib
import json
from pathlib import Path

from mcp.types import ListToolsResult

from firefly_forge.mcp_server import create_server
from firefly_forge.prompts import prompt_catalog

NEW_TOOLS = {
    "get_run_activity_content",
    "configure_scm_polling_resume_grant",
    "get_scm_polling_resume_grant",
    "get_current_scm_polling_resume_grant",
    "review_scm_polling_resume",
    "request_run_creation",
    "get_run_creation_request",
    "cancel_run_creation_request",
    "get_run_creation_decision",
    "get_agent_checkpoint_candidates",
    "request_agent_checkpoint_restore",
    "get_agent_checkpoint_restore",
    "query_run_evidence",
    "configure_scm_resume_grant",
    "get_scm_resume_grant",
    "get_current_scm_resume_grant",
    "configure_scm_product_change_grant",
    "get_scm_product_change_grant",
    "get_current_scm_product_change_grant",
    "get_scm_product_change",
}


def _baseline():
    return json.loads((Path(__file__).parent / "fixtures/mcp-prompt-baseline-v1.json").read_text())


async def test_actual_mcp_wire_metadata_stays_byte_exact():
    tools = await create_server().list_tools()
    assert len(tools) == 162
    original = [tool for tool in tools if tool.name not in NEW_TOOLS]
    assert len(original) == 142
    actual = ListToolsResult(tools=original).model_dump_json(by_alias=True, exclude_none=True)
    baseline = _baseline()["wire_json"]
    # The eighth task and optional optimistic-concurrency seal are the only routing wire changes.
    expected = json.loads(baseline)
    routing = next(tool for tool in expected["tools"] if tool["name"] == "save_task_routing")
    bindings = routing["inputSchema"]["$defs"]["TaskRoutingInput"]["properties"]["bindings"]
    assert bindings["maxProperties"] == 7
    bindings["maxProperties"] = 8
    properties = routing["inputSchema"]["$defs"]["TaskRoutingInput"]["properties"]
    assert "expected_digest" not in properties
    properties["expected_digest"] = {
        "anyOf": [{"pattern": "^[a-f0-9]{64}$", "type": "string"}, {"type": "null"}],
        "default": None,
        "title": "Expected Digest",
    }
    assert actual == json.dumps(expected, ensure_ascii=False, separators=(",", ":"))


def test_all_mcp_descriptions_are_empty_variable_native_resources():
    tools = json.loads(_baseline()["wire_json"])["tools"]
    catalog = prompt_catalog()
    from importlib.resources import files

    manifest = json.loads(files("firefly_forge").joinpath("resources/prompts/v1/manifest.json").read_text())
    entries = {entry["id"]: entry for entry in manifest["templates"]}
    for tool in tools:
        assert entries["mcp/" + tool["name"]]["variables"] == {}
        assert catalog.text("mcp/" + tool["name"]) == tool["description"], tool["name"]
    assert len(catalog.registry) == 286
    assert entries["collaboration/decision-destination"]["variables"] == {"reference": "string"}
    assert entries["collaboration/decision-reply"]["variables"] == {"command": "string"}
    assert entries["collaboration/decision-resume"]["variables"] == {"command": "string"}
    assert entries["coding-runtime/run-configured-check"]["variables"] == {}
    assert entries["implementation/configured-checks"]["variables"] == {
        "checks": "string",
        "call_limit": "string",
        "final_check_count": "string",
    }


def test_mcp_callbacks_preserve_the_original_executable_ast():
    from firefly_forge import mcp_server

    tree = ast.parse(Path(mcp_server.__file__).read_text())
    server = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "create_server")
    callbacks = {}
    for node in server.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            node.decorator_list = []
            if ast.get_docstring(node, clean=False) is not None:
                node.body = node.body[1:]
            callbacks[node.name] = hashlib.sha256(ast.dump(node, include_attributes=False).encode()).hexdigest()
    assert {name: value for name, value in callbacks.items() if name not in NEW_TOOLS} == _baseline()[
        "callback_ast_sha256"
    ]
    assert set(callbacks) == set(_baseline()["callback_ast_sha256"]) | NEW_TOOLS


def test_mcp_descriptions_have_one_packaged_source():
    from firefly_forge import mcp_server

    tree = ast.parse(Path(mcp_server.__file__).read_text())
    server = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "create_server")
    names = set()
    for node in server.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            assert ast.get_docstring(node) is None, node.name
            tool = next(
                decorator
                for decorator in node.decorator_list
                if isinstance(decorator, ast.Call)
                and isinstance(decorator.func, ast.Attribute)
                and decorator.func.attr == "tool"
            )
            description = next(keyword.value for keyword in tool.keywords if keyword.arg == "description")
            assert ast.dump(description) == ast.dump(
                ast.Call(
                    func=ast.Name(id="prompt_text", ctx=ast.Load()),
                    args=[ast.Constant("mcp/" + node.name)],
                    keywords=[],
                )
            )
            names.add(node.name)
    assert names == {tool["name"] for tool in json.loads(_baseline()["wire_json"])["tools"]} | NEW_TOOLS
