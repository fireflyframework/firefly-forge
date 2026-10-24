# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Current projection follows the latest exact intent, never an earlier confirmation."""

import copy

import pytest
from test_git_delivery_lifecycle import remote_case as remote_case
from test_git_delivery_local import delivery_case as delivery_case

RUN, ACTOR = "1" * 32, {"user_id": "4" * 32}


async def next_intent(c):
    (c.root / "product.py").write_text("value = 2\n")
    first = await c.service.checkpoint(RUN, request_id="a" * 32, expected_head=c.base, actor=ACTOR)
    old = next(e for e in c.state["entries"] if e["kind"] == "git_delivery_remote_intent")
    payload = copy.deepcopy(old["payload"])
    payload["purpose"] = "verified_ready_for_review"
    pending = await c.service._append(RUN, "git_delivery_remote_intent", "git-final:review", payload)
    return first, pending


async def test_later_unconfirmed_remote_intent_wins_over_earlier_draft(remote_case):
    c = remote_case
    first, pending = await next_intent(c)
    projected = (await c.service.view(RUN, actor=ACTOR))["items"][-1]
    assert projected["remote_status"] == "unknown"
    assert projected["pr_url"] == first["pr_url"]
    assert not any(
        e["kind"] == "git_delivery_remote_receipt" and e["idempotency_key"] == pending["idempotency_key"]
        for e in c.state["entries"]
    )


async def test_exact_latest_confirmation_replaces_unknown_state(remote_case):
    c = remote_case
    first, pending = await next_intent(c)
    prior = next(e for e in c.state["entries"] if e["kind"] == "git_delivery_remote_receipt")
    result = {**copy.deepcopy(prior["payload"]), "intent_digest": pending["digest"], "remote_status": "ready"}
    await c.service._append(RUN, "git_delivery_remote_receipt", pending["idempotency_key"], result)
    projected = (await c.service.view(RUN, actor=ACTOR))["items"][-1]
    assert projected["remote_status"] == "ready" and projected["pr_url"] == first["pr_url"]


async def test_orphan_confirmation_is_never_shown_as_current_remote_state(remote_case):
    c = remote_case
    await next_intent(c)
    prior = next(e for e in c.state["entries"] if e["kind"] == "git_delivery_remote_receipt")
    await c.service._append(RUN, "git_delivery_remote_receipt", "git-orphan", copy.deepcopy(prior["payload"]))
    with pytest.raises(ValueError, match="intent|custody"):
        await c.service.view(RUN, actor=ACTOR)
