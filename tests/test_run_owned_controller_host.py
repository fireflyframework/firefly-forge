# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Real controller coordination with memory storage and an explicit authority oracle."""

import asyncio
from types import SimpleNamespace
from uuid import UUID

import pytest
from test_run_owned_authority_host import case

from firefly_forge.runner_pool_execution import RunnerPoolExecutions
from firefly_forge.runner_run_liveness import RunRenewalV1


def renewal(ack):
    return RunRenewalV1.model_validate({**ack, "protocol": "forge-run-renewal-v1", "sequence": ack["sequence"] + 1})


def controller(monkeypatch, *, records=None, lease_seconds=30, work_cap=864000):
    invocation, fence, run, _, _ = case(lease_seconds=lease_seconds, work_cap=work_cap)
    records = {} if records is None else records
    effects, cancellations, quarantine = [], [], []
    clock, permission = {"now": 0.0}, {"active": True, "revoked": set()}

    class Pools:
        async def check_factory_fence(self, selected):
            assert selected == fence

        async def observe(self, profile, identity):
            if identity not in records:
                raise KeyError(identity)
            return records[identity]

        async def submit(self, profile, request, **kwargs):
            effects.append("submit")
            records[request.execution_id] = {
                "id": "e" * 32,
                "status": "queued",
                "owner_id": None,
                "epoch": 0,
                "request": request.model_dump(mode="json"),
                "receipt": None,
            }
            return records[request.execution_id]

        async def quarantine(self, profile, identity, owner, epoch, receipt):
            quarantine.append(receipt)

    async def daemon():
        effects.append("daemon")

    def prepare(*args):
        effects.append("prepare")

    def retain(*args):
        effects.append("retain")

    executor = RunnerPoolExecutions(
        Pools(),
        SimpleNamespace(prepare=prepare, retain=retain),
        SimpleNamespace(
            authority=SimpleNamespace(profile=run["sandbox_snapshot"]["configuration"]["profile"]), verify_daemon=daemon
        ),
    )
    from firefly_forge.runner_pool_models import RunnerPoolProfile

    executor.dispatcher.authority.profile = RunnerPoolProfile.model_validate(executor.dispatcher.authority.profile)

    async def authorize(*args):
        if not permission["active"] or args[0].execution_id in permission["revoked"]:
            raise ValueError("Controlled run actor or stage revoked")

    async def pump():
        pass

    async def cancel(identity, selected):
        cancellations.append((identity, selected))

    monkeypatch.setattr(executor, "pump", pump)
    monkeypatch.setattr(executor, "cancel", cancel)
    return executor, invocation, fence, clock, permission, records, effects, cancellations, quarantine, authorize


async def initialized(monkeypatch, *, lease_seconds=30, work_cap=864000):
    result = controller(monkeypatch, lease_seconds=lease_seconds, work_cap=work_cap)
    executor, _, _, clock, *_, authorize = result
    assert hasattr(executor, "run_leases") and hasattr(executor, "renew_run"), "Run-owned controller renewal is missing"
    monkeypatch.setattr(executor, "run_authority", SimpleNamespace(authorize=authorize))
    monkeypatch.setattr(executor, "run_clock", lambda: clock["now"])
    return result


async def test_revoking_one_run_closes_its_lease_despite_healthy_factory_ownership(monkeypatch):
    executor, invocation, fence, clock, permission, _, _, cancellations, _, _ = await initialized(monkeypatch)
    accepted = await executor.accept(invocation, fence, b"fixture", b"")
    ack = accepted["run_lease"]
    assert ack["worker_epoch"] == invocation.run_liveness.worker_epoch
    clock["now"] = 20
    ack = await executor.renew_run(invocation, fence, renewal(ack))
    assert ack["sequence"] == 1
    permission["active"] = False
    with pytest.raises(ValueError):
        await executor.renew_run(invocation, fence, renewal(ack))
    assert executor.run_leases[invocation.execution_id].closed
    assert cancellations == [(invocation.execution_id, fence)]


async def test_fresh_controller_never_reinitializes_or_redispatches_durable_prior_run(monkeypatch):
    executor, invocation, fence, _, _, records, _, _, _, _ = await initialized(monkeypatch)
    await executor.accept(invocation, fence, b"fixture", b"")
    new, *rest = controller(monkeypatch, records=records)
    effects = rest[5]
    with pytest.raises(RuntimeError, match="reconciliation"):
        await new.accept(invocation, fence, b"fixture", b"")
    assert effects == []


async def test_expired_worker_renewal_cannot_be_replaced_by_global_factory_health(monkeypatch):
    executor, invocation, fence, clock, _, _, _, cancellations, _, _ = await initialized(monkeypatch)
    ack = (await executor.accept(invocation, fence, b"fixture", b""))["run_lease"]
    clock["now"] = 30
    with pytest.raises(ValueError):
        await executor.renew_run(invocation, fence, renewal(ack))
    assert cancellations == [(invocation.execution_id, fence)]


async def test_stale_or_foreign_renewal_cannot_cancel_the_legitimate_run(monkeypatch):
    executor, invocation, fence, _, _, _, _, cancellations, _, _ = await initialized(monkeypatch)
    ack = (await executor.accept(invocation, fence, b"fixture", b""))["run_lease"]
    request = renewal(ack).model_copy(update={"worker_epoch": "f" * 32})
    with pytest.raises(ValueError):
        await executor.renew_run(invocation, fence, request)
    assert not executor.run_leases[invocation.execution_id].closed and cancellations == []


async def test_one_revoked_run_cannot_stop_another_healthy_run_on_the_same_factory(monkeypatch):
    executor, first, fence, clock, permission, _, _, cancellations, _, _ = await initialized(monkeypatch)
    second = first.model_copy(update={"execution_id": "2" * 32, "run_id": UUID("3" * 32)})
    a = (await executor.accept(first, fence, b"fixture", b""))["run_lease"]
    b = (await executor.accept(second, fence, b"fixture", b""))["run_lease"]
    permission["revoked"].add(first.execution_id)
    clock["now"] = 20
    with pytest.raises(ValueError):
        await executor.renew_run(first, fence, renewal(a))
    assert (await executor.renew_run(second, fence, renewal(b)))["sequence"] == 1
    assert cancellations == [(first.execution_id, fence)]
    assert not executor.run_leases[second.execution_id].closed


async def test_known_finished_work_does_not_race_the_workers_final_renewal_readback(monkeypatch):
    executor, invocation, fence, _, _, records, _, _, _, _ = await initialized(monkeypatch)
    ack = (await executor.accept(invocation, fence, b"fixture", b""))["run_lease"]
    claim = records[invocation.execution_id]
    claim.update(status="leased", owner_id=executor.owner, epoch=1)

    async def complete(*args):
        claim["status"] = "finished"

    async def heartbeat(*args):
        await asyncio.Event().wait()

    monkeypatch.setattr(executor, "_execute", complete)
    monkeypatch.setattr(executor, "_renew", heartbeat)
    await executor.execute_claim(claim, fence, pump_after=False)
    assert (await executor.renew_run(invocation, fence, renewal(ack)))["sequence"] == 1


@pytest.mark.parametrize("work_cap", [None, 864000])
async def test_restarted_controller_quarantines_an_already_claimed_run_before_dispatch(monkeypatch, work_cap):
    original, invocation, fence, _, _, records, *_ = await initialized(monkeypatch, work_cap=work_cap)
    await original.accept(invocation, fence, b"fixture", b"")
    restarted, _, _, _, _, _, effects, _, quarantined, authorize = controller(
        monkeypatch, records=records, work_cap=work_cap
    )
    monkeypatch.setattr(restarted, "run_authority", SimpleNamespace(authorize=authorize))
    claim = records[invocation.execution_id]
    claim.update(status="leased", owner_id=restarted.owner, epoch=2)

    async def unexpected(*args):
        raise AssertionError("A restarted owner must not dispatch the unknown command")

    monkeypatch.setattr(restarted, "_execute", unexpected)
    await restarted.execute_claim(claim, fence, pump_after=False)
    assert effects == []
    assert quarantined == [{"reason": "run_owned_authority_unavailable"}]
    assert restarted.run_leases == {}
