# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Characterize custody before proposing a partial-source settlement API."""

import httpx2
import pytest
from pydantic_ai import models
from test_agentic_checkpoint_activation_host import activated, result
from test_bridge_completion_callbacks import CAPABILITY
from test_bridge_completion_native import mock_model_requests
from test_bridge_inflight_cancel import RUN
from test_git_delivery_local import delivery_case as delivery_case


async def test_capture_refusal_after_known_write_preserves_consume_without_next_intent(tmp_path, monkeypatch):
    from firefly_forge.agentic_checkpoint_boundary import AgenticBoundary

    env = await activated(tmp_path, monkeypatch)
    posts = []
    original = AgenticBoundary.capture

    def capture(boundary, wire):
        if (env.workspace / "service.py").exists():
            raise ValueError("Checkpoint size or digest differs")
        return original(boundary, wire)

    monkeypatch.setattr(AgenticBoundary, "capture", capture)

    def transport(request):
        if request.method == "GET":
            return httpx2.Response(200, json={"completionRecovery": CAPABILITY})
        posts.append(request.content)
        assert len(posts) == 1, "A refused checkpoint must never send the next POST"
        return httpx2.Response(200, json=result(tool=True))

    env.model.client._client._transport = httpx2.MockTransport(transport)
    env.model.client._client._mounts = {}
    try:
        with models.override_allow_model_requests(False), mock_model_requests(env.model):
            await env.engine.execute_run(RUN)
        current = await env.store.get_run(RUN)
        assert current["status"] == "failed"
        assert current["implementation_pending"] is True
        assert current["error"] == "Checkpoint size or digest differs"
        assert (env.workspace / "service.py").read_text() == "VALUE = 1\n"
        rows = env.journal.records.rows
        for kind in ("bridge_request_intent", "agent_checkpoint", "bridge_request_response", "agent_consume"):
            assert len([row for row in rows if row.kind == kind]) == 1
        assert not any(row.kind == "worker_feedback" for row in rows)
        assert not any("predispatch" in row.kind or "partial_source" in row.kind for row in rows)
        assert len(posts) == 1
    finally:
        await env.model.client.close()


@pytest.mark.parametrize("flag", ["implementation_pending", "bridge_completion_hold"])
async def test_failed_status_does_not_settle_effects_for_existing_git_candidate(delivery_case, flag):
    c = delivery_case()
    c.state["run"]["status"] = "failed"
    c.state["run"][flag] = True
    (c.root / "product.py").write_text("value = 2\n")
    with pytest.raises(ValueError, match="source owner to settle"):
        await c.service.candidate("1" * 32, actor=c.state["run"]["execution_actor"])
    assert not c.state["entries"]


def test_failed_feedback_proof_cannot_be_invented_from_an_exception_string():
    from test_git_failed_unit_adoption import failed_case

    from firefly_forge.git_failed_unit_adoption import failed_unit_proof

    run, task, entries, observation = failed_case()
    run["worker_feedback"] = None
    run["error"] = "Checkpoint size or digest differs"
    entries = [entry for entry in entries if entry["kind"] != "worker_feedback"]
    with pytest.raises(ValueError, match="exact accepted feedback"):
        failed_unit_proof(run, task, entries, observation)
