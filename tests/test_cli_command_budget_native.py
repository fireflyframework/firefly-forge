# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Real SQL archive and native fences; CLI messages are controlled public fixtures."""

import json
from contextlib import asynccontextmanager

import pytest
from test_cli_command_budget_guard import transcript
from test_workspace_reconciliation_native import admission, retained_environment_refusal
from worker_reply_fixtures import authenticated_operator

from firefly_forge.cli_command_budget import retained_command_observation


async def retain_cli(store, run_id, count=21):
    for event in transcript(count):
        await store.event(run_id, "stdout", json.dumps(event))


async def test_native_twenty_one_cli_and_seven_governed_calls_hold_before_environment_intent(
    store, tmp_path, monkeypatch
):
    async with authenticated_operator(store):
        env = await retained_environment_refusal(store, tmp_path, monkeypatch)
        before = (env.workspace / ".git/index").read_bytes()
        wal = await env.journal.entries(env.run_id)
        await retain_cli(store, env.run_id)
        assert await retained_command_observation(store, env.run_id) == {"cli_commands": 21, "cli_invocations": 1}
        with pytest.raises(ValueError, match=r"CLI=21.*Sandbox=7.*final=7.*remaining=7"):
            await env.service.candidate(env.run_id)
        assert (env.workspace / ".git/index").read_bytes() == before
        assert await env.journal.entries(env.run_id) == wal
        assert (await store.get_run(env.run_id))["status"] == "failed"
        assert len(env.calls) == 1 and len(env.checks) == 7


async def test_native_archive_change_after_committed_intent_holds_before_filesystem_effect(
    store, tmp_path, monkeypatch
):
    async with authenticated_operator(store):
        env = await retained_environment_refusal(store, tmp_path, monkeypatch)
        body = admission(await env.service.candidate(env.run_id))
        locked = env.service.recoveries._locked
        injected = False

        @asynccontextmanager
        async def archive_after_commit(*args, **kwargs):
            nonlocal injected
            async with locked(*args, **kwargs) as row:
                yield row
            if not injected:
                injected = True
                await retain_cli(store, env.run_id)

        monkeypatch.setattr(env.service.recoveries, "_locked", archive_after_commit)
        before = (env.workspace / ".git/index").read_bytes()
        with pytest.raises(ValueError, match="Unmetered retained CLI"):
            await env.service.admit(env.run_id, body)
        assert (env.workspace / ".git/index").read_bytes() == before
        assert (await env.service.get(env.run_id, body.request_id))["status"] == "needs_reconciliation"
        with pytest.raises(ValueError, match="unknown"):
            await env.service.admit(env.run_id, body)
        assert (await store.get_run(env.run_id))["status"] == "failed" and len(env.calls) == 1


@pytest.mark.parametrize("boundary", ["dispatch", "launch"])
async def test_native_reconciled_provider_fence_refuses_unmetered_cli_without_new_dispatch(
    store, tmp_path, monkeypatch, boundary
):
    async with authenticated_operator(store):
        env = await retained_environment_refusal(store, tmp_path, monkeypatch)
        body = admission(await env.service.candidate(env.run_id))
        await env.service.admit(env.run_id, body)
        await store.update(env.run_id, status="developing")
        run = await store.get_run(env.run_id)
        intent = await env.engine._begin_task(
            env.run_id, "implement", env.workspace, store.configuration.load_snapshot(run["config_revision"])
        )
        key = intent["idempotency_key"]
        recovery = env.service.recoveries
        if boundary == "launch":
            await recovery.normal_dispatch(env.run_id, key)
        wal = await env.journal.entries(env.run_id)
        await retain_cli(store, env.run_id)
        with pytest.raises(ValueError, match="Unmetered retained CLI"):
            if boundary == "dispatch":
                await recovery.normal_dispatch(env.run_id, key)
            else:
                await recovery.assert_launch(env.run_id, key)
        assert await env.journal.entries(env.run_id) == wal
        assert len(env.calls) == 1 and len(env.checks) == 7
