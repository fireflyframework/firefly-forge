# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Fictional consumed final output after a real native checkpoint continuation."""

import json
from uuid import uuid4

import httpx2
from checkpoint_continuation_native_fixtures import (
    checkpoint_input,
    enable_checkpoint_continuation,
    seed_pending_checkpoint,
)
from test_agentic_checkpoint_activation_host import result
from test_bridge_completion_native import mock_model_requests

from firefly_forge.final_outcome_models import FinalOutcomeContinuationInputV1
from firefly_forge.invalid_read_lease import ContinuityLeasePolicyV1, write_policy


def final_input(candidate):
    return FinalOutcomeContinuationInputV1(
        version=1,
        candidate_digest=candidate["digest"],
        valid_for_seconds=180,
        acknowledge_already_consumed_final_output=True,
        acknowledge_no_completed_effect_replay=True,
        acknowledge_same_request_attempt_and_budgets=True,
        acknowledge_finite_ownership_lease=True,
    )


def enable_final(c):
    write_policy(
        c.store.configuration,
        ContinuityLeasePolicyV1(
            version=1,
            enabled=True,
            allow_checkpoint_continuation=True,
            allow_final_outcome_continuation=True,
            max_window_seconds=180,
        ),
    )


async def seed_consumed_final(c, monkeypatch, *, feedback=True):
    c.store.execution_requests = c.requests
    failed = await seed_pending_checkpoint(c, monkeypatch)
    enable_checkpoint_continuation(c)
    candidate = await c.service.checkpoints.candidate(c.run_id, failed["id"])
    await c.service.checkpoints.request(c.run_id, failed["id"], uuid4().hex, checkpoint_input(candidate))
    model = c.engine.provider_factory("bridge").model()
    original = model.client._client._transport

    def transport(request):
        if request.method == "GET":
            return original.handler(request)
        c.posts.append(request.content)
        assert len(c.posts) == 4, "Only the next final response may be generated"
        report = dict(summary="Known write needs its already approved check", qa_report=None, worker_feedback=None)
        if feedback:
            report["worker_feedback"] = dict(
                id="fictional-consumed-final",
                role_id="implementer",
                task_id="implement",
                attempt=2,
                disposition="blocked",
                summary="Run the frozen verification command",
                request=dict(
                    id="fictional-final-check",
                    kind="verification",
                    question="Can the approved check run?",
                    context="No new command or grant",
                ),
            )
        return httpx2.Response(200, json=result(json.dumps(report)))

    async def stop(*args, **kwargs):
        raise ValueError("Fictional authority loss before known final acceptance")

    with monkeypatch.context() as held, mock_model_requests(model):
        held.setattr(model.client._client, "_transport", httpx2.MockTransport(transport))
        held.setattr(c.engine, "_worker_feedback" if feedback else "_snapshot", stop)
        await c.service.execute(failed["id"])
    current = await c.store.get_run(c.run_id)
    failed = await c.service.get(c.run_id, failed["id"])
    entries = await c.journal.entries(c.run_id)
    assert failed["status"] == "needs_reconciliation" and current["status"] in {"failed", "needs_reconciliation"}
    assert current["implementation_rounds"] == 2 and current["implementation_pending"] is False
    assert entries[-1]["kind"] == "agent_consume" and len(c.posts) == 4
    assert sum(e["kind"] == "agent_consume" and e["payload"].get("version") == 2 for e in entries) == 1
    return failed
