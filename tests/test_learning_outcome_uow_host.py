# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Actual Git/scanner and PyFly units expose the learning preparation boundary."""

import asyncio
import copy
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID

import pytest
from pyfly.data.transaction import TransactionTemplate, current_unit_of_work
from test_engine_diff_artifacts import snapshot_case as snapshot_case
from test_git_delivery_remote_uow_host import MemoryManager

from firefly_forge import learning as module
from firefly_forge.journal import digest
from firefly_forge.learning_models import LearningPolicy
from firefly_forge.process import checked
from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied, ProjectAccess


@pytest.fixture
async def outcome_case(snapshot_case, monkeypatch):
    c = snapshot_case
    (c.root / "implementation.py").write_text("public_value = 42\n")
    await checked(["git", "add", "implementation.py"], c.root)
    tree = await checked(["git", "write-tree"], c.root)
    policy = {"version": 1, "revision": None, "settings": LearningPolicy().model_dump()}
    run = c.state["run"]
    run.update(
        repository_id="2" * 32,
        status="succeeded",
        checkpoint_revision=c.base,
        source_tree=tree,
        backend="none",
        frontend="none",
        brief="Observe the public check",
        final_verification=[{"argv": ["public-check"], "exit_code": 0}],
        learning_snapshot={**policy, "digest": digest(policy)},
    )
    c.state.update(records=[], events=[], projections=[])
    manager = MemoryManager(c.state)
    c.manager, c.revoked, c.effect = manager, False, None
    c.unit_entries = 0

    async def execute(_statement):
        return SimpleNamespace(mappings=lambda: [])

    @asynccontextmanager
    async def unit(**_kwargs):
        # The final acquisition can await and change the captured authority/cut.
        c.unit_entries += 1
        if c.effect and current_unit_of_work() is None and c.prepared:
            effect, c.effect = c.effect, None
            effect()
        async with TransactionTemplate(manager).transaction():
            yield SimpleNamespace(execute=execute)

    c.store._unit = unit
    c.prepared = False

    async def authorize(_self, current):
        assert current_unit_of_work() is not None
        if c.revoked:
            raise AccessDenied("Public fixture execution permission revoked")

    monkeypatch.setattr(ProjectAccess, "authorize_execution", authorize)
    receipts = []
    for task, payload in [
        ("verify", {"checks": run["final_verification"], "source_tree": tree}),
        ("checkpoint", {"source_tree": tree, "checkpoint_revision": c.base}),
    ]:
        item = {
            "kind": "receipt",
            "task_id": task,
            "payload": payload,
            "idempotency_key": task,
            "sequence": len(receipts) + 1,
            "previous_hash": receipts[-1]["digest"] if receipts else "0" * 64,
        }
        item["digest"] = digest(item)
        receipts.append(item)
    c.receipts = receipts

    async def getrun(_id):
        assert current_unit_of_work() is not None
        return SimpleNamespace(id=UUID(run["id"]), repository_id=UUID(run["repository_id"]), data=run)

    async def entries(_id):
        assert current_unit_of_work() is not None
        return copy.deepcopy(c.receipts)

    async def append(*args):
        assert current_unit_of_work() is not None
        c.state["events"].append(args)

    journal = SimpleNamespace(_run=getrun, entries=entries, _append=append)

    class Records:
        async def for_run(self, _id):
            assert current_unit_of_work() is not None
            if effect := getattr(c, "late_effect", None):
                c.late_effect = None
                await asyncio.sleep(0)
                effect()
            return c.state["records"]

        async def database_time(self):
            return datetime(2026, 10, 24, tzinfo=UTC)

        async def save(self, row):
            assert current_unit_of_work() is not None
            c.state["records"].append(row)

    class Projections:
        async def save(self, row):
            assert current_unit_of_work() is not None
            c.state["projections"].append(row)

    ledger = module.LearningLedger(c.store, journal, records=Records(), projections=Projections())
    ledger.engine = c.engine

    async def inspect(*args):
        assert current_unit_of_work() is None, "Workspace observation inside writer unit"
        return {"base_revision": c.base, "head": c.base, "source_tree": tree}

    c.engine._inspect_workspace = inspect
    original_admit = c.store.admit_credentials

    async def admit(value):
        assert current_unit_of_work() is None, "Envelope admission inside writer unit"
        await original_admit(value)
        c.prepared = True

    c.store.admit_credentials = admit

    async def record_append(*args):
        await append(*args)

    ledger._append = record_append
    c.ledger = ledger
    return c


async def test_outcome_prepares_real_git_scanner_outside_units_and_retains_once(outcome_case):
    c = outcome_case
    result = await c.ledger.capture_outcome(c.state["run"]["id"])
    assert result["state"] == "verified"
    again = await c.ledger.capture_outcome(c.state["run"]["id"])
    assert again["id"] == result["id"]
    assert len(c.state["records"]) == len(c.state["projections"]) == 1
    assert len(c.state["events"]) == 2
    assert current_unit_of_work() is None


@pytest.mark.parametrize("change", ["run", "receipt", "revoked", "principal", "configuration", "ownership"])
async def test_outcome_rechecks_after_preparation_before_atomic_publication(outcome_case, change):
    c = outcome_case
    token = None

    def mutate():
        nonlocal token
        if change == "run":
            c.state["run"]["brief"] = "Different current purpose"
        elif change == "receipt":
            c.receipts[0]["payload"]["checks"] = []
            c.receipts[0]["digest"] = digest({k: v for k, v in c.receipts[0].items() if k != "digest"})
        elif change == "revoked":
            c.revoked = True
        elif change == "configuration":
            c.store.configuration._write(c.store.configuration.directory / "fixture.json", {"changed": True})
        elif change == "ownership":
            c.state["stopped"] = True
        else:
            token = CURRENT_PRINCIPAL.set(object())

    c.late_effect = mutate
    try:
        with pytest.raises((ValueError, AccessDenied, RuntimeError), match="changed|receipt|revoked|actor|stopped"):
            await c.ledger.capture_outcome(c.state["run"]["id"])
        assert c.state["records"] == c.state["events"] == c.state["projections"] == []
    finally:
        if token is not None:
            CURRENT_PRINCIPAL.reset(token)


async def test_ambient_caller_is_refused_without_reacquiring_its_locks(outcome_case):
    c = outcome_case
    async with TransactionTemplate(c.manager).transaction():
        caller = current_unit_of_work()
        before = c.unit_entries
        with pytest.raises(ValueError, match="Prepare learning outcome outside"):
            await c.ledger.capture_outcome(c.state["run"]["id"])
        assert current_unit_of_work() is caller
        assert c.unit_entries == before and not c.prepared
        assert c.state["records"] == c.state["events"] == c.state["projections"] == []
    assert current_unit_of_work() is None


async def test_preparation_cancellation_does_not_publish(outcome_case):
    import asyncio

    c = outcome_case

    async def cancel(*args):
        assert current_unit_of_work() is None
        raise asyncio.CancelledError()

    c.engine._inspect_workspace = cancel
    with pytest.raises(asyncio.CancelledError):
        await c.ledger.capture_outcome(c.state["run"]["id"])
    assert c.state["records"] == c.state["events"] == c.state["projections"] == []
    assert current_unit_of_work() is None


async def test_final_projection_failure_rolls_back_record_and_both_events(outcome_case):
    c = outcome_case

    async def refuse(row):
        assert current_unit_of_work() is not None
        assert len(c.state["records"]) == 1 and len(c.state["events"]) == 2
        raise RuntimeError("Public fixture final projection failed")

    c.ledger.projections.save = refuse
    with pytest.raises(RuntimeError, match="projection failed"):
        await c.ledger.capture_outcome(c.state["run"]["id"])
    assert c.state["records"] == c.state["events"] == c.state["projections"] == []
