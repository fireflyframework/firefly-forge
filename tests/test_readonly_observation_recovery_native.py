# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""PREPARED, UNRUN: only the root owner executes fresh fictional PostgreSQL cases."""

import asyncio
import copy
from uuid import uuid4

import pytest
from pydantic_ai import models
from readonly_observation_native_fixtures import observation_input, seed_observation_failure

from firefly_forge.invalid_read_dispatch_guard import assert_generic_dispatch_allowed
from firefly_forge.invalid_read_lease import ContinuityLeasePolicyV1, write_policy

pytest_plugins = ["test_invalid_read_continuity_native"]


async def test_native_same_ticket_read_observation_reaches_next_model_and_preserves_unknown(
    native_continuity, monkeypatch
):
    c = native_continuity
    with models.override_allow_model_requests(True):
        failed = await seed_observation_failure(c, monkeypatch)
        prefix = copy.deepcopy(await c.journal.entries(c.run_id))
        events = copy.deepcopy(await c.requests.events(failed["id"]))
        candidate = await c.service.observations.candidate(c.run_id, failed["id"])
        assert candidate["usage"]["commands"] == 11 and candidate["usage"]["command_limit"] == 22
        await c.service.observations.request(c.run_id, failed["id"], uuid4().hex, observation_input(candidate))
        await c.journal.recover()
        await c.service.execute(failed["id"])
        final = await c.service.get(c.run_id, failed["id"])
        assert final["status"] == "completed", final.get("result")
        assert final["generation"] != failed["generation"] and final["owner_pid"] == c.store._ownership_pid
        assert all(final[k] == failed[k] for k in ["checkpoint", "checkpoint_digest", "admission_seal", "binding"])
        assert (await c.journal.entries(c.run_id))[: len(prefix)] == prefix
        assert (await c.requests.events(failed["id"]))[:8] == events
        assert len(await c.requests.events(failed["id"])) == 11
        assert len(c.missing_observations) == 1
        commands = await c.journal.sandbox_executions(c.run_id)
        assert len(commands) == 13 and commands[:11] == c.initial_commands
        assert len(c.posts) == 7 and (await c.store.get_run(c.run_id))["attempt"] == 2
        c.store.execution_requests = c.requests
        await assert_generic_dispatch_allowed(c.store, c.journal, await c.store.get_run(c.run_id))
        await c.service.execute(failed["id"])
        assert len(c.posts) == 7


async def test_native_observation_admission_atomic_rollback_and_same_action_readback(native_continuity, monkeypatch):
    c = native_continuity
    with models.override_allow_model_requests(True):
        failed = await seed_observation_failure(c, monkeypatch)
        candidate = await c.service.observations.candidate(c.run_id, failed["id"])
        body = observation_input(candidate)
        action = uuid4().hex
        prefix = copy.deepcopy(await c.journal.entries(c.run_id))
        events = copy.deepcopy(await c.requests.events(failed["id"]))
        event = c.requests._event

        async def rollback(row, kind, evidence):
            await event(row, kind, evidence)
            if kind == "request_recovered_after_readonly_observation":
                raise RuntimeError("Controlled post-event rollback")

        with monkeypatch.context() as patch:
            patch.setattr(c.requests, "_event", rollback)
            with pytest.raises(RuntimeError, match="rollback"):
                await c.service.observations.request(c.run_id, failed["id"], action, body)
        assert await c.service.get(c.run_id, failed["id"]) == failed
        assert await c.journal.entries(c.run_id) == prefix and await c.requests.events(failed["id"]) == events
        replies = await asyncio.gather(
            *(c.service.observations.request(c.run_id, failed["id"], action, body) for _ in range(3))
        )
        assert replies[1:] == replies[:1] * 2
        assert len([e for e in await c.journal.entries(c.run_id) if e["kind"] == "readonly_observation_recovery"]) == 1
        assert len(await c.requests.events(failed["id"])) == 9 and len(c.posts) == 3


async def test_native_observation_revoked_policy_cannot_claim_or_replay(native_continuity, monkeypatch):
    c = native_continuity
    with models.override_allow_model_requests(True):
        failed = await seed_observation_failure(c, monkeypatch)
        candidate = await c.service.observations.candidate(c.run_id, failed["id"])
        await c.service.observations.request(c.run_id, failed["id"], uuid4().hex, observation_input(candidate))
        write_policy(c.store.configuration, ContinuityLeasePolicyV1(version=1, enabled=False))
        await c.service.execute(failed["id"])
        final = await c.service.get(c.run_id, failed["id"])
        assert final["status"] == "failed" and final["generation"] == failed["generation"]
        assert len(c.posts) == 3 and len(c.missing_observations) == 1
