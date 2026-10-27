# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Exact public call custody through actual FireflyAgent capability execution."""

import copy
from contextlib import asynccontextmanager
from functools import wraps
from types import SimpleNamespace

import pytest
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel
from pyfly.data.transaction import current_unit_of_work

from firefly_forge.agentic_runtime import execute_agentic
from firefly_forge.journal import Journal
from firefly_forge.workspace_tools import ToolRuntimeContext, WorkspaceTools


class MemoryJournal:
    def __init__(self, run):
        self.run, self.rows = run, []
        self.records = self

    async def _run(self, run_id):
        return SimpleNamespace(data=self.run)

    async def _task(self, run_id, task_id):
        return SimpleNamespace(status="running", attempt=1, data={"idempotency_key": "task-one"})

    async def _append(self, run_id, task_id, key, kind, payload):
        from firefly_forge.journal import ZERO_HASH, digest

        row = dict(
            run_id=run_id,
            sequence=len(self.rows) + 1,
            task_id=task_id,
            idempotency_key=key,
            kind=kind,
            payload=copy.deepcopy(payload),
            created_at="2026-10-27T00:00:00Z",
            previous_hash=self.rows[-1]["digest"] if self.rows else ZERO_HASH,
        )
        row["digest"] = digest(row)
        self.rows.append(row)
        return copy.deepcopy(row)

    async def find_for_run(self, run_id):
        from uuid import UUID

        return [SimpleNamespace(**{**copy.deepcopy(row), "run_id": UUID(row["run_id"])}) for row in self.rows]

    async def entries(self, run_id):
        return copy.deepcopy(self.rows)


async def setup(tmp_path, monkeypatch):
    from firefly_forge.agentic_tool_custody import AgenticToolCustody

    run = dict(id="1" * 32, active_task="implement", attempt=1, status="developing", agent="api")
    journal = MemoryJournal(run)
    allowed, source = {"value": True}, {"value": "source-one"}

    async def guard():
        if not allowed["value"]:
            raise ValueError("current authority revoked")

    @asynccontextmanager
    async def unit():
        yield None

    async def credentials(value):
        if "SECRET-FIXTURE" in str(value):
            raise ValueError("credential rejected")

    async def source_cut(*args, **kwargs):
        assert current_unit_of_work() is None
        return {"path": str(tmp_path), "head": "a" * 40, "metadata": "b" * 64, "source": source["value"]}

    async def actor(*args, **kwargs):
        await guard()

    monkeypatch.setattr("firefly_forge.agentic_tool_custody.workspace_cut", source_cut)
    monkeypatch.setattr("firefly_forge.project_access.ProjectAccess.authorize_execution", actor)

    async def get_run(run_id):
        return run

    store = SimpleNamespace(
        get_run=get_run,
        _unit=unit,
        admit_credentials=credentials,
        reject_credentials=lambda v: None,
        assert_ownership=guard,
    )
    custody = AgenticToolCustody(store, journal, run, {"idempotency_key": "task-one"}, tmp_path, guard)
    return SimpleNamespace(custody=custody, journal=journal, allowed=allowed, source=source, run=run)


async def direct(env, callback, *, name="list_files", arguments=None, call_id="public-call-one", step=1):
    from firefly_forge.agentic_tool_custody import AgenticToolCapability

    return await AgenticToolCapability(env.custody, frozenset({name})).wrap_tool_execute(
        SimpleNamespace(run_step=step, messages=[]),
        call=ToolCallPart(name, arguments or {}, tool_call_id=call_id),
        tool_def=SimpleNamespace(name=name),
        args=arguments or {},
        handler=callback,
    )


async def test_exact_completed_duplicate_reuses_retained_result_after_admission(tmp_path, monkeypatch):
    env = await setup(tmp_path, monkeypatch)
    effects = []

    async def callback(args):
        assert env.journal.rows[-1]["kind"] == "agentic_tool_intent"
        effects.append(1)
        return {"files": ["service.py"]}

    first = await direct(env, callback)
    assert await direct(env, callback) == first
    assert effects == [1]
    assert [r["kind"] for r in env.journal.rows] == ["agentic_tool_intent", "agentic_tool_result"]
    assert Journal.verify_chain(env.journal.rows)
    env.allowed["value"] = False
    with pytest.raises(ValueError, match="revoked"):
        await direct(env, callback)
    assert effects == [1]


@pytest.mark.parametrize("mutation", ["arguments", "tool", "source", "step"])
async def test_changed_exact_call_scope_never_reexecutes(tmp_path, monkeypatch, mutation):
    env = await setup(tmp_path, monkeypatch)
    effects = []

    async def callback(args):
        effects.append(1)
        return "retained"

    await direct(env, callback)
    kwargs = {}
    if mutation == "arguments":
        kwargs["arguments"] = {"path": "changed"}
    elif mutation == "tool":
        kwargs["name"] = "read_file"
    elif mutation == "source":
        env.source["value"] = "changed"
    else:
        kwargs["step"] = 2
    with pytest.raises(ValueError, match="changed|another"):
        await direct(env, callback, **kwargs)
    assert effects == [1]


@pytest.mark.parametrize("cut", ["after_intent", "after_callback", "credential", "lease"])
async def test_uncertain_effect_never_reexecutes(tmp_path, monkeypatch, cut):
    env = await setup(tmp_path, monkeypatch)
    effects = []

    async def callback(args):
        if cut == "after_intent":
            raise KeyboardInterrupt("controlled crash")
        effects.append(1)
        if cut == "after_callback":
            raise KeyboardInterrupt("controlled crash")
        if cut == "lease":
            env.allowed["value"] = False
        return "SECRET-FIXTURE" if cut == "credential" else "observed"

    with pytest.raises((KeyboardInterrupt, ValueError)):
        await direct(env, callback)
    env.allowed["value"] = True
    with pytest.raises(ValueError, match="unresolved"):
        await direct(env, callback)
    assert len(effects) == (0 if cut == "after_intent" else 1)
    assert all(r["kind"] != "agentic_tool_result" for r in env.journal.rows)
    assert "SECRET-FIXTURE" not in str(env.journal.rows)


@pytest.mark.parametrize("raw_json", [False, True])
async def test_public_fireflyagent_capability_captures_actual_call_id_and_result(tmp_path, monkeypatch, raw_json):
    from firefly_forge.agentic_tool_custody import agentic_tool_custody_scope

    env = await setup(tmp_path, monkeypatch)
    (tmp_path / "service.py").write_text("VALUE = 1\n")
    calls = []
    original = WorkspaceTools.list_files_page

    @wraps(original)
    async def listing(self, *args, **kwargs):
        calls.append(1)
        return await original(self, *args, **kwargs)

    monkeypatch.setattr(WorkspaceTools, "list_files_page", listing)

    def model(messages, info):
        if not any(isinstance(p, ToolReturnPart) for m in messages for p in m.parts):
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        "list_files",
                        '{"path_prefix":""}' if raw_json else {"path_prefix": ""},
                        tool_call_id="provider-call-42",
                    )
                ]
            )
        if not any(isinstance(p, ToolReturnPart) and p.tool_name == "write_file" for m in messages for p in m.parts):
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        "write_file", {"path": "done.py", "content": "VALUE = 2"}, tool_call_id="provider-write-43"
                    )
                ]
            )
        return ModelResponse(parts=[TextPart('{"summary":"Inspected"}')])

    async def emit(*args):
        pass

    with agentic_tool_custody_scope(env.custody):
        _, usage = await execute_agentic(
            tmp_path,
            "Inspect",
            "pyfly",
            "none",
            emit,
            model=FunctionModel(model),
            allow_output_token_cap=False,
            runtime_context=ToolRuntimeContext(tmp_path, tmp_path, frozenset({"list_files", "write_file"})),
        )
    assert calls == [1]
    assert env.journal.rows[0]["payload"]["tool_call_id"] == "provider-call-42"
    payload = env.journal.rows[0]["payload"]
    assert isinstance(payload["arguments"], dict) and payload["arguments"]["path_prefix"] == ""
    assert payload["raw_arguments"] == ('{"path_prefix":""}' if raw_json else {"path_prefix": ""})
    assert "service.py" in str(env.journal.rows[1]["payload"]["result"])
    assert usage["agentic_tool_custody"]["status"] == "captured"


async def test_retained_result_payload_mutation_is_rejected_by_native_chain(tmp_path, monkeypatch):
    from firefly_forge.journal import digest

    env = await setup(tmp_path, monkeypatch)
    effects = []

    async def callback(args):
        effects.append(1)
        return {"files": ["original.py"]}

    await direct(env, callback)
    payload = env.journal.rows[-1]["payload"]
    payload["result"] = {"files": ["invented.py"]}
    payload["result_sha256"] = digest({"result": payload["result"]})
    with pytest.raises(ValueError, match="chain"):
        await direct(env, callback)
    assert effects == [1]


async def test_native_frozen_tool_grant_cannot_be_bypassed_by_runtime_context(tmp_path, monkeypatch):
    from firefly_forge.agent_roles import ProjectRoles, RoleRegistry

    env = await setup(tmp_path, monkeypatch)
    roles = RoleRegistry(tmp_path / "roles")
    snapshot = roles.snapshot(ProjectRoles(disabled=["docs"], allowed_tools={"implementer": ["read_file"]}))
    env.run["role_snapshot"] = snapshot
    env.custody.run["role_snapshot"] = copy.deepcopy(snapshot)
    effects = []

    async def callback(args):
        effects.append(1)
        return {}

    with pytest.raises(ValueError, match="grant"):
        await direct(env, callback)
    assert effects == [] and env.journal.rows == []


async def test_large_existing_tool_result_uses_verified_artifact_without_lowering_tool_limit(tmp_path, monkeypatch):
    env = await setup(tmp_path, monkeypatch)
    env.custody.store.path = tmp_path / "state" / "factory.db"
    env.custody.store.path.parent.mkdir()
    result = "source-line\n" * 90_000
    calls = []

    async def callback(args):
        calls.append(1)
        return result

    assert await direct(env, callback, name="read_file", arguments={"path": "large.py"}) == result
    payload = env.journal.rows[-1]["payload"]
    assert "result" not in payload and payload["result_artifact"]["bytes"] > 800_000
    assert await direct(env, callback, name="read_file", arguments={"path": "large.py"}) == result
    artifact = env.custody.store.path.parent / "agentic-tool-results" / (payload["result_artifact"]["sha256"] + ".json")
    artifact.write_text('"tampered"')
    with pytest.raises(ValueError, match="artifact"):
        await direct(env, callback, name="read_file", arguments={"path": "large.py"})
    assert calls == [1]


async def test_completed_large_arguments_require_original_artifact_custody(tmp_path, monkeypatch):
    env = await setup(tmp_path, monkeypatch)
    env.custody.store.path = tmp_path / "state" / "factory.db"
    env.custody.store.path.parent.mkdir()
    arguments = {"path": "large.py", "content": "VALUE = 1\n" * 20_000}
    calls = []

    async def callback(args):
        calls.append(1)
        return "Wrote large.py"

    await direct(env, callback, name="write_file", arguments=arguments)
    descriptor = env.journal.rows[0]["payload"]["arguments_artifact"]
    artifact = env.custody.store.path.parent / "agentic-tool-results" / (descriptor["sha256"] + ".json")
    artifact.write_text('"changed"')
    with pytest.raises(ValueError, match="artifact"):
        await direct(env, callback, name="write_file", arguments=arguments)
    assert calls == [1]


async def test_new_public_call_links_exact_cached_broker_receipts_without_second_effect(tmp_path, monkeypatch):
    from firefly_forge.agentic_tool_custody import link_retained_broker_effects

    env = await setup(tmp_path, monkeypatch)
    effects = []

    async def callback(args):
        if not effects:
            effects.append(1)
            await env.journal._append(
                env.run["id"], "implement", "child-one", "tool_intent", {"tool": "knowledge_lookup"}
            )
            await env.journal._append(
                env.run["id"],
                "implement",
                "child-one",
                "tool_receipt",
                {"tool": "knowledge_lookup", "status": "succeeded", "result": {"text": "retained"}},
            )
        await link_retained_broker_effects(env.journal, env.run["id"], "child-one")
        return {"text": "retained"}

    await direct(env, callback, name="knowledge_lookup")
    await direct(env, callback, name="knowledge_lookup", call_id="new-public-call")
    retained = [row for row in env.journal.rows if row["kind"] == "agentic_tool_result"]
    children = [row for row in env.journal.rows if row["kind"] in {"tool_intent", "tool_receipt"}]
    expected = [{key: row[key] for key in ("kind", "idempotency_key", "digest")} for row in children]
    assert len(children) == 2 and effects == [1]
    assert retained[0]["payload"]["parent_effects"] == retained[1]["payload"]["parent_effects"] == expected


@pytest.mark.parametrize("tool", ["write_file", "execute_command"])
async def test_unresolved_native_effect_holds_later_calls_model_requests_and_completion(tmp_path, monkeypatch, tool):
    from firefly_forge.agentic_tool_custody import AgenticToolCapability

    env = await setup(tmp_path, monkeypatch)
    effects = []

    async def uncertain(args):
        effects.append(tool)
        raise RuntimeError("result lost after effect")

    async def later(args):
        effects.append("later")
        return "must not execute"

    with pytest.raises(RuntimeError):
        await direct(env, uncertain, name=tool)
    with pytest.raises(ValueError, match="unresolved.*effect"):
        await direct(env, later, call_id="different-public-call")
    capability = AgenticToolCapability(env.custody, frozenset({tool}))
    with pytest.raises(ValueError, match="unresolved.*effect"):
        await capability.before_model_request(SimpleNamespace(), SimpleNamespace())
    with pytest.raises(ValueError, match="unresolved.*effect"):
        await env.custody.assert_effects_settled()
    assert effects == [tool]


@pytest.mark.parametrize("tool", ["browser_journey", "profile_python", "internet_search"])
async def test_unresolved_write_also_holds_uncaptured_public_tool_callback(tmp_path, monkeypatch, tool):
    from firefly_forge.agentic_tool_custody import AgenticToolCapability

    env = await setup(tmp_path, monkeypatch)
    callbacks = []

    async def uncertain(args):
        raise RuntimeError("result lost after write")

    async def uncaptured(args):
        callbacks.append(tool)
        return object()

    with pytest.raises(RuntimeError):
        await direct(env, uncertain, name="write_file")
    capability = AgenticToolCapability(env.custody, frozenset({"write_file"}))
    with pytest.raises(ValueError, match="unresolved.*effect"):
        await capability.wrap_tool_execute(
            SimpleNamespace(run_step=1, messages=[]),
            call=ToolCallPart(tool, {}, tool_call_id="uncaptured-call"),
            tool_def=SimpleNamespace(name=tool),
            args={},
            handler=uncaptured,
        )
    assert callbacks == []
