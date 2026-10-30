# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Explicit operator adapters preserve the SDK wire contract and caller choices."""

import ast
import copy
import hashlib
import json
from pathlib import Path

import httpx
import pytest
from click.testing import CliRunner
from jsonschema import Draft202012Validator
from mcp import Client
from mcp.server.mcpserver.exceptions import UnexpectedToolError
from mcp.types import ListToolsResult
from test_mcp_prompt_templates import KNOWLEDGE_DESCRIPTION_CHANGES, KOINON_TOOLS
from test_original_recovery_sdk import (
    CANCEL,
    CANCELLED_DECISION,
    COMPLETION,
    DECISION,
    ENVELOPE,
    RECEIPT,
    REQUEST,
    RESTORE,
    RUN,
    TICKET,
    connection,
)

from firefly_forge.cli import main
from firefly_forge.mcp_server import create_server
from firefly_forge.prompts import prompt_catalog

CASES = [
    (
        "request-run-creation",
        "request_run_creation",
        [REQUEST],
        {"request_id": REQUEST},
        ENVELOPE,
        "POST",
        f"/api/v1/run-creation-requests/{REQUEST}",
        RECEIPT,
    ),
    (
        "run-creation-request",
        "get_run_creation_request",
        [REQUEST],
        {"request_id": REQUEST},
        None,
        "GET",
        f"/api/v1/run-creation-requests/{REQUEST}",
        RECEIPT,
    ),
    (
        "cancel-run-creation-request",
        "cancel_run_creation_request",
        [REQUEST],
        {"request_id": REQUEST},
        CANCEL,
        "POST",
        f"/api/v1/run-creation-requests/{REQUEST}/cancel",
        DECISION,
    ),
    (
        "run-creation-decision",
        "get_run_creation_decision",
        [REQUEST],
        {"request_id": REQUEST},
        None,
        "GET",
        f"/api/v1/run-creation-requests/{REQUEST}/decision",
        DECISION,
    ),
    (
        "agent-checkpoint-candidates",
        "get_agent_checkpoint_candidates",
        [RUN],
        {"identifier": RUN},
        None,
        "GET",
        f"/api/v1/runs/{RUN}/agent-checkpoints/candidates",
        {"candidates": [], "truncated": False},
    ),
    (
        "request-agent-checkpoint-restore",
        "request_agent_checkpoint_restore",
        [RUN, COMPLETION],
        {"identifier": RUN, "completion_id": COMPLETION},
        RESTORE,
        "POST",
        f"/api/v1/runs/{RUN}/agent-checkpoints/{COMPLETION}/restores",
        TICKET,
    ),
    (
        "agent-checkpoint-restore",
        "get_agent_checkpoint_restore",
        [RUN, COMPLETION, REQUEST],
        {"identifier": RUN, "completion_id": COMPLETION, "request_id": REQUEST},
        None,
        "GET",
        f"/api/v1/runs/{RUN}/agent-checkpoints/{COMPLETION}/restores/{REQUEST}",
        TICKET,
    ),
]
NEW_TOOLS = {case[1] for case in CASES} | {
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
    "get_run_activity_content",
    "configure_scm_polling_resume_grant",
    "get_scm_polling_resume_grant",
    "get_current_scm_polling_resume_grant",
    "review_scm_polling_resume",
    "configure_scm_polling_functional_grant",
    "get_scm_polling_functional_grant",
    "get_current_scm_polling_functional_grant",
    "review_scm_polling_functional",
    "get_final_outcome_candidate",
    "request_final_outcome_continuation",
    "get_final_outcome_status",
}
PRESENCE_CASES = [
    (*CASES[0][:4], {"version": 1, "input": {**ENVELOPE["input"], "skills": []}}, *CASES[0][5:]),
    (*CASES[2][:4], {**CANCEL, "project_id": None, "component_id": None}, *CASES[2][5:7], CANCELLED_DECISION),
    (*CASES[3][:7], CANCELLED_DECISION),
    (
        *CASES[5][:4],
        {
            **RESTORE,
            "source": {
                "kind": "bridge_observation",
                "record_digest": "2" * 64,
                "observation_id": "e" * 32,
                "terminal_digest": "6" * 64,
            },
        },
        *CASES[5][5:],
    ),
]


@pytest.mark.parametrize("case", CASES + PRESENCE_CASES, ids=[case[0] for case in CASES + PRESENCE_CASES])
@pytest.mark.parametrize("surface", ["cli", "mcp"])
async def test_exact_single_call_identity_body_presence_and_receipt(monkeypatch, tmp_path, case, surface):
    command, tool, arguments, parameters, body, method, path, receipt = case
    calls = []
    original = copy.deepcopy(body)
    receipt_bytes = json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode()

    def handle(request):
        calls.append(request)
        assert request.headers["Authorization"] == "Bearer retained-caller-token"
        return httpx.Response(200, content=receipt_bytes)

    monkeypatch.setattr("firefly_forge.cli.client", lambda: connection(handle))
    if surface == "cli":
        args = [command, *arguments]
        if body is not None:
            document = tmp_path / "retained.json"
            document.write_text(json.dumps(body))
            args.append(str(document))
        result = CliRunner().invoke(main, args)
        assert result.exit_code == 0, result.output
        returned = json.loads(result.output)
    else:
        async with Client(create_server(), cache=None, read_timeout_seconds=5) as session:
            result = await session.call_tool(tool, {**parameters, **({"request": body} if body is not None else {})})
            assert not result.is_error
            returned = result.structured_content
    assert returned == receipt
    assert json.dumps(returned, sort_keys=True, separators=(",", ":")).encode() == receipt_bytes
    assert body == original
    assert len(calls) == 1
    assert (calls[0].method, calls[0].url.path) == (method, path)
    assert (json.loads(calls[0].content) if body is not None else calls[0].content) == (
        body if body is not None else b""
    )


@pytest.mark.parametrize("case", CASES, ids=[case[0] for case in CASES])
@pytest.mark.parametrize("failure", ["404", "lost-response"])
@pytest.mark.parametrize("surface", ["cli", "mcp"])
async def test_errors_leave_exact_operation_unresolved_without_followup(monkeypatch, tmp_path, case, failure, surface):
    command, tool, arguments, parameters, body, method, path, _ = case
    calls = []

    def handle(request):
        calls.append(request)
        if failure == "lost-response":
            raise httpx.ReadTimeout("Uncertain response", request=request)
        return httpx.Response(404, json={"detail": "No recorded decision"})

    monkeypatch.setattr("firefly_forge.cli.client", lambda: connection(handle))
    if surface == "cli":
        args = [command, *arguments]
        if body is not None:
            document = tmp_path / "retained.json"
            document.write_text(json.dumps(body))
            args.append(str(document))
        result = CliRunner().invoke(main, args)
        assert result.exit_code != 0
        assert isinstance(result.exception, RuntimeError if failure == "404" else httpx.ReadTimeout)
    else:
        with pytest.raises(UnexpectedToolError):
            await create_server().call_tool(tool, {**parameters, **({"request": body} if body is not None else {})})
    assert len(calls) == 1
    assert (calls[0].method, calls[0].url.path) == (method, path)
    assert (json.loads(calls[0].content) if body is not None else calls[0].content) == (
        body if body is not None else b""
    )


INVALID = [
    (0, {"request_id": "../secret-marker"}, None),
    (0, {}, {**ENVELOPE, "version": True}),
    (0, {}, {**ENVELOPE, "version": 1.0}),
    (0, {}, {"input": ENVELOPE["input"]}),
    (0, {}, {**ENVELOPE, "input": {**ENVELOPE["input"], "actor": "secret-marker"}}),
    (1, {"request_id": REQUEST.upper()}, None),
    (2, {}, {key: value for key, value in CANCEL.items() if key != "project_id"}),
    (2, {}, {**CANCEL, "request_digest": "secret-marker"}),
    (2, {}, {**CANCEL, "force": True}),
    (2, {}, {**CANCEL, "component_id": None}),
    (3, {"request_id": True}, None),
    (3, {"request_id": "secret-marker"}, None),
    (4, {"identifier": "../secret-marker"}, None),
    (5, {"completion_id": "secret-marker"}, None),
    (5, {}, {**RESTORE, "request_id": "secret-marker"}),
    (5, {}, {**RESTORE, "version": 1}),
    (5, {}, {**RESTORE, "source": {"kind": "bridge_observation", "record_digest": "2" * 64}}),
    (6, {"request_id": "../secret-marker"}, None),
]


@pytest.mark.parametrize("index,overrides,invalid_body", INVALID)
@pytest.mark.parametrize("surface", ["cli", "mcp"])
async def test_strict_native_contracts_refuse_invalid_input_without_io(
    monkeypatch, tmp_path, index, overrides, invalid_body, surface
):
    command, tool, arguments, parameters, body, *_ = CASES[index]
    parameters = {**parameters, **overrides}
    body = invalid_body if invalid_body is not None else body
    calls = []
    monkeypatch.setattr(
        "firefly_forge.cli.client",
        lambda: connection(lambda request: calls.append(request) or httpx.Response(200, json={})),
    )
    if surface == "cli":
        args = [command, *[str(parameters[key]) for key in CASES[index][3]]]
        if body is not None:
            document = tmp_path / "retained.json"
            document.write_text(json.dumps(body))
            args.append(str(document))
        result = CliRunner().invoke(main, args)
        assert result.exit_code != 0
        assert isinstance(result.exception, ValueError)
        assert "secret-marker" not in str(result.exception)
    else:
        with pytest.raises(UnexpectedToolError):
            await create_server().call_tool(tool, {**parameters, **({"request": body} if body is not None else {})})
    assert calls == []


@pytest.mark.parametrize("case", CASES, ids=[case[0] for case in CASES])
def test_cli_requires_every_explicit_argument_without_io(monkeypatch, case):
    calls = []
    monkeypatch.setattr("firefly_forge.cli.client", lambda: calls.append("unexpected client"))
    result = CliRunner().invoke(main, [case[0]])
    assert result.exit_code == 2
    assert "Missing argument" in result.output
    assert calls == []


async def test_mcp_native_contracts_annotations_and_additive_150_tool_compatibility():
    tools = await create_server().list_tools()
    baseline = json.loads((Path(__file__).parent / "fixtures/mcp-recovery-additive-baseline-v1.json").read_text())
    original = [tool for tool in tools if tool.name not in NEW_TOOLS | KOINON_TOOLS]
    wire = ListToolsResult(tools=original).model_dump_json(by_alias=True, exclude_none=True)
    assert len(original) == baseline["tool_count"] == 150
    expected = json.loads(wire)
    assert json.dumps(expected, ensure_ascii=False, separators=(",", ":")) == wire
    routing = next(tool for tool in expected["tools"] if tool["name"] == "save_task_routing")
    bindings = routing["inputSchema"]["$defs"]["TaskRoutingInput"]["properties"]["bindings"]
    # Only the reviewed routing binding and campaign aggregate limit changes differ.
    assert bindings["maxProperties"] == 8
    bindings["maxProperties"] = 7
    for name, model in [("launch_project_campaign", "CampaignLaunch"), ("retry_project_campaign", "CampaignRetry")]:
        tool = next(tool for tool in expected["tools"] if tool["name"] == name)
        components = tool["inputSchema"]["$defs"][model]["properties"]["component_ids"]
        array = (
            next(value for value in components["anyOf"] if value.get("type") == "array")
            if "anyOf" in components
            else components
        )
        assert array == {
            "items": {"type": "string"},
            "minItems": 1,
            **({"title": "Component Ids"} if model == "CampaignRetry" else {}),
            "type": "array",
        }
        retained = {
            "items": array["items"],
            "maxItems": 50,
            **{key: value for key, value in array.items() if key != "items"},
        }
        array.clear()
        array.update(retained)
    from firefly_forge.sandbox_models import SandboxInput
    from firefly_forge.work_duration_models import WorkDurationPolicyV2

    runner = SandboxInput.model_json_schema()
    additions = {"process_monitor", "monitor_owner_grace_seconds", "monitor_timeout_seconds", "work_duration"}
    for name in ("save_repository_runner", "probe_runner"):
        tool = next(item for item in expected["tools"] if item["name"] == name)
        definitions = tool["inputSchema"]["$defs"]
        properties = definitions["SandboxInput"]["properties"]
        for field in additions:
            assert properties.pop(field) == runner["properties"][field]
        assert definitions.pop("WorkDurationPolicyV2") == WorkDurationPolicyV2.model_json_schema()
    for name, (retained, current) in KNOWLEDGE_DESCRIPTION_CHANGES.items():
        tool = next(item for item in expected["tools"] if item["name"] == name)
        assert tool["description"] == current
        tool["description"] = retained
    original_wire = json.dumps(expected, ensure_ascii=False, separators=(",", ":"))
    assert hashlib.sha256(original_wire.encode()).hexdigest() == baseline["wire_sha256"]
    assert len(tools) == 198
    catalog = prompt_catalog()
    root = Path("src/firefly_forge/resources/prompts/v1")
    entries = {
        entry["id"]: entry
        for entry in json.loads((root / "manifest.json").read_text())["templates"]
        if entry["version"] == "1.0.0"
    }
    for identity, digest in baseline["resource_sha256"].items():
        assert entries[identity]["sha256"] == digest
        assert hashlib.sha256((root / entries[identity]["path"]).read_bytes()).hexdigest() == digest
    tree = ast.parse(Path("src/firefly_forge/mcp_server.py").read_text())
    server = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "create_server")
    for node in server.body:
        if isinstance(node, ast.FunctionDef) and node.name not in NEW_TOOLS | KOINON_TOOLS:
            node.decorator_list = []
            assert (
                hashlib.sha256(ast.dump(node, include_attributes=False).encode()).hexdigest()
                == baseline["callback_ast_sha256"][node.name]
            )
    by_name = {tool.name: tool for tool in tools}
    for _, name, _, parameters, body, method, *_ in CASES:
        tool = by_name[name]
        Draft202012Validator.check_schema(tool.input_schema)
        Draft202012Validator(tool.input_schema).validate(
            {**parameters, **({"request": body} if body is not None else {})}
        )
        assert tool.description == catalog.text("mcp/" + name)
        assert entries["mcp/" + name]["variables"] == {}
        assert tool.input_schema["required"] == [*parameters, *(["request"] if body is not None else [])]
        assert tool.annotations.read_only_hint is (method == "GET")
        assert tool.annotations.idempotent_hint is (method == "GET")
        assert tool.annotations.open_world_hint is (method == "POST")
        assert tool.annotations.destructive_hint is (name == "cancel_run_creation_request")
        for identifier in parameters:
            expected = r"^fbr1_[a-f0-9]{32}$" if identifier == "completion_id" else r"^[a-f0-9]{32}$"
            assert tool.input_schema["properties"][identifier]["pattern"] == expected
        if body is not None:
            assert tool.input_schema["properties"]["request"]["additionalProperties"] is False
    creation = by_name["request_run_creation"].input_schema["properties"]["request"]
    assert creation["properties"]["version"]["const"] == 1
    cancel = by_name["cancel_run_creation_request"].input_schema["properties"]["request"]
    assert set(cancel["required"]) == {"version", "request_digest", "repository_id", "project_id", "component_id"}
    restore = by_name["request_agent_checkpoint_restore"].input_schema["properties"]["request"]
    assert set(restore["required"]) == set(RESTORE)
