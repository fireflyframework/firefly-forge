# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Actual MCP wire compatibility and packaged native description ownership."""

import ast
import hashlib
import json
from pathlib import Path

from mcp.types import ListToolsResult

from firefly_forge.mcp_server import create_server
from firefly_forge.prompts import prompt_catalog

KOINON_TOOLS = {
    "ingest_koinon_source",
    "upload_koinon_document",
    "get_koinon_ingestion",
    "observe_koinon_ingestion",
    "get_knowledge_upload",
    "reconcile_knowledge_upload",
    "get_knowledge_configuration",
    "configure_koinon_knowledge",
    "check_koinon_knowledge",
    "get_project_knowledge_schema",
    "read_project_knowledge_chunks",
    "execute_project_knowledge_sql",
    "get_project_knowledge_sql_request",
}
KNOWLEDGE_DESCRIPTION_CHANGES = {
    "search_project_knowledge": (
        "Retrieve cited project/framework references in the repository's flycanon scopes.",
        "Retrieve cited references through the repository’s configured knowledge platform and current mapped scopes.",
    ),
    "remember_verified_run": (
        "Save verified run evidence as cited, retrievable project memory.",
        "Retain verified run evidence through its configured knowledge workflow; "
        "Koinon stages an external proposal for explicit private review.",
    ),
    "submit_learning_projection": (
        "Submit a verified local outcome once; unknown effects cannot be automatically replayed.",
        "Submit retained local outcome evidence once through its frozen provider; "
        "Koinon records positive outcomes and unclassified failure hints as versioned source evidence. "
        "Unknown writes require readback without replay.",
    ),
    "observe_learning_projection": (
        "Verify admitted source content and retrievable citations without creating another source.",
        "Read back the admitted source and scoped citations without another upload; "
        "native operational evidence also requires exact source versions and extraction identities.",
    ),
}
NEW_TOOLS = KOINON_TOOLS | {
    "get_framework_knowledge_status",
    "check_framework_updates",
    "refresh_framework_knowledge",
    "get_framework_knowledge_refresh",
    "observe_framework_knowledge_refresh",
    "list_framework_corpora",
    "list_framework_corpus_sources",
    "search_framework_knowledge",
    "get_run_diff_index",
    "get_run_diff_file",
    "get_git_delivery_policy",
    "configure_git_delivery_policy",
    "get_git_deliveries",
    "request_git_delivery",
    "get_git_delivery_request",
    "get_git_delivery_observations",
    "configure_scm_polling_functional_grant",
    "get_scm_polling_functional_grant",
    "get_current_scm_polling_functional_grant",
    "review_scm_polling_functional",
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
    "get_final_outcome_candidate",
    "request_final_outcome_continuation",
    "get_final_outcome_status",
}


def _baseline():
    return json.loads((Path(__file__).parent / "fixtures/mcp-prompt-baseline-v1.json").read_text())


async def test_original_mcp_wire_metadata_keeps_reviewed_configuration_extensions():
    tools = await create_server().list_tools()
    assert len(tools) == 198
    original = [tool for tool in tools if tool.name not in NEW_TOOLS]
    assert len(original) == 142
    actual = ListToolsResult(tools=original).model_dump_json(by_alias=True, exclude_none=True)
    baseline = _baseline()["wire_json"]
    expected = json.loads(baseline)
    routing = next(tool for tool in expected["tools"] if tool["name"] == "save_task_routing")
    bindings = routing["inputSchema"]["$defs"]["TaskRoutingInput"]["properties"]["bindings"]
    assert bindings["maxProperties"] == 7
    bindings["maxProperties"] = 8
    launch = next(tool for tool in expected["tools"] if tool["name"] == "launch_project_campaign")
    selection = launch["inputSchema"]["$defs"]["CampaignLaunch"]["properties"]["component_ids"]["anyOf"][0]
    assert selection.pop("maxItems") == 50
    retry = next(tool for tool in expected["tools"] if tool["name"] == "retry_project_campaign")
    selection = retry["inputSchema"]["$defs"]["CampaignRetry"]["properties"]["component_ids"]
    assert selection.pop("maxItems") == 50
    from firefly_forge.sandbox_models import SandboxInput
    from firefly_forge.work_duration_models import WorkDurationPolicyV2

    runner = SandboxInput.model_json_schema()
    additions = {"process_monitor", "monitor_owner_grace_seconds", "monitor_timeout_seconds", "work_duration"}
    for name in ("save_repository_runner", "probe_runner"):
        tool = next(item for item in expected["tools"] if item["name"] == name)
        definitions = tool["inputSchema"]["$defs"]
        properties = definitions["SandboxInput"]["properties"]
        assert runner["properties"].keys() - properties.keys() == additions
        properties.update({field: runner["properties"][field] for field in additions})
        definitions["WorkDurationPolicyV2"] = WorkDurationPolicyV2.model_json_schema()
    for name, (retained, current) in KNOWLEDGE_DESCRIPTION_CHANGES.items():
        tool = next(item for item in expected["tools"] if item["name"] == name)
        assert tool["description"] == retained
        tool["description"] = current
    assert json.loads(actual) == expected


def test_all_mcp_descriptions_are_empty_variable_native_resources():
    tools = json.loads(_baseline()["wire_json"])["tools"]
    catalog = prompt_catalog()
    from importlib.resources import files

    manifest = json.loads(files("firefly_forge").joinpath("resources/prompts/v1/manifest.json").read_text())
    entries = {entry["id"]: entry for entry in manifest["templates"]}
    for tool in tools:
        assert entries["mcp/" + tool["name"]]["variables"] == {}
        assert catalog.text("mcp/" + tool["name"]) == tool["description"], tool["name"]
    assert len(catalog.registry) == len(manifest["templates"])
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
                    keywords=[ast.keyword(arg="version", value=ast.Constant("1.0.1"))]
                    if node.name
                    in {
                        "search_project_knowledge",
                        "remember_verified_run",
                        "submit_learning_projection",
                        "observe_learning_projection",
                    }
                    else [],
                )
            )
            names.add(node.name)
    assert names == {tool["name"] for tool in json.loads(_baseline()["wire_json"])["tools"]} | NEW_TOOLS
