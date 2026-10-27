# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Static text tools retain checkpoints; unowned runner effects forbid graph restore."""

from types import SimpleNamespace

import pytest
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel
from test_agentic_checkpoint_activation_host import RUN, activated, queue_original

from firefly_forge.agent_checkpoint_models import CheckpointPayloadV2
from firefly_forge.agent_roles import ProjectRoles, RoleRegistry
from firefly_forge.agentic_checkpoint_boundary import AgenticBoundary
from firefly_forge.agentic_checkpoint_invocation import CheckpointInvocation, checkpoint_invocation
from firefly_forge.agentic_runtime import execute_agentic
from firefly_forge.process import ProcessResult
from firefly_forge.workspace_tools import ToolRuntimeContext

GRANTS = frozenset(
    {
        "list_files",
        "read_file",
        "write_file",
        "search_text",
        "edit_lines",
        "git_diff",
        "execute_command",
        "knowledge_lookup",
        "read_run_evidence",
    }
)


async def nothing(*args):
    pass


def invocation_for(tmp_path, *, restored=None):
    return CheckpointInvocation(
        SimpleNamespace(credential_registry=dict),
        SimpleNamespace(),
        {"id": RUN},
        {},
        tmp_path,
        nothing,
        restored=restored,
    )


@pytest.mark.parametrize(
    "grants,qa",
    [
        (None, ()),
        (frozenset(), ()),
        (GRANTS | {"external_mcp"}, ()),
        (GRANTS | {"browser_check"}, ()),
        (GRANTS, ({"artifact_id": "qa"},)),
    ],
)
def test_static_checkpoint_still_refuses_implicit_dynamic_and_qa_grants(tmp_path, grants, qa):
    invocation = invocation_for(tmp_path)
    with pytest.raises(ValueError, match="explicit static.*without QA|exact frozen textual QA"):
        invocation.attach(
            AgenticBoundary(lambda: {"files_written": []}),
            "Build",
            "pyfly",
            "none",
            SimpleNamespace(allowed_tools=grants, qa_references=qa),
            {},
        )


def test_restored_static_grants_must_match_the_original_exactly(tmp_path):
    original = invocation_for(tmp_path)
    context = SimpleNamespace(allowed_tools=GRANTS, qa_references=())
    original.attach(AgenticBoundary(lambda: {"files_written": []}), "Build", "pyfly", "none", context, {})
    restored = SimpleNamespace(binding=original.binding)
    invocation = invocation_for(tmp_path, restored=restored)
    with pytest.raises(ValueError, match="grants or runtime contract changed"):
        invocation.attach(
            AgenticBoundary(lambda: {"files_written": []}),
            "Build",
            "pyfly",
            "none",
            SimpleNamespace(allowed_tools=GRANTS - {"execute_command"}, qa_references=()),
            {},
        )


async def test_required_checkpoint_runs_actual_static_function_tools_and_captures_their_returns(tmp_path):
    invocation = CheckpointInvocation(
        SimpleNamespace(credential_registry=dict), SimpleNamespace(), {"id": RUN}, {}, tmp_path, nothing
    )
    calls, checkpoints = [], []

    async def command(argv, timeout):
        calls.append(("execute_command", argv, timeout))
        (tmp_path / "service.py").write_text("VALUE = 2\n")
        return ProcessResult(argv, 0, "created")

    async def knowledge(query, refresh, request):
        calls.append(("knowledge_lookup", query, refresh, request))
        return {"text": "Recorded framework guidance"}

    async def evidence(query):
        calls.append(("read_run_evidence", query))
        return {"text": "Recorded failed check"}

    sequence = [
        ("execute_command", {"argv": ["python", "build.py"], "timeout": 10}),
        ("knowledge_lookup", {"query": "data transactions"}),
        ("read_run_evidence", {"query": {"kind": "journal"}}),
    ]

    def model(messages, info):
        assert {tool.name for tool in info.function_tools} == GRANTS
        assert invocation.boundary is not None
        checkpoints.append(invocation.boundary.capture(b"controlled-function-model-wire"))
        returns = [part for msg in messages for part in msg.parts if isinstance(part, ToolReturnPart)]
        if len(returns) < len(sequence):
            name, args = sequence[len(returns)]
            return ModelResponse(parts=[ToolCallPart(name, args, tool_call_id="static-" + str(len(returns)))])
        return ModelResponse(parts=[TextPart('{"summary":"Prepared and inspected"}')])

    context = ToolRuntimeContext(
        tmp_path, tmp_path, GRANTS, command, knowledge_lookup=knowledge, read_run_evidence=evidence
    )
    with checkpoint_invocation(invocation):
        summary, usage = await execute_agentic(
            tmp_path,
            "Build",
            "pyfly",
            "none",
            nothing,
            model=FunctionModel(model),
            runtime_context=context,
            settings={"FORGE_AGENTIC_CHECKPOINTS": "required", "FORGE_BRIDGE_COMPLETION_RECOVERY": "required"},
            allow_output_token_cap=False,
        )
    assert summary == "Prepared and inspected"
    assert [call[0] for call in calls] == [name for name, _ in sequence]
    assert usage["files_written"] == ["service.py"]
    assert checkpoints[-1].state == {"files_written": ["service.py"]}
    assert invocation.binding is not None and invocation.binding.grants == sorted(GRANTS)
    assert len(checkpoints) == 4


def set_command_grants(env, tmp_path):
    from test_sandbox_command_ordinals_host import Rows

    env.journal.sandboxes = Rows()
    catalog = RoleRegistry.tools()
    for tool in catalog:
        if tool["id"] in GRANTS:
            tool["availability"] = "worker_unverified"
    env.store.run.data["role_snapshot"] = RoleRegistry(tmp_path / "expanded-roles").snapshot(
        ProjectRoles(disabled=["docs"], allowed_tools={"implementer": sorted(GRANTS)}), tool_catalog=catalog
    )


@pytest.mark.parametrize("effect", ["done", "unknown", "interrupted"])
async def test_command_enabled_restore_admission_refuses_unowned_runner_history(tmp_path, monkeypatch, effect):
    from firefly_forge.workspace_tools import WorkspaceTools

    env = await activated(tmp_path, monkeypatch)
    set_command_grants(env, tmp_path)
    write = WorkspaceTools.write_file

    async def orphan(tools, path, content):
        await env.journal._append(
            RUN,
            "implement",
            "orphan-command",
            "sandbox_receipt",
            {"execution_id": "c" * 32, "effect": "start", "evidence": {"outcome": effect}},
        )
        return await write(tools, path, content)

    monkeypatch.setattr(WorkspaceTools, "write_file", orphan)
    try:
        with pytest.raises(ValueError, match="Prior runner.*restore"):
            await queue_original(env, monkeypatch, source="http_response")
        assert any(row.kind == "sandbox_receipt" for row in env.journal.records.rows)
        assert not env.requests.records.rows
    finally:
        await env.model.client.close()


async def test_command_enabled_restore_reentry_refuses_runner_effect_added_after_admission(tmp_path, monkeypatch):
    env = await activated(tmp_path, monkeypatch)
    set_command_grants(env, tmp_path)
    try:
        posts, _, checkpoint, _, request = await queue_original(env, monkeypatch, source="http_response")
        assert CheckpointPayloadV2.model_validate(checkpoint["payload"]).invocation.grants == sorted(GRANTS)
        await env.journal._append(
            RUN,
            "implement",
            "orphan-command",
            "sandbox_intent",
            {"execution_id": "c" * 32, "effect": "start", "request": {}},
        )
        await env.restores.execute(request.request_id)
        ticket = await env.requests.get(request.request_id)
        assert ticket["status"] == "failed"
        assert len(posts) == 2
        assert len([row for row in env.journal.records.rows if row.kind == "agent_consume"]) == 1
    finally:
        await env.model.client.close()


async def test_read_only_restore_does_not_consult_or_erase_runner_history(tmp_path, monkeypatch):
    env = await activated(tmp_path, monkeypatch)

    async def never(run_id):
        raise AssertionError("File-only restoration must retain its existing behavior")

    monkeypatch.setattr(env.journal, "sandbox_executions", never)
    try:
        await queue_original(env, monkeypatch, source="http_response")
        assert len(env.requests.records.rows) == 1
    finally:
        await env.model.client.close()
