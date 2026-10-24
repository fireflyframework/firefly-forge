# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Failed-check source preparation and atomic candidate custody use separate units."""

import asyncio
import copy

import pytest
from pyfly.data.transaction import TransactionTemplate, current_unit_of_work
from test_engine_diff_artifacts import snapshot_case as snapshot_case
from test_learning_outcome_uow_host import outcome_case as outcome_case

from firefly_forge import learning as module
from firefly_forge.journal import digest
from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied


@pytest.fixture
async def failure_case(outcome_case):
    c = outcome_case
    run = c.state["run"]
    run.update(status="running", verification=[{"argv": ["public-check"], "exit_code": 1}])
    run.pop("final_verification")
    receipt = copy.deepcopy(c.receipts[0])
    receipt["payload"]["checks"] = copy.deepcopy(run["verification"])
    receipt["digest"] = digest({key: value for key, value in receipt.items() if key != "digest"})
    c.receipts[:] = [receipt]
    c.observations = []

    async def inspect(*args):
        c.observations.append(current_unit_of_work())
        _, _, tree = await c.engine._snapshot(c.root, c.base, allow_unchanged=True)
        return {"base_revision": c.base, "head": c.base, "source_tree": tree}

    c.engine._inspect_workspace = inspect
    return c


async def test_failed_check_prepares_actual_git_outside_units_and_retains_candidate_once(failure_case):
    c = failure_case
    result = await c.ledger.capture_failure(c.state["run"]["id"])
    assert result["state"] == "candidate" and result["kind"] == "observed_failure"
    assert result["envelope"]["purpose"] == "verification"
    assert "cannot authorize repair suppression" in result["envelope"]["statement"]
    assert result["envelope"]["receipts"] == [
        {"kind": "receipt", "key": "verify", "sequence": 1, "digest": c.receipts[0]["digest"]}
    ]
    assert (await c.ledger.capture_failure(c.state["run"]["id"]))["id"] == result["id"]
    assert len(c.state["records"]) == 1 and len(c.state["events"]) == 2
    assert c.state["projections"] == []
    assert c.observations and all(unit is None for unit in c.observations)
    assert current_unit_of_work() is None


@pytest.mark.parametrize("change", ["run", "receipt", "revoked", "principal", "configuration", "ownership"])
async def test_failed_check_rechecks_final_lookup_wait_before_any_candidate_write(failure_case, change):
    c = failure_case
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
            await c.ledger.capture_failure(c.state["run"]["id"])
        assert c.state["records"] == c.state["events"] == c.state["projections"] == []
    finally:
        if token is not None:
            CURRENT_PRINCIPAL.reset(token)


async def test_failed_check_refuses_ambient_unit_without_reacquiring_locks(failure_case):
    c = failure_case
    async with TransactionTemplate(c.manager).transaction():
        caller = current_unit_of_work()
        before = c.unit_entries
        with pytest.raises(ValueError, match="Prepare learning failure outside"):
            await c.ledger.capture_failure(c.state["run"]["id"])
        assert current_unit_of_work() is caller and c.unit_entries == before
        assert c.state["records"] == c.state["events"] == []


async def test_failed_check_preparation_cancellation_never_publishes(failure_case):
    c = failure_case

    async def cancel(*args):
        assert current_unit_of_work() is None
        raise asyncio.CancelledError()

    c.engine._inspect_workspace = cancel
    with pytest.raises(asyncio.CancelledError):
        await c.ledger.capture_failure(c.state["run"]["id"])
    assert c.state["records"] == c.state["events"] == c.state["projections"] == []


async def test_failed_check_final_event_failure_rolls_back_candidate_and_first_event(failure_case):
    c = failure_case

    async def refuse(*args):
        assert current_unit_of_work() is not None
        assert len(c.state["records"]) == len(c.state["events"]) == 1
        raise RuntimeError("Public fixture final candidate event failed")

    c.ledger.journal._append = refuse
    with pytest.raises(RuntimeError, match="final candidate event failed"):
        await c.ledger.capture_failure(c.state["run"]["id"])
    assert c.state["records"] == c.state["events"] == c.state["projections"] == []


@pytest.mark.parametrize("code", [0, 2, None, True])
async def test_only_exact_failed_check_receipt_can_become_a_candidate(failure_case, code):
    c = failure_case
    c.receipts[0]["payload"]["checks"][0]["exit_code"] = code
    c.receipts[0]["digest"] = digest({k: v for k, v in c.receipts[0].items() if k != "digest"})
    assert await c.ledger.capture_failure(c.state["run"]["id"]) is None
    assert c.observations == [] and c.state["records"] == c.state["events"] == []


async def test_failed_check_source_change_during_context_observation_is_not_retained(failure_case, monkeypatch):
    c = failure_case
    observe = module.observe_context

    async def mutate(*args, **kwargs):
        result = await observe(*args, **kwargs)
        (c.root / "implementation.py").write_text("public_value = 99\n")
        return result

    monkeypatch.setattr(module, "observe_context", mutate)
    with pytest.raises(ValueError, match="source changed"):
        await c.ledger.capture_failure(c.state["run"]["id"])
    assert c.state["records"] == c.state["events"] == c.state["projections"] == []
