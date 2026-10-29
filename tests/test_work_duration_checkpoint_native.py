# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""PostgreSQL work-policy and CPv3 controls with owned processes and fictional HTTP.

The SDK transport is controlled; no live provider is contacted. These cases are
separate from retained legacy qualification and do not register a source profile.
"""

import copy
import json
import os
import sys
import time
from types import SimpleNamespace

import pytest
from pydantic_ai import models
from test_agentic_restore_receipt_native import native_restoration
from test_bridge_completion_native import mock_model_requests, prepared

from firefly_forge import checkpoint_work_duration
from firefly_forge.agent_restores import AgentRestores
from firefly_forge.checkpoint_work_duration import checkpoint_payload, selected_work_duration
from firefly_forge.journal import Journal
from firefly_forge.models import RunInput
from firefly_forge.sandbox import SandboxExecutor
from firefly_forge.sandbox_models import SandboxInput
from firefly_forge.work_duration_models import WorkDurationPolicyV2


async def active_implementation(store, run, journal):
    await store.update(run["id"], status="developing", active_task="implement")
    await journal.intent(run["id"], "implement")
    task = next(item for item in (await journal.plan(run["id"]))["tasks"] if item["id"] == "implement")
    assert task["status"] == "running"
    return await store.get_run(run["id"])


def pending_checkpoint(entries):
    return [entry for entry in entries if entry["kind"] == "agent_checkpoint"][-1]


async def test_native_new_admission_freezes_uncapped_policy_across_fresh_store(store, store_factory, tmp_path):
    async with prepared(store, tmp_path, work_duration=WorkDurationPolicyV2()) as (_, _, run, journal, *_):
        before = copy.deepcopy(await store.get_run(run["id"]))
        assert selected_work_duration(before) == WorkDurationPolicyV2()
        assert before["work_duration"] == before["sandbox_snapshot"]["configuration"]["work_duration"]
        frozen = await store_factory(store.path)
        assert await frozen.get_run(run["id"]) == before
        assert Journal.verify_chain(await journal.entries(run["id"]))


async def test_native_uncapped_host_command_survives_legacy_timeout_and_charges_once(store, tmp_path):
    async with prepared(store, tmp_path, work_duration=WorkDurationPolicyV2(), runner_timeout=1) as (
        _,
        _,
        run,
        journal,
        *_,
    ):
        await active_implementation(store, run, journal)
        argv = [sys.executable, "-B", "-c", "import time; time.sleep(1.2); print('OWNED_NATIVE_FINISHED')"]
        started = time.monotonic()
        executor = SandboxExecutor(store, journal)
        first = await executor.execute(run["id"], tmp_path, tmp_path, argv, None, "implement")
        assert time.monotonic() - started > 1 and first.exit_code == 0
        assert first.output.strip() == "OWNED_NATIVE_FINISHED"
        rows = await journal.sandbox_executions(run["id"])
        assert len(rows) == 1 and rows[0]["ordinal"] == 1
        assert rows[0]["request"]["request"]["timeout"] is None
        assert rows[0]["effects"]["start"]["status"] == rows[0]["effects"]["cleanup"]["status"] == "done"
        fresh = SandboxExecutor(store, Journal(store))
        assert await fresh.execute(run["id"], tmp_path, tmp_path, argv, None, "implement", ordinal=1) == first
        assert len(await journal.sandbox_executions(run["id"])) == 1
        assert Journal.verify_chain(await journal.entries(run["id"]))


async def test_native_chosen_host_deadline_drains_process_and_keeps_charged_slot(store, tmp_path):
    async with prepared(store, tmp_path, work_duration=WorkDurationPolicyV2(timeout_seconds=1)) as (
        _,
        _,
        run,
        journal,
        *_,
    ):
        await active_implementation(store, run, journal)
        pidfile = tmp_path / "owned-command.pid"
        argv = [
            sys.executable,
            "-B",
            "-c",
            "import os,time,pathlib; pathlib.Path('owned-command.pid').write_text(str(os.getpid())); time.sleep(30)",
        ]
        with pytest.raises(TimeoutError):
            await SandboxExecutor(store, journal).execute(run["id"], tmp_path, tmp_path, argv, None, "implement")
        pid = int(pidfile.read_text())
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
        rows = await journal.sandbox_executions(run["id"])
        assert len(rows) == 1 and rows[0]["request"]["request"]["timeout"] == 1
        assert rows[0]["effects"]["cleanup"]["status"] == "done"
        assert rows[0]["effects"]["start"]["status"] != "done"
        assert Journal.verify_chain(await journal.entries(run["id"]))


async def test_native_uncapped_cpv3_restores_original_response_once_through_fresh_service(store, tmp_path, monkeypatch):
    async with native_restoration(store, tmp_path, monkeypatch, work_duration=WorkDurationPolicyV2()) as env:
        prefix = await env.journal.entries(env.run_id)
        record = pending_checkpoint(prefix)
        binding = checkpoint_payload(record["payload"]).invocation
        assert record["payload"]["version"] == 3 and binding.work_deadline_ms is None
        assert binding.work_duration == WorkDurationPolicyV2()
        before = await store.get_run(env.run_id)
        assert before["status"] == "needs_reconciliation"
        for wire in env.posts:
            assert json.loads(wire)["bridge"]["workDuration"] == {"version": 1, "timeoutMs": None}
        fresh = AgentRestores(store, env.requests, env.engine)
        with models.override_allow_model_requests(False), mock_model_requests(env.model):
            await fresh.execute(env.request.request_id)
        ticket = await env.requests.get(env.request.request_id)
        current = await store.get_run(env.run_id)
        assert ticket["status"] == "completed" and current["status"] == "succeeded"
        assert current["attempt"] == before["attempt"] and current["repair_budget"] == before["repair_budget"]
        assert current["work_duration"] == before["work_duration"]
        assert (env.workspace / "service.py").read_text() == "VALUE = 1\n" and len(env.posts) == 2
        after = await env.journal.entries(env.run_id)
        assert after[: len(prefix)] == prefix and Journal.verify_chain(after)
        assert (
            sum(e["kind"] == "agent_consume" and e["idempotency_key"] == record["idempotency_key"] for e in after) == 1
        )
        await fresh.execute(env.request.request_id)
        assert len(env.posts) == 2 and await env.journal.entries(env.run_id) == after


async def test_native_chosen_cpv3_deadline_survives_restart_and_cannot_be_renewed(store, tmp_path, monkeypatch):
    async with native_restoration(
        store, tmp_path, monkeypatch, work_duration=WorkDurationPolicyV2(timeout_seconds=120)
    ) as env:
        before = await env.journal.entries(env.run_id)
        record = pending_checkpoint(before)
        binding = checkpoint_payload(record["payload"]).invocation
        assert binding.work_deadline_ms == binding.work_started_at_ms + 120000
        monkeypatch.setattr(
            checkpoint_work_duration, "time", SimpleNamespace(time=lambda: binding.work_deadline_ms / 1000 + 1)
        )
        fresh = AgentRestores(store, env.requests, env.engine)
        with models.override_allow_model_requests(False), mock_model_requests(env.model):
            await fresh.execute(env.request.request_id)
        assert (await env.requests.get(env.request.request_id))["status"] == "failed"
        after = await env.journal.entries(env.run_id)
        assert after[: len(before)] == before and len(env.posts) == 2
        assert not any(
            e["kind"] == "agent_consume" and e["idempotency_key"] == record["idempotency_key"] for e in after
        )
        async with store._unit():
            retained = await fresh.checkpoints._record(env.run_id, record["idempotency_key"], "agent_checkpoint")
        assert retained == record and Journal.verify_chain(after)


async def test_native_cpv3_policy_replacement_refuses_before_consume_or_model(store, tmp_path, monkeypatch):
    async with native_restoration(store, tmp_path, monkeypatch, work_duration=WorkDurationPolicyV2()) as env:
        before = await env.journal.entries(env.run_id)
        record = pending_checkpoint(before)
        await store.update(env.run_id, work_duration=WorkDurationPolicyV2(timeout_seconds=1).model_dump())
        with models.override_allow_model_requests(False), mock_model_requests(env.model):
            await AgentRestores(store, env.requests, env.engine).execute(env.request.request_id)
        assert (await env.requests.get(env.request.request_id))["status"] == "failed"
        after = await env.journal.entries(env.run_id)
        assert len(env.posts) == 2 and after[: len(before)] == before
        assert not any(
            e["kind"] == "agent_consume" and e["idempotency_key"] == record["idempotency_key"] for e in after
        )


async def test_native_new_uncapped_docker_refuses_before_run_or_command_admission(store, tmp_path):
    async with prepared(store, tmp_path) as (_, _, legacy, journal, *_):
        before_runs = await store.runs()
        before_legacy = await store.get_run(legacy["id"])
        before_wal = await journal.entries(legacy["id"])
        with pytest.raises(ValueError, match="watchdog"):
            await store.create_run(
                RunInput(
                    repository_id=legacy["repository_id"],
                    project_id=legacy["project_id"],
                    component_id="api",
                    brief="Unsupported fictional uncapped Docker work",
                    sandbox=SandboxInput(mode="docker", image="fixture/worker@sha256:" + "a" * 64, context="fixture"),
                )
            )
        assert await store.runs() == before_runs
        assert await store.get_run(legacy["id"]) == before_legacy
        assert await journal.entries(legacy["id"]) == before_wal
        assert "work_duration" not in before_legacy
        assert await journal.sandbox_executions(legacy["id"]) == []


async def test_native_legacy_cpv2_bytes_stay_unmodified_after_selected_new_work(store, tmp_path, monkeypatch):
    async with native_restoration(store, tmp_path, monkeypatch) as env:
        legacy_id = env.run_id
        legacy = await store.get_run(legacy_id)
        records = await env.journal.entries(legacy_id)
        checkpoints = [e for e in records if e["kind"] == "agent_checkpoint"]
        assert checkpoints and all(e["payload"]["version"] == 2 for e in checkpoints)
        assert "work_duration" not in legacy
        assert "workDuration" not in json.loads(env.posts[0]).get("bridge", {})
        async with prepared(store, tmp_path / "new", work_duration=WorkDurationPolicyV2()) as (_, _, selected, *_):
            assert selected_work_duration(selected) == WorkDurationPolicyV2()
        assert await store.get_run(legacy_id) == legacy
        assert await env.journal.entries(legacy_id) == records
