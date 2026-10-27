# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""PREPARED, UNRUN: fresh-schema PostgreSQL continuity, no provider or original CRM state.

The sole database operator must supply FORGE_TEST_DATABASE_URL ending in _test.
All model HTTP uses an injected public MockTransport; qualification seals are
fixture-only. These controls cannot qualify or admit an actual historical run.
"""

import asyncio
import copy
import json
from contextlib import AsyncExitStack
from types import SimpleNamespace
from uuid import uuid4

import httpx2
import pytest
from pydantic_ai import models
from test_agentic_checkpoint_activation_host import result
from test_agentic_restore_receipt_native import seed_restore_workspace
from test_bridge_completion_callbacks import CAPABILITY
from test_bridge_completion_native import prepared

from firefly_forge import agentic_runtime, agentic_tool_custody
from firefly_forge import invalid_read_continuities as native
from firefly_forge.agent_roles import ProjectRoles, RoleRegistry
from firefly_forge.agentic_checkpoint_invocation import runtime_contract
from firefly_forge.configuration import ProviderConfiguration
from firefly_forge.engine import Engine
from firefly_forge.execution_requests import ExecutionRequests
from firefly_forge.git_workspace import metadata_digest
from firefly_forge.invalid_read_continuity_models import InvalidReadAdmissionInputV1
from firefly_forge.invalid_read_lease import ContinuityLeasePolicyV1, write_policy
from firefly_forge.journal import Journal
from firefly_forge.models import RunInput
from firefly_forge.providers import BridgeProvider
from firefly_forge.runtime_roles import RuntimeRoles
from firefly_forge.workspace_tools import tool_catalog


@pytest.fixture
async def native_continuity(store, tmp_path, monkeypatch):
    async with prepared(store, tmp_path) as (_, actor, seeded, *_):
        store.configuration.update("bridge", ProviderConfiguration(values={"checkpoints": "required"}))
        run = await store.create_run(
            RunInput(
                repository_id=seeded["repository_id"],
                project_id=seeded["project_id"],
                component_id="api",
                agent="bridge",
                brief="Fictional same-attempt invalid-read control",
                skills=[],
            )
        )
        run_id = run["id"]
        await store.update(
            run_id,
            **store.configuration.snapshot("bridge"),
            resume_existing=True,
            repair_budget=2,
            attempt=2,
            implementation_rounds=1,
            commands=[],
            role_snapshot=RoleRegistry(tmp_path / "continuity-roles").snapshot(
                ProjectRoles(
                    disabled=["docs"],
                    allowed_tools={
                        "implementer": ["read_run_evidence", "read_file", "list_files", "execute_command", "write_file"]
                    },
                ),
                tool_catalog=tool_catalog({"read_run_evidence": True, "execute_command": True}),
            ),
        )
        journal = Journal(store)
        runtime = RuntimeRoles(store)
        await runtime.start()
        requests = ExecutionRequests(store, runtime)
        registry = store.session_provider._registry()
        assert registry is not None
        for repository in (journal.records, journal.tasks, journal.sandboxes, requests.records, requests.journal):
            repository._bind_transaction_managers(registry)
        await journal.ensure_plan(run_id)
        async with store._unit():
            task = await journal._task(run_id, "implement")
            task.attempt = 2
            await journal.tasks.save(task)
        provider = BridgeProvider(store.configuration.load_snapshot((await store.get_run(run_id))["config_revision"]))
        engine = Engine(store, tmp_path / "continuity-owned", lambda _: provider, journal=journal)
        from firefly_forge.process import ProcessResult
        from firefly_forge.sandbox import SandboxExecutor
        from firefly_forge.sandbox_models import SandboxInput, capture_sandbox

        command_calls = []

        class Backend:
            async def run(self, config, argv, cwd, timeout, output_limit):
                command_calls.append(argv)
                return ProcessResult(argv, 0, "Controlled SQL command")

        await store.update(run_id, sandbox_snapshot=capture_sandbox(SandboxInput(command_limit=22)))
        engine.sandbox = SandboxExecutor(store, journal, SimpleNamespace(select=lambda _: Backend()))
        workspace = engine.workspaces / run_id
        revision = seed_restore_workspace(workspace)
        (workspace / "existing.py").write_text("VALUE = 1\n")
        await store.update(run_id, git_metadata_digest=metadata_digest(workspace))

        async def inspect(*args, **kwargs):
            return {"base_revision": revision, "head": revision, "source_tree": "c" * 40}

        monkeypatch.setattr(engine, "_inspect_workspace", inspect)
        original_event = store.event
        seeded_history = False

        async def charged_history_event(current_id, stage, message, *args, **kwargs):
            nonlocal seeded_history
            if current_id == run_id and stage == "provider" and not seeded_history:
                seeded_history = True
                for index in range(11):
                    await engine.sandbox.execute(
                        run_id, workspace, workspace, ["retained-command-" + str(index + 1)], 2, "implement"
                    )
            return await original_event(current_id, stage, message, *args, **kwargs)

        monkeypatch.setattr(store, "event", charged_history_event)
        posts = []

        def transport(request):
            if request.method == "GET":
                return httpx2.Response(200, json={"completionRecovery": CAPABILITY})
            posts.append(request.content)
            assert len(posts) <= 7, "Continuity must never replay the consumed original response"
            if len(posts) <= 6:
                response = result(tool=True)
                call = response["choices"][0]["message"]["tool_calls"][0]
                call["id"] = "native-read-" + str(len(posts))
                name, arguments = {
                    1: ("read_run_evidence", '{"query":{"tool_name":"read_file"}}'),
                    2: ("read_run_evidence", '{"query":{"kind":"journal","limit":2}}'),
                    3: ("write_file", '{"path":"existing.py","content":"VALUE = 2\\n"}'),
                    4: ("execute_command", '{"argv":["first-controlled-command"],"timeout":2}'),
                    5: ("read_file", '{"path":"existing.py"}'),
                    6: ("execute_command", '{"argv":["second-controlled-command"],"timeout":2}'),
                }[len(posts)]
                call["function"] = {"name": name, "arguments": arguments}
            else:
                response = result(
                    json.dumps(
                        {
                            "summary": "Fictional native continuation retained",
                            "qa_report": None,
                            "worker_feedback": {
                                "id": "native-continuity-review",
                                "role_id": "implementer",
                                "task_id": "implement",
                                "attempt": 2,
                                "disposition": "clarification",
                                "summary": "Original source retained",
                                "request": {
                                    "id": "native-continuity-review",
                                    "kind": "product_definition",
                                    "question": "Proceed?",
                                    "context": "Fictional test",
                                },
                            },
                        }
                    )
                )
            return httpx2.Response(200, json=response)

        class HistoricalInput:
            @classmethod
            def model_json_schema(cls):
                from typing import Any

                from pydantic_ai.tools import Tool

                async def old_query(query: dict[str, Any]) -> dict[str, Any]:
                    raise AssertionError("Schema-only historical signature")

                return Tool(old_query, name="read_run_evidence").tool_def.parameters_json_schema

            @classmethod
            def model_validate(cls, arguments):
                query = arguments["query"]
                return SimpleNamespace(
                    query=SimpleNamespace(model_dump=lambda **_: dict(query), limit=query.get("limit", 10))
                )

        async with AsyncExitStack() as controlled_transports:
            controlled_transports.enter_context(models.override_allow_model_requests(True))
            http = await controlled_transports.enter_async_context(
                httpx2.AsyncClient(transport=httpx2.MockTransport(transport))
            )
            model = provider.model(http_client=http)
            monkeypatch.setattr(provider, "model", lambda **kwargs: model)
            try:
                custody_lookup = agentic_tool_custody.current_agentic_tool_custody
                with monkeypatch.context() as historical, models.override_allow_model_requests(True):
                    # Match the published pre-custody producer only; restore current custody before activation.
                    historical.setattr(agentic_tool_custody, "current_agentic_tool_custody", lambda: None)
                    historical.setattr(agentic_runtime, "WorkerEvidenceReadInput", HistoricalInput)
                    render = agentic_runtime.prompt_text
                    historical.setattr(
                        agentic_runtime,
                        "prompt_text",
                        lambda name, **kwargs: render(
                            name, **({**kwargs, "version": "1.0.0"} if name == "tools/read_run_evidence" else kwargs)
                        ),
                    )
                    await engine.execute_run(run_id)
                assert agentic_tool_custody.current_agentic_tool_custody is custody_lookup
                historical_entries = await journal.entries(run_id)
                assert not any(entry["kind"].startswith("agentic_tool_") for entry in historical_entries)
                frozen = await store.get_run(run_id)
                assert frozen["status"] == "failed" and frozen["bridge_completion_hold"] and len(posts) == 1
                initial_commands = copy.deepcopy(await journal.sandbox_executions(run_id))
                assert len(initial_commands) == 11
                monkeypatch.setattr(native, "LEGACY_RUNTIME_CONTRACT", runtime_contract())
                monkeypatch.setattr(
                    native, "current_profile_seal", lambda: {"profile_id": "fixture-only", "digest": "8" * 64}
                )
                service = native.InvalidReadContinuities(store, journal, engine, requests)
                requests.invalid_read_continuities = service
                write_policy(
                    store.configuration, ContinuityLeasePolicyV1(version=1, enabled=True, max_window_seconds=180)
                )
                yield SimpleNamespace(
                    store=store,
                    service=service,
                    requests=requests,
                    engine=engine,
                    journal=journal,
                    run_id=run_id,
                    frozen=frozen,
                    posts=posts,
                    actor=actor,
                    initial_commands=initial_commands,
                    command_calls=command_calls,
                )
            finally:
                await runtime.stop()


async def consent(case):
    candidate = await case.service.candidate(case.run_id)
    assert candidate["admission"] == "READY", candidate["holds"]
    return InvalidReadAdmissionInputV1(
        version=1,
        candidate_digest=candidate["digest"],
        expected_run_digest=candidate["run_digest"],
        expected_journal_digest=candidate["journal_digest"],
        expected_policy_digest=candidate["lease_policy"]["digest"],
        valid_for_seconds=180,
        acknowledge_no_new_grant=True,
        acknowledge_original_consumption=True,
        acknowledge_renewed_same_attempt_lease=True,
    )


async def test_native_continuity_atomic_rollback_and_idempotent_concurrent_admission(native_continuity, monkeypatch):
    c = native_continuity
    body = await consent(c)
    before, prefix = await c.store.get_run(c.run_id), await c.journal.entries(c.run_id)
    save = c.store.run_records.save

    async def refused(row):
        await save(row)
        if row.data.get("invalid_read_continuity"):
            raise RuntimeError("Controlled continuity admission rollback")

    with monkeypatch.context() as patch:
        patch.setattr(c.store.run_records, "save", refused)
        with pytest.raises(RuntimeError, match="rollback"):
            await c.service.request(c.run_id, uuid4().hex, body)
    assert await c.store.get_run(c.run_id) == before
    assert await c.journal.entries(c.run_id) == prefix
    request_id = uuid4().hex
    replies = await asyncio.gather(*(c.service.request(c.run_id, request_id, body) for _ in range(3)))
    assert replies[1:] == replies[:1] * 2
    entries = await c.journal.entries(c.run_id)
    assert len([e for e in entries if e["kind"] == "invalid_read_settlement"]) == 1
    assert len([e for e in entries if e["kind"] == "invalid_read_claim"]) == 1
    assert len(c.posts) == 1


async def test_native_continuity_preserves_usage_and_runs_two_new_tool_cycles_once(native_continuity):
    c = native_continuity
    body = await consent(c)
    ticket = await c.service.request(c.run_id, uuid4().hex, body)
    with models.override_allow_model_requests(True):
        await asyncio.gather(c.service.execute(ticket["id"]), c.service.execute(ticket["id"]))
    current = await c.store.get_run(c.run_id)
    observed = await c.service.get(c.run_id, ticket["id"])
    assert observed["status"] == "completed", observed.get("result")
    assert current["status"] == "paused" and current["usage"]["requests"] == 7
    assert current["repair_budget"] == 2
    assert current["attempt"] == c.frozen["attempt"] == 2
    assert current["implementation_rounds"] == c.frozen["implementation_rounds"] + 1 == 2
    assert json.loads(json.loads(c.posts[1])["messages"][-1]["content"]) == {"status": "invalid_query"}
    entries = await c.journal.entries(c.run_id)
    assert len([e for e in entries if e["kind"] == "agent_consume"]) == 7
    executions = await c.journal.sandbox_executions(c.run_id)
    assert len(executions) == len(c.command_calls) == 13
    assert [item["ordinal"] for item in executions] == list(range(1, 14))
    assert executions[:11] == c.initial_commands
    assert current["sandbox_snapshot"]["configuration"]["command_limit"] == 22
    await c.service.execute(ticket["id"])
    assert len(c.posts) == 7
    assert len(await c.journal.sandbox_executions(c.run_id)) == 13


async def test_native_continuity_revoked_policy_cannot_dispatch_or_double_charge(native_continuity):
    c = native_continuity
    body = await consent(c)
    ticket = await c.service.request(c.run_id, uuid4().hex, body)
    before = copy.deepcopy(await c.journal.entries(c.run_id))
    write_policy(c.store.configuration, ContinuityLeasePolicyV1(version=1, enabled=False))
    await c.service.execute(ticket["id"])
    assert (await c.service.get(c.run_id, ticket["id"]))["status"] == "failed"
    assert await c.journal.entries(c.run_id) == before and len(c.posts) == 1


async def test_native_request_repository_counts_and_keys_are_not_paginated(native_continuity):
    from datetime import UTC, datetime, timedelta

    from firefly_forge.execution_request_persistence import ForgeExecutionRequestEntity
    from firefly_forge.invalid_read_tickets import KIND
    from firefly_forge.store import identifier

    c = native_continuity
    original = await c.store.get_run(c.run_id)
    other = await c.store.create_run(
        RunInput(
            repository_id=original["repository_id"],
            project_id=original["project_id"],
            component_id="api",
            agent="bridge",
            brief="Fictional independent request-count scope",
            skills=[],
        )
    )
    records = c.requests.records
    history = []
    stamp = datetime.now(UTC)
    async with c.store._unit():
        for index in range(51):
            row = ForgeExecutionRequestEntity(
                id=uuid4(),
                run_id=identifier(c.run_id),
                kind=KIND,
                request_key="historical-" + str(index),
                status="failed",
                data={"fixture": "scoped-counter"},
                created_at=stamp + timedelta(microseconds=index),
            )
            await records.save(row)
            history.append(row)
        foreign_kind = []
        for index in range(3):
            row = ForgeExecutionRequestEntity(
                id=uuid4(),
                run_id=identifier(c.run_id),
                kind="git_delivery",
                request_key="other-kind-" + str(index),
                status="queued",
                data={"fixture": "other-kind"},
            )
            await records.save(row)
            foreign_kind.append(row)
        for index in range(5):
            await records.save(
                ForgeExecutionRequestEntity(
                    id=uuid4(),
                    run_id=identifier(other["id"]),
                    kind=KIND,
                    request_key="other-run-" + str(index),
                    status="queued",
                    data={"fixture": "other-run"},
                )
            )
        page = await records.for_run(identifier(c.run_id), KIND)
        assert len(page) == 50 and history[0].id not in {row.id for row in page}
        assert await records.count_for_run(identifier(c.run_id), KIND) == 51
        assert await records.count_for_run(identifier(c.run_id), "git_delivery") == 3
        assert await records.count_for_run(identifier(other["id"]), KIND) == 5
        assert (await records.for_key(identifier(c.run_id), KIND, "historical-0")).id == history[0].id
        assert await records.for_key(identifier(other["id"]), KIND, "historical-0") is None
        assert await records.for_key(identifier(c.run_id), "git_delivery", "historical-0") is None
        assert await records.active_for_run(identifier(c.run_id)) is True
        for row in foreign_kind:
            row.status = "completed"
            await records.save(row)
        assert await records.active_for_run(identifier(c.run_id)) is False
        assert await records.active_for_run(identifier(other["id"])) is True
    assert len(c.posts) == 1 and len(await c.journal.sandbox_executions(c.run_id)) == 11
