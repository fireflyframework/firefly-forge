# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Capability projection must not consume a queued source recovery or admit tools."""
# ruff: noqa: F401, F811

import copy
from types import SimpleNamespace
from uuid import NAMESPACE_URL, uuid5

import pytest
from test_source_completed_output_service_host import completed_source, request
from test_source_edit_continuations_host import controlled_metadata
from test_workspace_dependency_reconciliation import legacy_workspace
from test_workspace_reconciliation_journal import environment_journal
from test_workspace_reconciliation_service import reconciliation_service

from firefly_forge.project_access import AccessDenied
from firefly_forge.source_edit_continuations import SourceEditInvocation
from firefly_forge.source_edit_models import SourceReadRequest
from firefly_forge.source_result_readmission import SourceCompletedOutputReadmissions
from firefly_forge.tool_broker import ToolReconciliationRequired


async def queued_recovery(env, tmp_path, monkeypatch):
    env, parent, path, *_ = await completed_source(env, tmp_path, monkeypatch)
    service = SourceCompletedOutputReadmissions(env.service)
    review = await service.candidate(env.root.name, parent["request"]["request_id"], path)
    await service.admit(env.root.name, parent["request"]["request_id"], request(review, path))
    key = uuid5(NAMESPACE_URL, f"forge:{env.root.name}:implement:4").hex
    assert env.task.status == "queued" and env.task.data["idempotency_key"] == key
    assert env.store.run.data["provider_recovery"]["status"] == "reserved"
    env.store.run.status = "developing"
    env.store.run.data["status"] = "developing"

    async def rows(run_id):
        assert run_id.hex == env.root.name
        return []

    env.recoveries.engine.tool_broker = SimpleNamespace(records=SimpleNamespace(for_run=rows))
    return env, key


async def test_queued_recovery_capability_projection_keeps_reservation_and_tools_closed(
    reconciliation_service, tmp_path, monkeypatch
):
    env, key = await queued_recovery(reconciliation_service, tmp_path, monkeypatch)
    before = copy.deepcopy(env.store.run.data)
    wal = copy.deepcopy([row.data for row in env.journal.records.rows])
    runtime = SourceEditInvocation(env.recoveries, env.root.name, key)
    result = await runtime.capabilities(before_dispatch=True)
    assert result["total"] == {"used": 0, "limit": 30, "remaining": 30}
    assert env.store.run.data == before and env.task.status == "queued"
    assert [row.data for row in env.journal.records.rows] == wal
    with pytest.raises(ValueError, match="exact source-edit task"):
        await runtime.capabilities()
    with pytest.raises(ValueError, match="exact source-edit task"):
        await runtime.run(SourceReadRequest(tool="list_files", request_id="f" * 32))


@pytest.mark.parametrize("change", ["key", "reservation", "request", "attempt", "actor", "task", "task_request"])
async def test_predispatch_projection_rejects_forged_or_consumed_reservation(
    reconciliation_service, tmp_path, monkeypatch, change
):
    env, key = await queued_recovery(reconciliation_service, tmp_path, monkeypatch)
    reservation = env.store.run.data["provider_recovery"]
    if change == "key":
        key = "f" * 32
    elif change == "reservation":
        reservation["status"] = "consumed"
    elif change == "request":
        reservation["request_id"] = "f" * 32
    elif change == "attempt":
        reservation["attempt"] += 1
    elif change == "actor":
        reservation["actor"] = {**reservation["actor"], "user_id": "f" * 32}
    elif change == "task":
        env.task.status = "running"
    else:
        env.task.data["provider_recovery_id"] = "f" * 32
    with pytest.raises(ValueError):
        await SourceEditInvocation(env.recoveries, env.root.name, key).capabilities(before_dispatch=True)


@pytest.mark.parametrize("change", ["reservation", "task", "identity", "unknown"])
async def test_predispatch_projection_rechecks_after_awaited_tool_history(
    reconciliation_service, tmp_path, monkeypatch, change
):
    env, key = await queued_recovery(reconciliation_service, tmp_path, monkeypatch)
    before = len(env.journal.records.rows)

    async def rows(run_id):
        if change == "reservation":
            env.store.run.data["provider_recovery"]["status"] = "consumed"
        elif change == "task":
            env.task.status = "running"
        elif change == "identity":
            env.identity.enabled = False
        else:
            return [SimpleNamespace(status="unknown", tool="read_file")]
        return []

    env.recoveries.engine.tool_broker.records.for_run = rows
    expected = (
        AccessDenied if change == "identity" else ToolReconciliationRequired if change == "unknown" else ValueError
    )
    with pytest.raises(expected):
        await SourceEditInvocation(env.recoveries, env.root.name, key).capabilities(before_dispatch=True)
    assert len(env.journal.records.rows) == before


async def test_after_dispatch_only_strict_projection_is_valid(reconciliation_service, tmp_path, monkeypatch):
    env, key = await queued_recovery(reconciliation_service, tmp_path, monkeypatch)
    env.task.status = "running"
    env.store.run.data["provider_recovery"]["status"] = "consumed"
    runtime = SourceEditInvocation(env.recoveries, env.root.name, key)
    assert (await runtime.capabilities())["total"]["used"] == 0
    with pytest.raises(ValueError, match="queued source recovery"):
        await runtime.capabilities(before_dispatch=True)
