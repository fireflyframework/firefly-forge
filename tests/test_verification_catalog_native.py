# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Native Store/lease/UOW/Engine path with controlled worker and command backend only."""

import asyncio

import pytest
from pyfly.data.transaction import current_unit_of_work
from test_automatic_verification_native import prepared
from worker_reply_fixtures import authenticated_operator

from firefly_forge.identity_persistence import ForgeIdentityUserRepository
from firefly_forge.journal import Journal
from firefly_forge.verification_handoffs import automatic_candidate
from firefly_forge.verification_source_preparation import current_verification_source


@pytest.mark.parametrize("scenario", ["completed", "unknown_revoked"])
async def test_native_catalog_handoff_uses_transaction_free_preparation_without_source_maps(
    store, tmp_path, monkeypatch, scenario
):
    from firefly_forge import assurance, verification_handoffs

    original = verification_handoffs.prepare_verification_source
    observed = []

    async def outside_writer(*args, **kwargs):
        assert current_unit_of_work() is None
        await store.assert_ownership()
        observed.append("prepared_outside_owned_unit")
        return await original(*args, **kwargs)

    def no_map(*args, **kwargs):
        raise AssertionError("Native worker handoff must not construct a full project map")

    monkeypatch.setattr(verification_handoffs, "prepare_verification_source", outside_writer)
    monkeypatch.setattr(assurance, "_manifest", no_map)
    async with authenticated_operator(store) as actor:
        env = await prepared(store, tmp_path, monkeypatch)
        env.engine.enforce_ownership = True
        run_id = env.run["id"]
        workspace = env.engine.workspaces / run_id
        if scenario == "unknown_revoked":

            async def uncertain(config, argv, *args, **kwargs):
                env.backend_calls.append(argv)
                async with store._unit():
                    user = await ForgeIdentityUserRepository().find_by_id(actor.id)
                    user.enabled = False
                    await ForgeIdentityUserRepository().save(user)
                raise TimeoutError("Controlled unknown command outcome")

            env.backend.run = uncertain
        await env.service.execute(run_id)
        assert current_verification_source(workspace) is None
        current = await store.get_run(run_id)
        if scenario == "completed":
            assert current["status"] == "queued" and len(env.backend_calls) == 7
            assert len(await env.journal.sandbox_executions(run_id)) == 7
            await asyncio.create_task(env.engine.execute_run(run_id))
            current = await store.get_run(run_id)
            assert len(env.calls) == 2 and current["implementation_rounds"] == 2
            assert len(observed) >= 2 and current["repair_budget"] == 0
            assert Journal.verify_chain(await env.journal.entries(run_id))
        else:
            assert current["verification_handoff"]["status"] == "held"
            assert current["worker_request"]["status"] == "pending"
            await env.service.execute(run_id)
            assert len(env.calls) == 1 and len(env.backend_calls) == 1
            assert not automatic_candidate(await store.get_run(run_id))
        assert observed


async def test_native_lost_lease_after_intent_wait_rolls_back_verification_admission(store, tmp_path, monkeypatch):
    async with authenticated_operator(store):
        env = await prepared(store, tmp_path, monkeypatch)
        env.engine.enforce_ownership = True
        before = await env.journal.entries(env.run["id"])
        before_run = await store.get_run(env.run["id"])
        original = env.journal._append

        async def lose_owner(*args, **kwargs):
            result = await original(*args, **kwargs)
            if args[3] == "verification_handoff_intent":
                await store.release_ownership()
            return result

        monkeypatch.setattr(env.journal, "_append", lose_owner)
        with pytest.raises(RuntimeError):
            await env.service.execute(env.run["id"])
        assert await store.get_run(env.run["id"]) == before_run
        assert await env.journal.entries(env.run["id"]) == before
        assert env.backend_calls == [] and len(env.calls) == 1
