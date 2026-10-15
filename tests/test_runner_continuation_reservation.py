# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""A generic queue pump cannot replace explicit continuation authority."""

import asyncio
import copy
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import text
from test_runner_continuations import waiting
from test_runner_pools import invocation
from test_runner_pools import ledger as ledger
from test_runner_pools import profile as pool_profile

from firefly_forge.project_access import CURRENT_PRINCIPAL
from firefly_forge.runner_continuations import RunnerContinuationInput
from firefly_forge.runner_pool_models import FactoryFence
from firefly_forge.runner_pools import RunnerPools


@pytest.mark.parametrize("condition", ["alive", "expired", "refused", "empty", "malformed", "event_only"])
async def test_generic_pump_preserves_every_recorded_continuation(store, tmp_path, condition):
    fixture = await waiting(store, tmp_path)
    manager, controller, run, journal, task, release, context = fixture
    try:
        wal = (await journal.sandbox_executions(run["id"]))[0]
        body = RunnerContinuationInput(request_id=uuid4().hex, execution_id=wal["execution_id"])
        active = manager.worker.waiting[body.execution_id]
        pools = RunnerPools(store.session_provider, store.schema)
        await pools.initialize()
        profile, fence = controller.profile_value, active["scope"]["fence"]
        blockers = []
        if condition == "refused":
            for index in range(profile.slots):
                other = invocation(profile)
                await pools.submit(profile, other, fence=fence)
                blockers.append(await pools.claim(profile, "capacity-" + str(index), fence))
        await pools.submit(profile, active["invocation"], fence=fence)

        async def claim_only(original, owned_fence, request_id, request_digest):
            decision = await pools.claim_continuation(
                profile, "controller-b", owned_fence, original.execution_id, request_id, request_digest
            )
            return {key: value for key, value in decision.items() if key != "claim"}

        controller.continue_invocation = claim_only
        await manager.request(run["id"], body)
        await manager.execute(body.request_id)
        receipt = await manager.get(run["id"], body.request_id)
        assert receipt["status"] == "completed"
        assert receipt["result"]["disposition"] == ("refused" if condition == "refused" else "accepted")
        for blocker in blockers:
            await pools.finish(profile, blocker["id"], blocker["owner_id"], blocker["epoch"], fence, {"drained": True})
        async with pools._unit():
            row = await pools.invocations.execution(body.execution_id)
            if condition not in {"alive", "refused"}:
                row.lease_expires_at = await pools.invocations.database_time() - timedelta(seconds=1)
            data = copy.deepcopy(row.data)
            if condition == "empty":
                data["continuation"] = {}
            elif condition == "malformed":
                data["continuation"] = "untrusted"
            elif condition == "event_only":
                del data["continuation"]
            row.data = data
            await pools.invocations.save(row)
        foreign = invocation(profile)
        await pools.submit(profile, foreign, fence=fence)
        before = await pools.observe(profile, body.execution_id)
        history = await pools.history(profile, body.execution_id)
        immutable_receipt = copy.deepcopy(receipt)
        claimed = await pools.claim(profile, "generic-controller", fence)
        assert claimed is not None and claimed["request"]["execution_id"] == foreign.execution_id
        if condition == "refused":
            another = invocation(profile)
            await pools.submit(profile, another, fence=fence)
            available = await pools.claim(profile, "generic-second-slot", fence)
            assert available is not None and available["request"]["execution_id"] == another.execution_id
        else:
            assert await pools.claim(profile, "generic-second-slot", fence) is None
        assert await pools.observe(profile, body.execution_id) == before
        assert await pools.history(profile, body.execution_id) == history
        assert await manager.get(run["id"], body.request_id) == immutable_receipt
        assert not task.done()
        assert controller.submissions == 1
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        CURRENT_PRINCIPAL.reset(context)


async def test_generic_pump_still_recovers_a_normal_expired_preeffect_lease(ledger):
    pools, fence = ledger
    profile = pool_profile()
    original = invocation(profile)
    await pools.submit(profile, original, fence=fence)
    first = await pools.claim(profile, "normal-a", fence)
    async with pools._unit():
        row = await pools.invocations.execution(original.execution_id)
        row.lease_expires_at = await pools.invocations.database_time() - timedelta(seconds=1)
        await pools.invocations.save(row)
    second = await pools.claim(profile, "normal-b", fence)
    assert second["request"] == original.model_dump(mode="json")
    assert second["epoch"] == first["epoch"] + 1
    assert [event["kind"] for event in await pools.history(profile, original.execution_id)] == [
        "submitted",
        "claimed",
        "lease_expired",
        "claimed",
    ]


@pytest.mark.parametrize("marker", [{}, None, "untrusted"])
async def test_marker_presence_alone_reserves_ambiguous_continuation_without_durable_event(ledger, marker):
    pools, fence = ledger
    profile = pool_profile()
    original = invocation(profile)
    await pools.submit(profile, original, fence=fence)
    await pools.claim(profile, "previous-controller", fence)
    async with pools._unit():
        row = await pools.invocations.execution(original.execution_id)
        row.data = {**row.data, "continuation": marker}
        row.lease_expires_at = await pools.invocations.database_time() - timedelta(seconds=1)
        await pools.invocations.save(row)
    foreign = invocation(profile)
    await pools.submit(profile, foreign, fence=fence)
    history = await pools.history(profile, original.execution_id)
    before = await pools.observe(profile, original.execution_id)
    claimed = await pools.claim(profile, "generic-controller", fence)
    assert claimed["request"]["execution_id"] == foreign.execution_id
    assert await pools.claim(profile, "generic-second-slot", fence) is None
    assert await pools.history(profile, original.execution_id) == history
    assert await pools.observe(profile, original.execution_id) == before


@pytest.mark.parametrize("reserved", [True, False])
async def test_factory_replacement_preserves_reserved_queue_but_cancels_normal_old_owner(
    ledger, store, store_factory, tmp_path, reserved
):
    pools, original_fence = ledger
    profile = pool_profile()
    original = invocation(profile)
    await pools.submit(profile, original, fence=original_fence)
    if reserved:
        async with pools._unit():
            row = await pools.invocations.execution(original.execution_id)
            row.data = {**row.data, "continuation": {}}
            await pools.invocations.save(row)
    before = await pools.observe(profile, original.execution_id)
    history = await pools.history(profile, original.execution_id)
    async with pools._unit():
        row = await pools.invocations.execution(original.execution_id)
        retained = (copy.deepcopy(row.data), row.lease_expires_at, row.admission_expires_at)
    replacement = await store_factory(tmp_path / "replacement" / "factory.db")
    await store.release_ownership()
    await replacement.acquire_ownership()
    async with replacement._unit() as session:
        pid, started = (
            await session.execute(
                text("SELECT pid, backend_start FROM pg_stat_activity WHERE pid=:pid"),
                {"pid": replacement._ownership_pid},
            )
        ).one()
    current_fence = FactoryFence(factory_schema=store.schema, pid=pid, backend_started_at=started)
    assert current_fence != original_fence
    foreign = invocation(profile)
    await pools.submit(profile, foreign, fence=current_fence)
    claimed = await pools.claim(profile, "current-controller", current_fence)
    assert claimed["request"]["execution_id"] == foreign.execution_id
    if reserved:
        assert await pools.observe(profile, original.execution_id) == before
        assert await pools.history(profile, original.execution_id) == history
        async with pools._unit():
            row = await pools.invocations.execution(original.execution_id)
            assert (row.data, row.lease_expires_at, row.admission_expires_at) == retained
    else:
        assert (await pools.observe(profile, original.execution_id))["status"] == "cancelled"
        assert [event["kind"] for event in await pools.history(profile, original.execution_id)] == [
            "submitted",
            "submission_owner_lost",
        ]
