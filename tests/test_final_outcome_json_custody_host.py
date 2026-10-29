# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Native JSON types remain part of retained output and its current authority."""

import pytest
from test_bridge_inflight_cancel import RUN
from test_final_outcome_continuation_host import consumed_blocker, reviewed_action

pytest_plugins = ["test_invalid_read_native_protocol_host"]


@pytest.mark.parametrize("continuity_case", [True], indirect=True)
@pytest.mark.parametrize("mutation", ["requests", "feedback_attempt", "attempt", "rounds"])
async def test_final_projection_rejects_native_json_type_changes(continuity_case, monkeypatch, mutation):
    c = continuity_case
    ticket = await consumed_blocker(c, monkeypatch)
    await c.continuity.final_outcomes._prepare(RUN, ticket["id"])
    usage = c.store.run.data["usage"]
    if mutation == "requests":
        usage["requests"] = float(usage["requests"])
    elif mutation in {"attempt", "rounds"}:
        key = "attempt" if mutation == "attempt" else "implementation_rounds"
        c.store.run.data[key] = float(c.store.run.data[key])
    else:
        feedback = usage["runtime_report"]["worker_feedback"]
        feedback["attempt"] = float(feedback["attempt"])
    with pytest.raises(ValueError):
        await c.continuity.final_outcomes._prepare(RUN, ticket["id"])
    assert len(c.posts) == 3


@pytest.mark.parametrize("continuity_case", [True], indirect=True)
async def test_final_claim_rejects_numeric_type_drift_after_admission(continuity_case, monkeypatch):
    c = continuity_case
    ticket = await consumed_blocker(c, monkeypatch)
    body = await reviewed_action(c, ticket)
    await c.continuity.final_outcomes.request(RUN, ticket["id"], "e" * 32, body)
    c.store.run.data["usage"]["requests"] = float(c.store.run.data["usage"]["requests"])
    await c.continuity.execute(ticket["id"])
    assert (await c.requests.get(ticket["id"]))["status"] == "failed"
    assert not any(row.kind == "terminal_completion_receipt" for row in c.journal.records.rows)
    assert len(c.posts) == 3
