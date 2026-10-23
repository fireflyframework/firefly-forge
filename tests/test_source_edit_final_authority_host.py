# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Final source-only dispatch retains the sealed administrator across awaited reads."""
# ruff: noqa: F811

import pytest
from pyfly.data.relational.sqlalchemy import LockMode
from test_source_edit_continuations_host import controlled_metadata, source_environment, source_request  # noqa: F401
from test_workspace_dependency_reconciliation import legacy_workspace  # noqa: F401
from test_workspace_reconciliation_journal import environment_journal  # noqa: F401
from test_workspace_reconciliation_service import reconciliation_service  # noqa: F401

from firefly_forge import source_edit_continuations as module
from firefly_forge.identity_persistence import ForgeIdentityUserRepository
from firefly_forge.project_access import AccessDenied, ProjectAccess


async def activated(env, tmp_path, monkeypatch):
    env = await source_environment(env, tmp_path, monkeypatch)
    await env.service.activate_source_edit(
        env.root.name, source_request(await env.service.source_candidate(env.root.name))
    )
    return env


@pytest.mark.parametrize("waitpoint", ["history", "records", "ownership"])
@pytest.mark.parametrize("revocation", ["disabled", "identity", "administrator"])
async def test_final_dispatch_rechecks_admin_after_awaited_reads(
    reconciliation_service, tmp_path, monkeypatch, waitpoint, revocation
):
    env = await activated(reconciliation_service, tmp_path, monkeypatch)
    current = True
    launched = []
    original_permission = ProjectAccess.authorize_actor

    async def permission(*args):
        return current and await original_permission(*args)

    monkeypatch.setattr(ProjectAccess, "authorize_actor", permission)

    def revoke():
        nonlocal current
        if revocation == "disabled":
            env.identity.enabled = False
        elif revocation == "identity":
            env.identity.subject += "-replaced"
        else:
            current = False

    if waitpoint == "history":
        original = module.retained_command_history

        async def history(*args, **kwargs):
            result = await original(*args, **kwargs)
            revoke()
            return result

        monkeypatch.setattr(module, "retained_command_history", history)
    elif waitpoint == "records":
        original = env.journal.sandbox_executions

        async def records(*args):
            result = await original(*args)
            revoke()
            return result

        monkeypatch.setattr(env.journal, "sandbox_executions", records)
    else:
        original = env.store.assert_ownership
        calls = 0

        async def ownership():
            nonlocal calls
            await original()
            calls += 1
            if calls == 2:
                revoke()

        monkeypatch.setattr(env.store, "assert_ownership", ownership)

    async def dispatch():
        await module.assert_source_final_budget(env.store, env.journal, env.store.run.data, "verify", "f" * 32)
        launched.append("public-final-command-callback")

    with pytest.raises(AccessDenied, match="administrator is no longer current"):
        await dispatch()
    assert launched == []


async def test_final_dispatch_current_admin_is_locked_twice(reconciliation_service, tmp_path, monkeypatch):
    env = await activated(reconciliation_service, tmp_path, monkeypatch)
    original = ForgeIdentityUserRepository.find_by_id
    locks = []

    async def identity(*args, **kwargs):
        locks.append(kwargs.get("lock"))
        return await original(*args, **kwargs)

    monkeypatch.setattr(ForgeIdentityUserRepository, "find_by_id", identity)
    await module.assert_source_final_budget(env.store, env.journal, env.store.run.data, "verify", "f" * 32)
    assert locks == [LockMode.PESSIMISTIC_WRITE, LockMode.PESSIMISTIC_WRITE]


async def test_final_dispatch_revoked_admin_fails_before_history(reconciliation_service, tmp_path, monkeypatch):
    env = await activated(reconciliation_service, tmp_path, monkeypatch)
    env.identity.enabled = False

    async def forbidden(*args, **kwargs):
        pytest.fail("Early authorization must refuse before retained-history IO")

    monkeypatch.setattr(module, "retained_command_history", forbidden)
    with pytest.raises(AccessDenied):
        await module.assert_source_final_budget(env.store, env.journal, env.store.run.data, "verify", "f" * 32)
