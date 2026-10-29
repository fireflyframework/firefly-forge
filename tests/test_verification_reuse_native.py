# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Fictional PostgreSQL lifecycle with real confined build checks and one fresh DB check."""

import copy
import json
import os
import sys
import traceback
from functools import wraps
from uuid import uuid4

import httpx2
import pytest
from final_outcome_native_fixtures import enable_final, final_input, seed_consumed_final
from test_agentic_checkpoint_activation_host import result
from test_bridge_completion_native import mock_model_requests

from firefly_forge.agent_checkpoints import AgentCheckpoints
from firefly_forge.journal import Journal
from firefly_forge.runner_backends import HostRunnerBackend
from firefly_forge.verification_handoffs import VerificationHandoffs
from firefly_forge.verification_models import VerificationAdoption
from firefly_forge.workspace_tools import WorkspaceTools

pytest_plugins = ["test_invalid_read_continuity_native"]


async def queued_build(c, monkeypatch, *, mixed, expect_held=False, failed_check=False):
    python = sys.executable
    commands = [
        dict(
            argv=[python, "-B", "-m", "ruff", "check", "--isolated", "--no-cache", "--select", rule, "existing.py"],
            timeout=20,
        )
        for rule in ("E", "F", "W", "I", "B", "UP", "SIM")
    ]
    if mixed:
        folder = c.store.path.parent / "fresh-database-checks"
        folder.mkdir(parents=True)
        (folder / "test_owned_database.py").write_text("""import asyncio,json,os
from pathlib import Path
import asyncpg

def test_current_database_is_fresh():
    async def check():
        settings=json.loads(Path(os.environ['QA_REUSE_INPUT_FILE']).read_text())
        connection=await asyncpg.connect(**settings)
        try: assert await connection.fetchval('SELECT 1') == 1
        finally: await connection.close()
    asyncio.run(check())
""")
        from sqlalchemy.engine import make_url

        url = make_url(os.environ["FORGE_TEST_DATABASE_URL"])
        config = c.store.path.parent / "qa-input.private.json"
        config.write_text(
            json.dumps(
                dict(host=url.host, port=url.port, user=url.username, password=url.password, database=url.database)
            )
        )
        config.chmod(0o600)
        monkeypatch.setenv("QA_REUSE_INPUT_FILE", str(config))
        commands[0] = dict(argv=[python, "-B", "-m", "pytest", "-p", "no:cacheprovider", "-q", str(folder)], timeout=20)
    if failed_check:
        commands[-1]["argv"][-2] = "INVALID_RULE"
    await c.store.update(c.run_id, commands=commands, repair_budget=1)
    failed = await seed_consumed_final(c, monkeypatch)
    enable_final(c)
    service = c.service.final_outcomes
    await service.request(
        c.run_id, failed["id"], uuid4().hex, final_input(await service.candidate(c.run_id, failed["id"]))
    )
    await c.service.execute(failed["id"])
    before = await c.store.get_run(c.run_id)
    assert before["status"] == "paused"
    assert len(await c.journal.sandbox_executions(c.run_id)) == 11
    handoff = VerificationHandoffs(c.store, c.journal, c.engine)
    candidate = await handoff.candidates(c.run_id, "bridge")
    selected = candidate["reusable_build_candidates"]
    assert len(selected) == (6 if mixed else 7)
    body = VerificationAdoption(
        request_id=uuid4().hex,
        agent="bridge",
        **{
            k: candidate[k]
            for k in ("pending_request_digest", "source_digest", "commands_digest", "policy_digest", "provider_digest")
        },
        reusable_build_checks=[
            dict(
                command_digest=row["command_digest"],
                purpose="source_build",
                independent_of_time_and_external_state=True,
            )
            for row in selected
        ],
    )
    await handoff.adopt(c.run_id, body)
    actual = HostRunnerBackend()
    calls, holds = [], []

    class Backend:
        async def run(self, config, argv, cwd, timeout, output_limit):
            calls.append(list(argv))
            return await actual.run(config, argv, cwd, timeout, output_limit)

    monkeypatch.setattr(c.engine.sandbox.backends, "select", lambda _: Backend())
    hold = c.journal.verification_hold

    async def held(*args, **kwargs):
        holds.append("".join(traceback.format_exception(*sys.exc_info())))
        return await hold(*args, **kwargs)

    monkeypatch.setattr(c.journal, "verification_hold", held)
    await handoff.execute(c.run_id)
    queued = await c.store.get_run(c.run_id)
    if expect_held:
        assert queued["verification_handoff"]["status"] == "held", holds
        return calls, commands, copy.deepcopy(queued), copy.deepcopy(await c.journal.entries(c.run_id))
    assert queued["status"] == "queued" and queued["verification_handoff"]["status"] == "completed", holds
    assert len(calls) == 7 and len(await c.journal.sandbox_executions(c.run_id)) == 18
    assert all(argv[0] == "/usr/bin/sandbox-exec" for argv in calls[(1 if mixed else 0) :])
    observed = [r["effects"]["start"]["evidence"] for r in await c.journal.sandbox_executions(c.run_id)]
    assert sum(e["exit_code"] != 0 for e in observed) == int(failed_check), [e for e in observed if e["exit_code"] != 0]
    assert queued["verification_handoff"]["failed_commands"] == int(failed_check)
    return calls, commands, copy.deepcopy(queued), copy.deepcopy(await c.journal.entries(c.run_id))


@pytest.mark.skipif(sys.platform != "darwin", reason="The current confined transport is Darwin host-only")
@pytest.mark.parametrize("mixed", [False, True])
async def test_native_verification_reuses_only_scoped_checks_without_resetting_original_budget(
    native_continuity, monkeypatch, mixed
):
    c = native_continuity
    calls, commands, queued, prefix = await queued_build(c, monkeypatch, mixed=mixed)
    provider = c.engine.provider_factory("bridge")
    model = provider.model()
    transport = model.client._client._transport
    posts, consumed = [], []
    old_consumes = {e["idempotency_key"] for e in prefix if e["kind"] == "agent_consume"}
    consume = AgentCheckpoints._consume

    def respond(request):
        if request.method == "GET":
            return transport.handler(request)
        posts.append(request.content)
        assert len(posts) == 1
        return httpx2.Response(
            200,
            json=result(
                json.dumps(
                    dict(summary="Retained checks reviewed; source unchanged", worker_feedback=None, qa_report=None)
                )
            ),
        )

    @wraps(consume)
    async def consume_new(self, run, intent, body, current, reader):
        assert intent["idempotency_key"] not in old_consumes
        consumed.append(intent["idempotency_key"])
        return await consume(self, run, intent, body, current, reader)

    def forbid(original):
        @wraps(original)
        async def no_old_callbacks(*args, **kwargs):
            raise AssertionError("Prior source callbacks cannot replay")

        return no_old_callbacks

    monkeypatch.setattr(AgentCheckpoints, "_consume", consume_new)
    monkeypatch.setattr(WorkspaceTools, "read_file", forbid(WorkspaceTools.read_file))
    monkeypatch.setattr(WorkspaceTools, "write_file", forbid(WorkspaceTools.write_file))
    monkeypatch.setattr(model.client._client, "_transport", httpx2.MockTransport(respond))
    with mock_model_requests(model):
        await c.engine.execute_run(c.run_id)
    final = await c.store.get_run(c.run_id)
    assert final["status"] == "succeeded", final.get("error")
    assert len(posts) == len(consumed) == 1 and final["attempt"] == final["implementation_rounds"] == 3
    assert final["repair_budget"] == queued["repair_budget"] == 1
    assert final["sandbox_snapshot"] == queued["sandbox_snapshot"] and final["commands"] == commands
    rows = await c.journal.sandbox_executions(c.run_id)
    assert len(rows) == (19 if mixed else 18)
    assert len(calls) == (8 if mixed else 7)
    fresh = [r for r in rows if r["task_id"] == "verify"]
    assert len(fresh) == (1 if mixed else 0)
    if mixed:
        assert fresh[0]["ordinal"] == 1 and calls[-1] == commands[0]["argv"]
    entries = await c.journal.entries(c.run_id)
    assert entries[: len(prefix)] == prefix and Journal.verify_chain(entries)
    intents = [e for e in entries if e["kind"] == "verification_reuse_intent"]
    receipts = [e for e in entries if e["kind"] == "verification_reuse_receipt"]
    assert len(intents) == len(receipts) == 1
    assert receipts[0]["payload"]["intent_digest"] == intents[0]["digest"]
    assert receipts[0]["payload"]["new_execution_count"] == 0
    reused = [r for r in final["final_verification"] if r.get("execution") == "reused"]
    assert len(reused) == (6 if mixed else 7)
    assert len(final["final_verification"]) == 7
    for check in reused:
        original = next(e for e in prefix if e["digest"] == check["source_record_digest"])
        assert check["created_at"] == original["created_at"] and check["attempt"] == 2
        assert check["verification_reuse_intent_digest"] == intents[0]["digest"]
        assert check in final["verification"]
    assert sum(e["kind"] == "agent_consume" for e in entries) == len(old_consumes) + 1
    assert final["usage"]["files_written"] == [] and c.checkpoint_writes == [("existing.py", "VALUE = 2\n")]
