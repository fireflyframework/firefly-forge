# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Bounded candidate projection over actual checkpoint/restore services; owned rows only."""

import copy
import json

import pytest
from pyfly.data.transaction import current_unit_of_work
from test_agentic_checkpoint_activation_host import activated, lost_completion, queue_original
from test_bridge_inflight_cancel import RUN

from firefly_forge.api import create_runtime
from firefly_forge.project_access import AccessDenied


async def selectors(env):
    rows = env.journal.records

    async def keys(run_id):
        return [
            r.idempotency_key for r in sorted(rows.rows, key=lambda r: -r.sequence) if r.kind == "agent_checkpoint"
        ][:16]

    async def observations(run_id, completion_id):
        return [
            r.idempotency_key
            for r in sorted(rows.rows, key=lambda r: -r.sequence)
            if r.kind == "bridge_request_observation" and r.data["payload"]["request_id"] == completion_id
        ][:1]

    async def tickets(run_id):
        return [
            {"id": r.id.hex, "completion_id": r.request_key, "status": r.status}
            for r in env.requests.records.rows
            if r.kind == "agent_restore"
        ]

    rows.checkpoint_candidates = keys
    rows.checkpoint_observations = observations
    rows.checkpoint_restore_tickets = tickets


async def projection(env):
    token = env.principal_context.set(env.principal)
    try:
        return await env.restores.candidates(RUN)
    finally:
        env.principal_context.reset(token)


@pytest.mark.parametrize("source", ["http_response", "bridge_observation"])
async def test_candidate_projection_is_allowlisted_readonly_and_discovers_existing_ticket(
    tmp_path, monkeypatch, source
):
    env = await activated(tmp_path, monkeypatch)
    try:
        posts, intent, checkpoint, _, request = await queue_original(env, monkeypatch, source=source)
        await selectors(env)
        before = copy.deepcopy([r.data for r in env.journal.records.rows])
        page = await projection(env)
        assert set(page) == {"version", "actor_id", "run_id", "repository_id", "project_id", "state", "items"}
        assert page["state"] == "available" and len(page["items"]) == 2
        item = page["items"][0]
        assert set(item) == {
            "completion_id",
            "checkpoint_digest",
            "intent_digest",
            "task",
            "attempt",
            "ordinal",
            "deadline_ms",
            "created_at",
            "state",
            "result",
            "observation",
            "ticket",
        }
        assert item["completion_id"] == intent["idempotency_key"] and item["checkpoint_digest"] == checkpoint["digest"]
        assert item["state"] == "ticket_recorded" and item["ticket"] == {"id": request.request_id, "status": "queued"}
        assert item["result"]["kind"] == source
        assert len(posts) == 2 and before == [r.data for r in env.journal.records.rows]
        encoded = json.dumps(page)
        for private in ("snapshot", "brief", "response", "workspace", "terminal_digest_invalid"):
            assert '"' + private + '"' not in encoded
    finally:
        await env.model.client.close()


async def test_original_result_candidate_does_not_probe_source_or_issue_ticket(tmp_path, monkeypatch):
    env = await activated(tmp_path, monkeypatch)
    try:
        posts, intent, _ = await lost_completion(env, monkeypatch, http_failure=True)
        await selectors(env)
        page = await projection(env)
        item = page["items"][0]
        assert item["state"] == "ready" and item["ticket"] is None
        assert item["completion_id"] == intent["idempotency_key"] and item["result"]["kind"] == "http_response"
        assert not env.requests.records.rows and len(posts) == 2
    finally:
        await env.model.client.close()


@pytest.mark.parametrize("drift", ["identity", "run", "credential", "record"])
async def test_candidate_final_admission_rejects_late_changes_without_partial_candidates(tmp_path, monkeypatch, drift):
    env = await activated(tmp_path, monkeypatch)
    try:
        await lost_completion(env, monkeypatch, http_failure=True)
        await selectors(env)
        admit = env.store.admit_credentials
        changed = []

        async def late(value):
            assert current_unit_of_work() is None
            await admit(value)
            if not changed:
                changed.append(True)
                if drift == "identity":
                    env.user.enabled = False
                elif drift == "run":
                    env.store.run.data["publish"] = True
                elif drift == "credential":
                    env.store.reject_credentials = lambda value: (_ for _ in ()).throw(
                        ValueError("fictional credential")
                    )
                else:
                    checkpoint = next(r for r in env.journal.records.rows if r.kind == "agent_checkpoint")
                    checkpoint.data["payload"]["snapshot"] = "tampered"

        env.store.admit_credentials = late
        if drift in {"identity", "run"}:
            with pytest.raises(AccessDenied):
                await projection(env)
        else:
            page = await projection(env)
            assert page["state"] == "evidence_unavailable" and page["items"] == []
        assert not env.requests.records.rows
    finally:
        await env.model.client.close()


async def test_candidate_scalar_budget_precedes_body_selector(tmp_path, monkeypatch):
    env = await activated(tmp_path, monkeypatch)
    try:
        await lost_completion(env, monkeypatch, http_failure=True)
        await selectors(env)

        async def full(run_id):
            return {"count": 101, "consumes": 0, "max_bytes": 0, "bytes": 0}

        async def forbidden(*args):
            raise AssertionError("Oversized body selector ran")

        env.journal.records.checkpoint_budget = full
        env.journal.records.checkpoint_candidates = forbidden
        page = await projection(env)
        assert page["state"] == "evidence_unavailable" and page["items"] == []
    finally:
        await env.model.client.close()


@pytest.mark.parametrize("binding", ["expected_checkpoint_digest", "expected_intent_digest", "source", "request_id"])
async def test_candidate_rejects_a_chained_ticket_bound_to_other_checkpoint_evidence(tmp_path, monkeypatch, binding):
    from firefly_forge.execution_requests import digest

    env = await activated(tmp_path, monkeypatch)
    try:
        await queue_original(env, monkeypatch, source="http_response")
        await selectors(env)
        row = env.requests.records.rows[0]
        if binding == "source":
            row.data["request"]["input"]["source"]["record_digest"] = "e" * 64
        else:
            row.data["request"]["input"][binding] = "e" * (32 if binding == "request_id" else 64)
        row.data["request_digest"] = digest(row.data["request"])
        event = env.requests.journal.rows[0]
        event.data["evidence"] = {"request_digest": row.data["request_digest"]}
        event.data["digest"] = digest({k: v for k, v in event.data.items() if k != "digest"})
        assert env.requests.verify_chain([event.data])
        page = await projection(env)
        assert page["state"] == "evidence_unavailable" and page["items"] == []
    finally:
        await env.model.client.close()


def test_candidate_route_is_registered_in_the_real_api_container_without_startup(tmp_path):
    from firefly_forge.agent_restore_controllers import AgentCheckpointCandidateController

    runtime = create_runtime(tmp_path, "owned-bootstrap-fixture-at-least-24-characters", role="api")
    assert runtime.context.container.contains_type(AgentCheckpointCandidateController)
