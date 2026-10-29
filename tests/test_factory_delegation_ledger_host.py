# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Native family serialization makes ambiguous dispatch spent, never a new allowance."""

import asyncio
import copy
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from firefly_forge.delegation_contract import team_capture
from firefly_forge.delegation_ledger import FamilyLedger
from firefly_forge.journal import Journal, digest

ROOT = "a" * 32


class NativeJournal:
    def __init__(self, run):
        self.run, self.rows, self.lock = run, [], asyncio.Lock()
        self.scalar_drift = False
        self.records = SimpleNamespace(find_for_run=self.native_rows)
        self.store = SimpleNamespace(_unit=self.unit, assert_ownership=self.owned, get_run=self.get_run)

    async def owned(self):
        pass

    async def get_run(self, run_id):
        assert run_id == ROOT
        return copy.deepcopy(self.run)

    @asynccontextmanager
    async def unit(self):
        async with self.lock:
            before = copy.deepcopy(self.rows)
            try:
                yield None
            except BaseException:
                self.rows = before
                raise

    async def native_rows(self, run_id):
        assert run_id.hex == ROOT
        return [
            SimpleNamespace(
                **{
                    **row,
                    "run_id": uuid.UUID(row["run_id"]),
                    "sequence": row["sequence"] + int(self.scalar_drift),
                    "data": copy.deepcopy(row),
                }
            )
            for row in self.rows
        ]

    async def _run(self, run_id):
        return SimpleNamespace(id=uuid.UUID(run_id), data=copy.deepcopy(self.run))

    async def entries(self, run_id):
        return copy.deepcopy(self.rows)

    async def _append(self, run_id, task_id, key, kind, payload):
        value = {
            "run_id": run_id,
            "sequence": len(self.rows) + 1,
            "task_id": task_id,
            "idempotency_key": key,
            "kind": kind,
            "payload": copy.deepcopy(payload),
            "created_at": "controlled",
            "previous_hash": self.rows[-1]["digest"] if self.rows else "0" * 64,
        }
        value["digest"] = digest(value)
        self.rows.append(value)
        return value


def ledger():
    run = {
        "id": ROOT,
        "work_duration": {"protocol": "forge-work-duration-v2", "timeout_seconds": None},
        "sandbox_snapshot": {"configuration": {"command_limit": 2}},
        "tool_policy_snapshot": {"settings": {"calls_limit": 4, "per_tool_limit": 2}},
    }
    run["factory_team"] = team_capture(run)
    journal = NativeJournal(run)
    return FamilyLedger(journal.store, journal), journal


async def test_concurrent_reservations_serialize_one_envelope_and_reinstantiate_from_native_rows():
    manager, journal = ledger()
    results = await asyncio.gather(
        *(manager.reserve(ROOT, ROOT, f"cmd:{i}", "commands") for i in range(3)), return_exceptions=True
    )
    assert sum(not isinstance(item, BaseException) for item in results) == 2
    assert sum(isinstance(item, ValueError) for item in results) == 1
    restored = FamilyLedger(journal.store, journal)
    state = await restored.state(ROOT)
    assert state["charged"]["commands"] == 2 and state["remaining"]["commands"] == 0
    assert Journal.verify_chain(journal.rows)


async def test_same_nonce_is_idempotent_only_for_identical_resource_body():
    manager, journal = ledger()
    first = await manager.reserve(ROOT, ROOT, "model:1", "requests")
    assert await manager.reserve(ROOT, ROOT, "model:1", "requests") == first
    assert len(journal.rows) == 1
    with pytest.raises(ValueError):
        await manager.reserve(ROOT, ROOT, "model:1", "commands")
    assert len(journal.rows) == 1


async def test_unknown_dispatch_survives_restart_and_cannot_be_replayed_or_refunded():
    manager, journal = ledger()
    await manager.reserve(ROOT, ROOT, "model:1", "requests")
    await manager.observe(ROOT, "model:1", "budget_dispatched")
    await manager.observe(ROOT, "model:1", "budget_unknown")
    restored = FamilyLedger(journal.store, journal)
    assert (await restored.state(ROOT))["unknown"]["requests"] == 1
    with pytest.raises(ValueError):
        await restored.observe(ROOT, "model:1", "budget_dispatched")
    with pytest.raises(ValueError):
        await restored.observe(ROOT, "model:1", "budget_settled")
    assert (await restored.state(ROOT))["remaining"]["requests"] == 99


async def test_resource_observation_is_idempotent_but_rolled_back_transaction_does_not_consume():
    manager, journal = ledger()
    await manager.reserve(ROOT, ROOT, "read:1", "tool_calls", tool="read_file")
    await manager.observe(ROOT, "read:1", "budget_dispatched")
    first = await manager.observe(ROOT, "read:1", "budget_settled")
    assert await manager.observe(ROOT, "read:1", "budget_settled") == first
    before = copy.deepcopy(journal.rows)
    with pytest.raises(ValueError):
        await manager.observe(ROOT, "absent", "budget_settled")
    assert journal.rows == before


async def test_modified_native_event_scalar_or_family_payload_refuses_before_reservation():
    manager, journal = ledger()
    await manager.reserve(ROOT, ROOT, "read:1", "requests")
    journal.rows[0]["payload"]["event"]["payload"]["units"] = 0
    with pytest.raises(ValueError):
        await manager.reserve(ROOT, ROOT, "read:2", "requests")
    assert len(journal.rows) == 1


async def test_native_scalar_chain_cannot_be_bypassed_by_self_consistent_json():
    manager, journal = ledger()
    await manager.reserve(ROOT, ROOT, "model:1", "requests")
    journal.scalar_drift = True
    with pytest.raises(ValueError, match="scalar"):
        await manager.reserve(ROOT, ROOT, "model:2", "requests")
    assert len(journal.rows) == 1


async def test_parallel_dispatch_claims_never_authorize_two_callbacks_for_one_charge():
    manager, journal = ledger()
    await manager.reserve(ROOT, ROOT, "model:1", "requests")
    claims = await asyncio.gather(
        *(manager.observe(ROOT, "model:1", "budget_dispatched") for _ in range(2)), return_exceptions=True
    )
    assert sum(not isinstance(item, BaseException) for item in claims) == 1
    assert sum(isinstance(item, ValueError) for item in claims) == 1
    assert (await manager.state(ROOT))["attempted"]["requests"] == 1


@pytest.mark.parametrize("limit", [None, 2**40])
async def test_nullable_and_large_approval_is_one_parallel_family_envelope_after_restart(limit):
    manager, journal = ledger()
    journal.run["factory_team"]["budget"]["requests"] = limit
    async with journal.store._unit():
        await manager.append_locked(ROOT, "child_admitted", "child", {"parent_run_id": ROOT, "child_run_id": "b" * 32})
    await asyncio.gather(
        *(manager.reserve(ROOT, ROOT if i % 2 else "b" * 32, f"model:{i}", "requests") for i in range(4))
    )
    state = await FamilyLedger(journal.store, journal).state(ROOT)
    assert state["charged"]["requests"] == 4
    assert state["remaining"]["requests"] == (None if limit is None else limit - 4)
    assert len(state["members"]) == 2
