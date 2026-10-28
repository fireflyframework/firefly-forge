# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""PREPARED, UNRUN: fictional PostgreSQL custody; only the root database owner executes."""

import asyncio
import copy
from uuid import uuid4

import pytest
from provider_rejection_native_fixtures import rejection_input, reviewed_rejection, seed_claimed_refusal
from pydantic_ai import models

from firefly_forge.invalid_read_dispatch_guard import assert_generic_dispatch_allowed
from firefly_forge.invalid_read_lease import ContinuityLeasePolicyV1, write_policy

pytest_plugins = ["test_invalid_read_continuity_native"]


def enable(c):
    write_policy(
        c.store.configuration,
        ContinuityLeasePolicyV1(
            version=1,
            enabled=True,
            max_window_seconds=180,
            allow_pre_admission_recovery=True,
        ),
    )


async def test_native_claimed_refusal_resumes_original_request_and_charges_only_new_commands(native_continuity):
    c = native_continuity
    with models.override_allow_model_requests(True):
        failed = await seed_claimed_refusal(c)
        prefix = copy.deepcopy(await c.journal.entries(c.run_id))
        events = copy.deepcopy(await c.requests.events(failed["id"]))
        enable(c)
        proof = await reviewed_rejection(c, failed)
        candidate = await c.service.rejections.candidate(c.run_id, failed["id"], proof)
        body = rejection_input(candidate, proof)
        receipt = await c.service.rejections.request(c.run_id, failed["id"], uuid4().hex, body)
        assert receipt["status"] == "queued"
        queued = await c.service.get(c.run_id, failed["id"])
        assert all(queued[k] == failed[k] for k in ("generation", "owner_pid", "admission_seal", "checkpoint"))
        await c.journal.recover()
        await c.service.execute(failed["id"])
        final = await c.service.get(c.run_id, failed["id"])
        assert final["status"] == "completed", final.get("result")
        assert final["generation"] != failed["generation"] and final["admission_seal"] == failed["admission_seal"]
        assert final["checkpoint"] == failed["checkpoint"]
        assert (await c.requests.events(failed["id"]))[:5] == events
        assert len(await c.requests.events(failed["id"])) == 8
        assert (await c.journal.entries(c.run_id))[: len(prefix)] == prefix
        commands = await c.journal.sandbox_executions(c.run_id)
        assert len(commands) == 13 and commands[:11] == c.initial_commands
        current = await c.store.get_run(c.run_id)
        assert current["attempt"] == 2 and current["repair_budget"] == 2
        assert current["sandbox_snapshot"]["configuration"]["command_limit"] == 22
        c.store.execution_requests = c.requests
        await assert_generic_dispatch_allowed(c.store, c.journal, current)
        await c.service.execute(failed["id"])
        assert len(c.posts) == 7 and len(await c.journal.sandbox_executions(c.run_id)) == 13


async def test_native_rejection_admission_rolls_back_and_concurrent_identical_actions_read_one_receipt(
    native_continuity, monkeypatch
):
    c = native_continuity
    with models.override_allow_model_requests(True):
        failed = await seed_claimed_refusal(c)
        enable(c)
        proof = await reviewed_rejection(c, failed)
        candidate = await c.service.rejections.candidate(c.run_id, failed["id"], proof)
        body = rejection_input(candidate, proof)
        action = uuid4().hex
        prefix = copy.deepcopy(await c.journal.entries(c.run_id))
        events = copy.deepcopy(await c.requests.events(failed["id"]))
        original = c.requests._event

        async def fail_after_event(row, kind, evidence):
            await original(row, kind, evidence)
            if kind == "request_recovered_after_rejection":
                raise RuntimeError("Controlled post-event rollback")

        with monkeypatch.context() as scoped:
            scoped.setattr(c.requests, "_event", fail_after_event)
            with pytest.raises(RuntimeError, match="rollback"):
                await c.service.rejections.request(c.run_id, failed["id"], action, body)
        assert await c.service.get(c.run_id, failed["id"]) == failed
        assert await c.journal.entries(c.run_id) == prefix
        assert await c.requests.events(failed["id"]) == events
        replies = await asyncio.gather(
            *(c.service.rejections.request(c.run_id, failed["id"], action, body) for _ in range(3))
        )
        assert replies[1:] == replies[:1] * 2
        assert len([e for e in await c.journal.entries(c.run_id) if e["kind"] == "provider_rejection_recovery"]) == 1
        assert len(await c.requests.events(failed["id"])) == 6 and len(c.posts) == 2


async def test_native_revoked_rejection_policy_cannot_reclaim_or_dispatch(native_continuity):
    c = native_continuity
    with models.override_allow_model_requests(True):
        failed = await seed_claimed_refusal(c)
        enable(c)
        proof = await reviewed_rejection(c, failed)
        candidate = await c.service.rejections.candidate(c.run_id, failed["id"], proof)
        await c.service.rejections.request(c.run_id, failed["id"], uuid4().hex, rejection_input(candidate, proof))
        write_policy(c.store.configuration, ContinuityLeasePolicyV1(version=1, enabled=False))
        await c.service.execute(failed["id"])
        current = await c.service.get(c.run_id, failed["id"])
        assert current["status"] == "failed" and current["generation"] == failed["generation"]
        assert len(c.posts) == 2 and len(await c.journal.sandbox_executions(c.run_id)) == 11
        assert (await c.requests.events(failed["id"]))[-1]["kind"] == "worker_failed"
