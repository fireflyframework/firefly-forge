# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Committed native identity revocation blocks the final source-only dispatch callback."""

from uuid import UUID

import pytest
from test_source_edit_native import activation, prepared
from worker_reply_fixtures import authenticated_operator

from firefly_forge import source_edit_continuations as module
from firefly_forge.identity_persistence import ForgeIdentityUserRepository
from firefly_forge.project_access import AccessDenied


@pytest.mark.parametrize("waitpoint", ["history", "records", "ownership"])
@pytest.mark.parametrize("revocation", ["disabled", "identity"])
async def test_native_final_dispatch_refuses_committed_admin_revocation(
    store, tmp_path, monkeypatch, waitpoint, revocation
):
    async with authenticated_operator(store) as actor:
        env = await prepared(store, tmp_path, monkeypatch)
        await env.service.activate_source_edit(env.run_id, activation(await env.service.source_candidate(env.run_id)))
        run = await store.get_run(env.run_id)
        await module.assert_source_final_budget(store, env.journal, run, "verify", "f" * 32)
        before = await env.journal.entries(env.run_id)
        launched = []

        async def revoke():
            async with store._unit():
                user = await ForgeIdentityUserRepository().find_by_id(actor.id)
                assert user is not None
                if revocation == "disabled":
                    user.enabled = False
                else:
                    user.subject += "-replaced"
                await ForgeIdentityUserRepository().save(user)

        if waitpoint == "history":
            original = module.retained_command_history

            async def history(*args, **kwargs):
                result = await original(*args, **kwargs)
                await revoke()
                return result

            monkeypatch.setattr(module, "retained_command_history", history)
        elif waitpoint == "records":
            original = env.journal.sandbox_executions

            async def records(*args):
                result = await original(*args)
                await revoke()
                return result

            monkeypatch.setattr(env.journal, "sandbox_executions", records)
        else:
            original = store.assert_ownership
            calls = 0

            async def ownership():
                nonlocal calls
                await original()
                calls += 1
                if calls == 2:
                    await revoke()

            monkeypatch.setattr(store, "assert_ownership", ownership)

        async def dispatch():
            await module.assert_source_final_budget(store, env.journal, run, "verify", "f" * 32)
            launched.append("public-final-command-callback")

        with pytest.raises(AccessDenied, match="administrator is no longer current"):
            await dispatch()
        assert launched == []
        if revocation == "disabled":
            with pytest.raises(AccessDenied):
                await env.journal.entries(env.run_id)
        async with store._unit():
            records = await env.journal.records.find_for_run(UUID(env.run_id))
            assert [{**row.data, "digest": row.digest} for row in records] == before
        async with store._unit():
            user = await ForgeIdentityUserRepository().find_by_id(actor.id)
            assert user is not None
            assert not user.enabled if revocation == "disabled" else user.subject.endswith("-replaced")
