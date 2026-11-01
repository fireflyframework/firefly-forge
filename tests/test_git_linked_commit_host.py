# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Completed SCM QA reaches real fixture Git; commit checks use native memory UoW."""

import copy
from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pyfly.data.transaction import TransactionTemplate, current_unit_of_work
from test_git_delivery_local import git
from test_scm_linked_verification_consumer_host import consent

from firefly_forge.git_deliveries import GitDeliveries
from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied

pytest_plugins = ["test_scm_linked_verification_consumer_host"]


@pytest.fixture(autouse=True)
def original_git_consent(monkeypatch, component_exists_before_fictional_history_is_frozen):
    import test_invalid_read_native_protocol_host as protocol
    import test_original_selection_readmission_host as historical

    policy = dict(version=1, enabled=True, every_units=1, on_pause=True, remote="local", max_checkpoints=50)
    original_history, activated = historical.historical_case, protocol.activated

    def history():
        value = original_history()
        value["run"]["git_delivery_policy"] = copy.deepcopy(policy)
        return value

    async def activate(*args):
        from firefly_forge.git_workspace import metadata_digest

        env = await activated(*args)
        branch = "forge/" + env.store.run.id.hex[:12]
        git(env.workspace, "checkout", "-qb", branch)
        env.store.run.data.update(
            git_delivery_policy=copy.deepcopy(policy),
            base_revision=git(env.workspace, "rev-parse", "HEAD"),
            branch=branch,
            git_metadata_digest=metadata_digest(env.workspace),
        )
        return env

    monkeypatch.setattr(historical, "historical_case", history)
    monkeypatch.setattr(protocol, "activated", activate)


@pytest.fixture
async def scm_git(scm_case, monkeypatch):

    s, c = scm_case, scm_case.case
    env = c.env
    token = CURRENT_PRINCIPAL.set(s.actor)
    try:
        s.state["confirmed"] = True
        body = consent(s.scope, await c.api.candidate(env.run_id))
        await c.api.request(env.run_id, body)
        worker = CURRENT_PRINCIPAL.set(None)
        try:
            await c.api.execute(body.request_id)
        finally:
            CURRENT_PRINCIPAL.reset(worker)
        result = await c.api.get(env.run_id, body.request_id)
    finally:
        CURRENT_PRINCIPAL.reset(token)
    assert result["status"] == "completed", result
    return await linked_git_case(c, result, monkeypatch, s=s)


async def linked_git_case(c, result, monkeypatch, *, s=None):
    from firefly_forge import (
        agentic_checkpoint_invocation,
        configuration,
        git_linked_verification,
        linked_verifications,
    )
    from firefly_forge.git_delivery_policy import GitDeliveryPolicies
    from firefly_forge.git_diff_artifacts import retain_git_diff
    from firefly_forge.git_diff_models import GitDiffBudget
    from firefly_forge.identity_persistence import ForgeIdentityUserRepository
    from firefly_forge.project_persistence import ForgeProjectRepository
    from firefly_forge.source_snapshot import SourceDiffSnapshotV1, diff_artifact_root

    env = c.env
    scalar = {key: result["candidate"][key] for key in ("runtime_contract", "policy_digest", "profile_digest")}
    monkeypatch.setattr(agentic_checkpoint_invocation, "_runtime_material_digest", lambda: scalar["runtime_contract"])
    monkeypatch.setattr(linked_verifications, "_policy_material_digest", lambda: scalar["policy_digest"])
    monkeypatch.setattr(linked_verifications, "profile_digest", lambda: scalar["profile_digest"])
    locks, commits = [], []
    after = {"action": None}

    async def execute(*args, **kwargs):
        return None

    @asynccontextmanager
    async def owned():
        async with TransactionTemplate(env.manager).transaction():
            yield SimpleNamespace(execute=execute)
            if after["action"] is not None:
                after["action"]()

    env.store.source_budget_admission_unit = owned
    original_identity = ForgeIdentityUserRepository.find_by_id

    async def identity(self, key, **kwargs):
        locks.append(key.hex)
        return await original_identity(self, key, **kwargs)

    async def project(*args, **kwargs):
        run = await env.store.get_run(env.run_id)
        return SimpleNamespace(
            id=env.store.run.id, data=dict(git_delivery_policy=dict(policy=run["git_delivery_policy"]))
        )

    async def saved_policy(self, project_id):
        return dict(policy=(await env.store.get_run(env.run_id))["git_delivery_policy"])

    monkeypatch.setattr(ForgeIdentityUserRepository, "find_by_id", identity)
    monkeypatch.setattr(ForgeProjectRepository, "find_by_id", project)
    monkeypatch.setattr(GitDeliveryPolicies, "snapshot", saved_policy)
    if hasattr(git_linked_verification, "_GitCommitFence"):
        before_commit = git_linked_verification._GitCommitFence.before_commit

        async def committed(self, read_only):
            assert current_unit_of_work() is self.unit and self.unit.owner_task is self.owner_task
            commits.append(self)
            return await before_commit(self, read_only)

        monkeypatch.setattr(git_linked_verification._GitCommitFence, "before_commit", committed)

    async def snapshot(workspace, base, **kwargs):
        git(workspace, "add", "--all", "--", ".")
        budget = kwargs.get("budget") or GitDiffBudget()
        scan = await env.store.source_patch_scanner(settings=kwargs.get("secrets"))
        ref = await retain_git_diff(
            workspace, base, diff_artifact_root(env.engine.workspaces, env.run_id), budget=budget, scan_chunk=scan
        )
        return (
            [],
            SourceDiffSnapshotV1(artifact=ref, preview="", preview_bytes=0, preview_truncated=ref.byte_count > 0),
            git(workspace, "write-tree"),
        )

    monkeypatch.setattr(env.engine, "_snapshot", snapshot)
    deliveries = GitDeliveries(env.store, env.engine, env.journal, workspace=lambda _: env.workspace)
    assert deliveries.authorization is None
    return SimpleNamespace(
        s=s,
        c=c,
        env=env,
        service=deliveries,
        scalars=scalar,
        locks=locks,
        commits=commits,
        after=after,
        generations=configuration._write_generations,
    )


async def test_completed_scm_verification_commits_fixture_git_with_native_before_commit(scm_git):
    c, env = scm_git, scm_git.env
    original = copy.deepcopy(await env.store.get_run(env.run_id))
    rows = copy.deepcopy(await env.journal.sandbox_executions(env.run_id))
    base = git(env.workspace, "rev-parse", "HEAD")
    assert original["git_delivery_policy"]["enabled"] and original["base_revision"] == base
    receipt = await c.service.checkpoint(
        env.run_id, request_id=uuid4().hex, expected_head=base, actor=original["execution_actor"]
    )
    assert git(env.workspace, "rev-parse", "HEAD^") == base
    assert git(env.workspace, "rev-parse", "HEAD") == receipt["revision"]
    assert c.commits and all(cut.prepared.scm is not None for cut in c.commits)
    assert all(cut.expected is not None and cut.index is not None for cut in c.commits)
    assert all(all(stream.closed for stream in cut.prepared.payload.streams) for cut in c.commits)
    assert c.s.actor.id.hex in c.locks
    current = await env.store.get_run(env.run_id)
    for key in ("usage", "attempt", "implementation_rounds", "sandbox_snapshot", "verification_handoff"):
        assert current.get(key) == original.get(key)
    assert current["checkpoint_revision"] == receipt["revision"]
    assert await env.journal.sandbox_executions(env.run_id) == rows
    assert len(c.c.calls) == 1


async def test_native_commit_refuses_late_authority_and_phase_mutations_without_git_replay(scm_git):
    c, env = scm_git, scm_git.env
    original = copy.deepcopy(await env.store.get_run(env.run_id))
    base = git(env.workspace, "rev-parse", "HEAD")
    generation = env.store.configuration.write_generation
    key = env.store.configuration._generation_key
    scalar = copy.deepcopy(c.scalars)
    old_calls = (len(env.posts), len(c.c.calls))
    content = (env.workspace / "existing.py").read_bytes()
    for change in (
        "grant",
        "seal",
        "generation",
        "runtime",
        "profile",
        "inputs",
        "source",
        "payload",
        "usage",
        "index",
    ):

        def mutate(change=change):
            if change == "grant":
                c.s.state["grant_current"] = False
            elif change == "seal":
                c.s.state["mutate_view"] = True
            elif change == "generation":
                c.generations[key] = object()
            elif change == "runtime":
                c.scalars["runtime_contract"] = "0" * 64
            elif change == "profile":
                c.scalars["profile_digest"] = "0" * 64
            elif change == "inputs":
                c.c.inputs_state["value"] = "late-drift"
            elif change == "source":
                env.store.run.data["brief"] = "Changed original source scope"
            elif change == "payload":
                (env.workspace / "existing.py").write_text("ACTUAL_SOURCE_CHANGED = True\n")
            elif change == "usage":
                env.store.run.data["usage"]["requests"] += 1
            else:
                blob = git(env.workspace, "hash-object", "-w", "--", "existing.py")
                git(env.workspace, "update-index", "--add", "--cacheinfo", "100644," + blob + ",index-probe.py")

        c.after["action"] = mutate
        try:
            with pytest.raises((ValueError, AccessDenied)):
                async with c.service._owned(env.run_id, original["execution_actor"]):
                    await env.store.update(env.run_id, git_delivery_summary={"controlled": change})
        finally:
            c.after["action"] = None
            c.s.state.update(grant_current=True, mutate_view=False)
            c.generations[key] = generation
            c.scalars.update(scalar)
            c.c.inputs_state["value"] = "original"
            if change == "payload":
                (env.workspace / "existing.py").write_bytes(content)
            if change == "index":
                git(env.workspace, "read-tree", "HEAD")
        assert (await env.store.get_run(env.run_id)).get("usage") == original.get("usage")
        assert "git_delivery_summary" not in env.store.run.data
        assert git(env.workspace, "rev-parse", "HEAD") == base
        assert all(all(stream.closed for stream in cut.prepared.payload.streams) for cut in c.commits)
    assert c.commits and "rollback" in env.manager.events
    assert (len(env.posts), len(c.c.calls)) == old_calls
