# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""The old paused dispatch remains consumed when an exact budget successor is admitted."""

# ruff: noqa: F401,F811
import asyncio
import copy
from unittest.mock import AsyncMock

import pytest
from test_source_budget_paused_invocation_host import paused_invocation
from test_source_budget_service_host import allowance_request
from test_source_edit_continuations_host import controlled_metadata
from test_workspace_dependency_reconciliation import legacy_workspace
from test_workspace_reconciliation_journal import environment_journal
from test_workspace_reconciliation_service import reconciliation_service

from firefly_forge import source_budget_allowances as module


async def admitted(env, tmp_path, monkeypatch):
    env, parent, path, *_ = await paused_invocation(env, tmp_path, monkeypatch)
    service = module.SourceBudgetReadmissions(env.service)
    review = await service.candidate(
        env.root.name,
        parent["request"]["request_id"],
        path,
        module.SourceBudgetProposal(
            version=1, read_file_lifetime_limit=20, activation_ceiling=5, additional_activations=1
        ),
    )
    receipt = await service.admit(env.root.name, parent["request"]["request_id"], allowance_request(review, path))
    return env, receipt


@pytest.mark.parametrize("status", ["queued", "preparing"])
async def test_sealed_queued_successor_uses_normal_dispatch_without_completing_old_consumed_turn(
    reconciliation_service, tmp_path, monkeypatch, status
):
    env, receipt = await admitted(reconciliation_service, tmp_path, monkeypatch)
    env.store.run.data["status"] = env.store.run.status = status
    env.recoveries.engine.active[env.root.name] = asyncio.current_task()
    before = copy.deepcopy(env.store.run.data)
    task = copy.deepcopy(env.task.data)
    assert before["status"] == status and env.task.attempt == 5
    assert before["provider_recovery"]["status"] == "consumed" and before["provider_recovery"]["attempt"] == 4
    assert await env.recoveries.begin(env.root.name) is None
    assert env.store.run.data == before and env.task.data == task


@pytest.mark.parametrize(
    "change",
    [
        "no_marker",
        "ordinary_parent",
        "current_consumed",
        "unknown",
        "task_key",
        "task_attempt",
        "task_running",
        "run_developing",
        "state_request",
        "policy",
        "expired",
        "unresolved",
    ],
)
async def test_old_consumed_state_never_bypasses_a_missing_or_changed_budget_activation(
    reconciliation_service, tmp_path, monkeypatch, change
):
    env, receipt = await admitted(reconciliation_service, tmp_path, monkeypatch)
    run = env.store.run.data
    if change == "no_marker":
        run.pop("source_edit_continuation")
    elif change == "ordinary_parent":
        run["source_edit_continuation"] = receipt["candidate"]["source_budget_readmission"]["parent_marker"]
    elif change == "current_consumed":
        run["provider_recovery"].update(attempt=5, task_key=env.task.data["idempotency_key"])
    elif change == "unknown":
        run["provider_recovery"]["status"] = "unknown"
    elif change == "task_key":
        env.task.data["idempotency_key"] = "f" * 32
    elif change == "task_attempt":
        env.task.attempt += 1
    elif change == "task_running":
        env.task.status = "running"
    elif change == "run_developing":
        run["status"] = env.store.run.status = "developing"
    elif change == "state_request":
        run["provider_recovery"]["request_id"] = "f" * 32
    elif change == "policy":
        module.write_policy(env.store.configuration, module.SourceBudgetPolicy())
    elif change == "expired":
        monkeypatch.setattr("firefly_forge.source_edit_continuations.clock_ms", lambda: receipt["expires_at_ms"] + 1)
    else:
        env.recoveries.engine.tool_broker.unresolved = AsyncMock(return_value=[{"status": "unknown"}])
    before, task = copy.deepcopy(run), copy.deepcopy(env.task.data)
    with pytest.raises(ValueError):
        await env.recoveries.begin(env.root.name)
    assert run == before and env.task.data == task


@pytest.mark.parametrize("change", ["marker", "task", "state", "policy", "expiry", "authority"])
async def test_budget_successor_rechecks_current_authority_after_awaited_settlement(
    reconciliation_service, tmp_path, monkeypatch, change
):
    env, receipt = await admitted(reconciliation_service, tmp_path, monkeypatch)
    settled = env.recoveries._settled

    async def drift(run, **kwargs):
        await settled(run, **kwargs)
        if change == "marker":
            env.store.run.data["source_edit_continuation"]["receipt_digest"] = "f" * 64
        elif change == "task":
            env.task.attempt += 1
        elif change == "state":
            env.store.run.data["provider_recovery"]["attempt"] = 5
        elif change == "policy":
            module.write_policy(env.store.configuration, module.SourceBudgetPolicy())
        elif change == "expiry":
            monkeypatch.setattr(
                "firefly_forge.source_edit_continuations.clock_ms", lambda: receipt["expires_at_ms"] + 1
            )
        else:
            from firefly_forge.project_access import AccessDenied

            async def revoked(*args):
                raise AccessDenied("Controlled current administrator revoked")

            monkeypatch.setattr("firefly_forge.source_edit_continuations._assert_source_administrator", revoked)

    monkeypatch.setattr(env.recoveries, "_settled", drift)
    from firefly_forge.project_access import AccessDenied

    with pytest.raises((ValueError, AccessDenied)):
        await env.recoveries.begin(env.root.name)


@pytest.mark.parametrize("name", ["provider_recoveries.py", "workspace_reconciliations.py"])
def test_allowance_contract_binds_provider_recovery_dispatch_decision(monkeypatch, name):
    from pathlib import Path

    before = module.admission_contract_digest()
    read = Path.read_bytes

    def changed(path):
        value = read(path)
        return value + b"\n# changed provider recovery authority\n" if path.name == name else value

    monkeypatch.setattr(Path, "read_bytes", changed)
    assert module.admission_contract_digest() != before


async def test_native_store_read_projection_events_do_not_change_persisted_budget_authority(
    reconciliation_service, tmp_path, monkeypatch
):
    env, _ = await admitted(reconciliation_service, tmp_path, monkeypatch)
    read = env.store.get_run

    async def native_get_run(run_id):
        return {**await read(run_id), "events": [{"id": "public-activity", "message": "Retained activity"}]}

    monkeypatch.setattr(env.store, "get_run", native_get_run)
    before = copy.deepcopy(env.store.run.data)
    assert await env.recoveries.begin(env.root.name) is None
    assert env.store.run.data == before


@pytest.mark.parametrize("change", ["policy", "expiry"])
async def test_last_task_read_cannot_outlive_current_budget_authority(
    reconciliation_service, tmp_path, monkeypatch, change
):
    env, receipt = await admitted(reconciliation_service, tmp_path, monkeypatch)
    read = env.journal._task
    seen = []

    async def last_read(*args):
        task = await read(*args)
        seen.append(task.attempt)
        if len(seen) == 3:
            if change == "policy":
                module.write_policy(env.store.configuration, module.SourceBudgetPolicy())
            else:
                monkeypatch.setattr(
                    "firefly_forge.source_edit_continuations.clock_ms", lambda: receipt["expires_at_ms"] + 1
                )
        return task

    monkeypatch.setattr(env.journal, "_task", last_read)
    with pytest.raises(ValueError):
        await env.recoveries.begin(env.root.name)
    assert seen == [5, 5, 5]


@pytest.mark.parametrize("after_wait", [False, True])
async def test_other_live_engine_owner_never_receives_a_budget_dispatch_handoff(
    reconciliation_service, tmp_path, monkeypatch, after_wait
):
    env, _ = await admitted(reconciliation_service, tmp_path, monkeypatch)
    stop = asyncio.Event()
    other = asyncio.create_task(stop.wait())
    settled = env.recoveries._settled

    async def drift(*args, **kwargs):
        await settled(*args, **kwargs)
        env.recoveries.engine.active[env.root.name] = other

    try:
        if after_wait:
            monkeypatch.setattr(env.recoveries, "_settled", drift)
        else:
            env.recoveries.engine.active[env.root.name] = other
        with pytest.raises(ValueError):
            await env.recoveries.begin(env.root.name)
    finally:
        stop.set()
        await other


async def test_begin_enters_owned_unit_before_any_current_actor_row_lock(reconciliation_service, tmp_path, monkeypatch):
    from contextlib import asynccontextmanager

    env, _ = await admitted(reconciliation_service, tmp_path, monkeypatch)
    owned = env.store.source_budget_admission_unit
    locked = env.recoveries._locked
    order = []

    @asynccontextmanager
    async def admission_unit():
        order.append("owner")
        async with owned() as value:
            yield value
        order.append("settled")

    @asynccontextmanager
    async def rows(*args):
        assert order == ["owner"]
        order.append("rows")
        async with locked(*args) as row:
            yield row

    monkeypatch.setattr(env.store, "source_budget_admission_unit", admission_unit)
    monkeypatch.setattr(env.recoveries, "_locked", rows)
    assert await env.recoveries.begin(env.root.name) is None
    assert order == ["owner", "rows", "settled"]
