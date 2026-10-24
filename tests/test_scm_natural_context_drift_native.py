# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Native Store/UOW lifecycle races; provider and SCM transports remain controlled."""

import copy

import pytest
from scm_polling_native_fixtures import native_poll as native_poll
from test_scm_natural_native import observe, prepare
from worker_reply_fixtures import authenticated_operator

from firefly_forge.scm_natural_models import NaturalDecision
from firefly_forge.scm_natural_tasks import SCMNaturalTasks


@pytest.mark.parametrize("drift_at", ["provider", "final_lock"])
def test_native_completed_status_refresh_preserves_store_unit_and_original_receipt(native_poll, monkeypatch, drift_at):
    from pyfly.data.transaction import current_unit_of_work

    case = native_poll
    prepare(case, monkeypatch)
    case.process(case.observe("status"))

    async def progress(status):
        async with authenticated_operator(case.store):
            await case.store.update(case.run["id"], status=status, worker_request=None)

    case.http.portal.call(progress, "validating")
    delivery = observe(case, "@forge, which checks finished and what remains unverified?")
    wal = case.http.portal.call(case.journal.entries, case.run["id"])
    natural = SCMNaturalTasks(case.collaboration)
    original = natural._context
    calls, units = [], []
    reads = 0

    async def context(source, scopes):
        nonlocal reads
        outer = current_unit_of_work()
        if len(calls) == 1:
            reads += 1
            if drift_at == "final_lock" and reads == 2:
                await progress("failed")
                assert current_unit_of_work() is outer
        result = await original(source, scopes)
        assert current_unit_of_work() is outer
        if outer is not None:
            units.append(outer)
        return result

    async def classify(message, context, settings):
        assert current_unit_of_work() is None, "No classifier I/O under native Store/UOW locks"
        calls.append(copy.deepcopy(context))
        if drift_at == "provider" and len(calls) == 1:
            await progress("failed")
        return NaturalDecision(
            intent="status", message="Native check status: " + context["candidates"][0]["run_status"]
        )

    monkeypatch.setattr(natural, "_context", context)
    monkeypatch.setattr("firefly_forge.scm_natural_tasks.classify", classify)
    result = case.http.portal.call(natural.process, delivery["id"])
    assert result["status"] == "processed", result
    assert result["natural_turn"]["reply"] == "Native check status: failed"
    assert result["natural_turn"]["classifier_requests_reserved"] == 2 and len(calls) == 2
    assert len(result["natural_turn"]["classifier_attempts"]) == 2 and units
    assert result["source_snapshot"] == delivery["source_snapshot"]
    assert result["verification"] == delivery["verification"]
    assert case.http.portal.call(case.journal.entries, case.run["id"]) == wal
    assert case.http.portal.call(case.store.get_run, case.run["id"])["status"] == "failed"
    assert case.http.portal.call(natural.process, delivery["id"]) == result and len(calls) == 2
