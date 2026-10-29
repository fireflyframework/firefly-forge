# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Prepared native PostgreSQL custody controls; no external providers or commands."""

import hashlib
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel
from pyfly.data.transaction import current_unit_of_work
from test_agentic_restore_receipt_native import seed_restore_workspace
from test_bridge_completion_native import prepared

from firefly_forge.agent_roles import ProjectRoles, RoleRegistry
from firefly_forge.agentic_runtime import execute_agentic
from firefly_forge.agentic_tool_custody import (
    EFFECTS,
    INTENT,
    RESULT,
    UNKNOWN,
    AgenticToolCapability,
    AgenticToolCustody,
    agentic_tool_custody_scope,
)
from firefly_forge.journal import Journal
from firefly_forge.project_access import AccessDenied
from firefly_forge.workspace_tools import ToolRuntimeContext

LEGACY_TOOLS = frozenset(
    {
        "list_files",
        "read_file",
        "search_text",
        "git_diff",
        "write_file",
        "edit_lines",
        "execute_command",
        "knowledge_lookup",
        "read_run_evidence",
    }
)


@asynccontextmanager
async def native_custody(store, tmp_path):
    assert EFFECTS.keys() >= LEGACY_TOOLS
    async with prepared(store, tmp_path) as (_, actor, run, journal, _, _, _, _):
        roles = RoleRegistry(tmp_path / "custody-roles")
        catalog = roles.tools()
        for tool in catalog:
            if tool["id"] in LEGACY_TOOLS:
                tool["availability"] = "worker_unverified"
        await store.update(
            run["id"],
            status="developing",
            attempt=1,
            role_snapshot=roles.snapshot(
                ProjectRoles(disabled=["docs"], allowed_tools={"implementer": sorted(LEGACY_TOOLS)}),
                tool_catalog=catalog,
            ),
        )
        run = await store.get_run(run["id"])
        workspace = tmp_path / "custody-workspace"
        seed_restore_workspace(workspace)
        (workspace / "service.py").write_text("VALUE = 1\n")
        async with store._unit():
            task = await journal._task(run["id"], "implement")
            invocation = {**task.data, "attempt": task.attempt}
        custody = AgenticToolCustody(store, journal, run, invocation, workspace, store.assert_ownership)
        yield SimpleNamespace(store=store, journal=journal, run=run, custody=custody, workspace=workspace, actor=actor)


async def native_call(env, callback, *, call_id="exact-native-call", arguments=None, name="list_files"):
    return await AgenticToolCapability(env.custody, LEGACY_TOOLS).wrap_tool_execute(
        SimpleNamespace(run_step=1, messages=[]),
        call=ToolCallPart(name, arguments or {}, tool_call_id=call_id),
        tool_def=SimpleNamespace(name=name),
        args=arguments or {},
        handler=callback,
    )


async def test_native_public_agentic_all_nine_tools_have_exact_durable_results(store, tmp_path):
    async with native_custody(store, tmp_path) as env:
        edited = "VALUE = 2\n"
        sequence = [
            ("list_files", {}),
            ("read_file", {"path": "service.py"}),
            ("search_text", {"text": "VALUE"}),
            ("git_diff", {}),
            ("write_file", {"path": "new.py", "content": edited}),
            (
                "edit_lines",
                {
                    "path": "new.py",
                    "start_line": 1,
                    "end_line": 1,
                    "replacement": "VALUE = 3\n",
                    "expected_sha256": hashlib.sha256(edited.encode()).hexdigest(),
                },
            ),
            ("execute_command", {"argv": ["fixture-command"], "timeout": 5}),
            ("knowledge_lookup", {"query": "fixture guidance"}),
            ("read_run_evidence", {"query": {"kind": "journal"}}),
        ]

        def model(messages, info):
            returns = [part for msg in messages for part in msg.parts if isinstance(part, ToolReturnPart)]
            if len(returns) < len(sequence):
                name, args = sequence[len(returns)]
                return ModelResponse(parts=[ToolCallPart(name, args, tool_call_id="native-" + str(len(returns)))])
            return ModelResponse(parts=[TextPart('{"summary":"Nine native tools observed"}')])

        async def command(argv, timeout):
            assert current_unit_of_work() is None
            return SimpleNamespace(exit_code=0, output="Fixture command outcome")

        async def knowledge(*args):
            assert current_unit_of_work() is None
            return {"text": "Fixture guidance"}

        async def evidence(query):
            assert current_unit_of_work() is None
            return {"records": []}

        async def emit(*args):
            pass

        with agentic_tool_custody_scope(env.custody):
            summary, usage = await execute_agentic(
                env.workspace,
                "Inspect and modify fixture",
                "pyfly",
                "none",
                emit,
                model=FunctionModel(model),
                allow_output_token_cap=False,
                runtime_context=ToolRuntimeContext(
                    env.workspace,
                    env.workspace,
                    LEGACY_TOOLS,
                    command,
                    knowledge_lookup=knowledge,
                    read_run_evidence=evidence,
                ),
            )
        assert summary == "Nine native tools observed"
        entries = await env.journal.entries(env.run["id"])
        intents = [r for r in entries if r["kind"] == INTENT]
        results = [r for r in entries if r["kind"] == RESULT]
        assert len(intents) == len(results) == 9 and Journal.verify_chain(entries)
        assert [r["payload"]["tool"] for r in intents] == [name for name, _ in sequence]
        for index, (intent, result) in enumerate(zip(intents, results, strict=True)):
            assert intent["payload"]["tool_call_id"] == "native-" + str(index)
            assert result["payload"]["intent_digest"] == intent["digest"]
            assert intent["idempotency_key"] == result["idempotency_key"] == intent["payload"]["custody_id"]
            assert result["payload"]["status"] == "retained"
        assert usage["agentic_tool_custody"]["captured_tools"] == sorted(LEGACY_TOOLS)


@pytest.mark.parametrize("cut", ["before_intent", "after_intent", "after_callback", "after_result", "credential"])
async def test_native_crash_windows_never_repeat_callback(store, tmp_path, monkeypatch, cut):
    async with native_custody(store, tmp_path) as env:
        effects = []
        append = env.journal._append
        committed = {"value": False}

        async def cutting_append(run_id, task, key, kind, payload):
            if cut == "before_intent" and kind == INTENT:
                raise RuntimeError("before intent commit")
            result = await append(run_id, task, key, kind, payload)
            if cut == "after_result" and kind == RESULT:
                committed["value"] = True
            return result

        monkeypatch.setattr(env.journal, "_append", cutting_append)

        async def callback(args):
            assert current_unit_of_work() is None
            if cut == "after_intent":
                raise RuntimeError("controlled process loss")
            effects.append(1)
            if cut == "after_callback":
                raise RuntimeError("controlled result loss")
            return "fixture-secret-value" if cut == "credential" else {"files": ["service.py"]}

        admit = store.admit_credentials

        async def rejecting(value):
            if cut == "credential" and "fixture-secret-value" in str(value):
                raise ValueError("credential rejected")
            await admit(value)

        monkeypatch.setattr(store, "admit_credentials", rejecting)
        if cut in {"before_intent", "after_intent", "after_callback", "credential"}:
            with pytest.raises((ValueError, RuntimeError)):
                await native_call(env, callback)
        else:
            first = await native_call(env, callback)
            assert committed["value"]
            assert await native_call(env, callback) == first
        monkeypatch.setattr(env.journal, "_append", append)
        if cut == "before_intent":
            assert effects == []
            assert not [r for r in await env.journal.entries(env.run["id"]) if r["kind"] in {INTENT, RESULT, UNKNOWN}]
        elif cut != "after_result":
            with pytest.raises(ValueError, match="unresolved"):
                await native_call(env, callback)
        assert len(effects) == (0 if cut in {"before_intent", "after_intent"} else 1)
        assert "fixture-secret-value" not in str(await env.journal.entries(env.run["id"]))


@pytest.mark.parametrize("drift", ["arguments", "source", "actor", "owner", "configuration"])
async def test_native_completed_duplicate_requires_current_exact_admission(store, tmp_path, monkeypatch, drift):
    async with native_custody(store, tmp_path) as env:
        effects = []

        async def callback(args):
            effects.append(1)
            assert current_unit_of_work() is None
            return {"files": ["service.py"]}

        await native_call(env, callback)
        kwargs = {}
        if drift == "arguments":
            kwargs["arguments"] = {"path_prefix": "changed"}
        elif drift == "source":
            (env.workspace / "service.py").write_text("VALUE = 99\n")
        elif drift == "configuration":
            await store.update(env.run["id"], config_revision="f" * 32)
        elif drift == "actor":
            from firefly_forge.identity_persistence import ForgeIdentityUserRepository

            async with store._unit():
                users = ForgeIdentityUserRepository()
                actor = await users.find_by_id(env.actor.id)
                actor.enabled = False
                await users.save(actor)
        else:

            async def lost():
                raise ValueError("execution owner lost")

            monkeypatch.setattr(store, "assert_ownership", lost)
        with pytest.raises((ValueError, AccessDenied)):
            await native_call(env, callback, **kwargs)
        assert effects == [1]


async def test_native_nested_broker_receipts_keep_exact_payload_and_single_charge(store, tmp_path):
    from test_tool_broker import invoke as broker_invoke
    from test_tool_broker import seed

    _, broker, run, source = await seed(store, tmp_path)
    await store.acquire_ownership()
    roles = RoleRegistry(tmp_path / "nested-roles")
    catalog = roles.tools()
    for tool in catalog:
        if tool["id"] in {"internet_search", "knowledge_lookup"}:
            tool["availability"] = "worker_unverified"
    await store.update(
        run["id"],
        status="developing",
        attempt=1,
        role_snapshot=roles.snapshot(
            ProjectRoles(disabled=["docs"], allowed_tools={"implementer": ["internet_search", "knowledge_lookup"]}),
            tool_catalog=catalog,
        ),
    )
    run = await store.get_run(run["id"])
    workspace = tmp_path / "nested-workspace"
    seed_restore_workspace(workspace)
    async with store._unit():
        task = await broker.journal._task(run["id"], "implement")
        task_data = {**task.data, "attempt": task.attempt}
    custody = AgenticToolCustody(store, broker.journal, run, task_data, workspace, store.assert_ownership)
    env = SimpleNamespace(custody=custody)
    effects = []

    async def external():
        assert current_unit_of_work() is None
        effects.append(1)
        return {"results": []}

    async def callback(args):
        return await broker_invoke(broker, run, source, "1" * 32, external)

    first = await native_call(env, callback, name="knowledge_lookup", arguments={"query": "fixture"})
    assert await native_call(env, callback, name="knowledge_lookup", arguments={"query": "fixture"}) == first
    assert (
        await native_call(
            env, callback, name="knowledge_lookup", arguments={"query": "fixture"}, call_id="new-cached-public-call"
        )
        == first
    )
    rows = await broker.invocations(run["id"])
    entries = await broker.journal.entries(run["id"])
    broker_intent = next(r for r in entries if r["kind"] == "tool_intent")
    broker_receipt = next(r for r in entries if r["kind"] == "tool_receipt")
    assert len(rows) == 1 and effects == [1]
    assert rows[0]["admission"] == broker_intent["payload"]
    assert rows[0]["result"] == broker_receipt["payload"]["result"]
    retained = [r["payload"] for r in entries if r["kind"] == RESULT]
    assert len(retained) == 2 and retained[0]["parent_effects"] == retained[1]["parent_effects"]
    assert retained[0]["parent_effects"] == [
        {k: entry[k] for k in ("kind", "idempotency_key", "digest")} for entry in (broker_intent, broker_receipt)
    ]
    assert Journal.verify_chain(entries)


async def test_native_bridge_engine_retains_exact_public_tool_after_consumption(store, tmp_path, monkeypatch):
    from test_agentic_restore_receipt_native import native_restoration

    async with native_restoration(store, tmp_path, monkeypatch, grants=["write_file"]) as env:
        entries = await env.journal.entries(env.run_id)
        intent = next(r for r in entries if r["kind"] == INTENT)
        result = next(r for r in entries if r["kind"] == RESULT)
        consumed = [r for r in entries if r["kind"] == "agent_consume"]
        assert intent["payload"]["tool"] == "write_file"
        assert intent["payload"]["tool_call_id"] == "write-one"
        assert consumed[0]["sequence"] < intent["sequence"] < result["sequence"]
        assert result["payload"]["intent_digest"] == intent["digest"]
        assert result["payload"]["status"] == "retained"
        assert Journal.verify_chain(entries)
