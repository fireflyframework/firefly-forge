# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Owned-operation work caps never become renewable leases or publication races."""

import asyncio
import time

import pytest
from test_tool_operation_policy_host import value

from firefly_forge.tool_operation_models import OwnedToolOperationV2
from firefly_forge.tool_operations import OwnedToolSupervisor, ToolOwnershipLost, ToolWorkTimedOut


async def test_uncapped_operation_and_result_admission_outlive_multiple_owner_renewals():
    ticks = []

    async def renew():
        ticks.append(1)

    scope = OwnedToolSupervisor(
        OwnedToolOperationV2.model_validate(value()), renew, started_monotonic=time.monotonic(), interval=0.01
    )

    async def work():
        await asyncio.sleep(0.05)
        async with scope.finalizing():
            await asyncio.sleep(0)
        return {"result": "known"}

    assert await scope.run(work) == {"result": "known"}
    assert len(ticks) >= 3 and scope.finished


async def test_delayed_callback_uses_remaining_sealed_work_cap_instead_of_fresh_timeout():
    entered = []

    async def renew():
        pass

    scope = OwnedToolSupervisor(
        OwnedToolOperationV2.model_validate(value(1)), renew, started_monotonic=time.monotonic() - 0.95, interval=0.01
    )

    async def work():
        entered.append(True)
        await asyncio.sleep(0.2)

    with pytest.raises(ToolWorkTimedOut):
        await scope.run(work)
    assert entered == [True] and not scope.finished


async def test_expired_chosen_work_never_enters_callback_or_renews_it():
    calls = []

    async def renew():
        calls.append("renew")

    async def work():
        calls.append("work")

    scope = OwnedToolSupervisor(
        OwnedToolOperationV2.model_validate(value(1)), renew, started_monotonic=time.monotonic() - 2
    )
    with pytest.raises(ToolWorkTimedOut):
        await scope.run(work)
    assert calls == []


async def test_owner_failure_cancels_callback_without_result_or_redispatch():
    calls = []

    async def renew():
        calls.append("renew")
        if len(calls) > 2:
            raise ValueError("revoked")

    async def work():
        calls.append("work")
        try:
            await asyncio.Event().wait()
        finally:
            calls.append("drained")

    scope = OwnedToolSupervisor(
        OwnedToolOperationV2.model_validate(value()), renew, started_monotonic=time.monotonic(), interval=0.01
    )
    with pytest.raises(ToolOwnershipLost):
        await scope.run(work)
    assert calls.count("work") == 1 and calls[-1] == "drained" and not scope.finished


async def test_explicit_cancellation_drains_both_operation_tasks_without_known_result():
    entered, drained = asyncio.Event(), asyncio.Event()

    async def renew():
        pass

    async def work():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            drained.set()

    scope = OwnedToolSupervisor(
        OwnedToolOperationV2.model_validate(value()), renew, started_monotonic=time.monotonic(), interval=0.01
    )
    task = asyncio.create_task(scope.run(work))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert drained.is_set() and not scope.finished


async def test_finalization_serializes_renewal_until_native_receipt_settles():
    entered, release = asyncio.Event(), asyncio.Event()
    renewals = []

    async def renew():
        renewals.append(True)

    scope = OwnedToolSupervisor(
        OwnedToolOperationV2.model_validate(value()), renew, started_monotonic=time.monotonic(), interval=0.01
    )

    async def work():
        async with scope.finalizing():
            entered.set()
            await release.wait()
        return "known"

    task = asyncio.create_task(scope.run(work))
    await entered.wait()
    before = len(renewals)
    await asyncio.sleep(0.04)
    assert len(renewals) == before
    release.set()
    assert await task == "known" and scope.finished
