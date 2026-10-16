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
    assert len(tools) == 150
    original = [tool for tool in tools if tool.name not in NEW_TOOLS]
    assert len(original) == 142
    assert ListToolsResult(tools=original).model_dump_json(by_alias=True, exclude_none=True) == _baseline()["wire_json"]


def test_all_mcp_descriptions_are_empty_variable_native_resources():
    tools = json.loads(_baseline()["wire_json"])["tools"]
    catalog = prompt_catalog()
    from importlib.resources import files

    manifest = json.loads(files("firefly_forge").joinpath("resources/prompts/v1/manifest.json").read_text())
    entries = {entry["id"]: entry for entry in manifest["templates"]}
    for tool in tools:
        assert entries["mcp/" + tool["name"]]["variables"] == {}
        assert catalog.text("mcp/" + tool["name"]) == tool["description"], tool["name"]
    assert len(catalog.registry) == 228


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
