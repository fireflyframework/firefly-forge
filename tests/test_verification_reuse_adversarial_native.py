# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Fictional native CAS and drift controls preserve the charged source-check history."""

import asyncio
import copy
import json
import sys
import time
from datetime import UTC, datetime, timedelta
from functools import wraps

import httpx2
import pytest
from test_agentic_checkpoint_activation_host import result
from test_bridge_completion_native import mock_model_requests
from test_final_outcome_continuation_native import (
    test_native_consumed_final_acceptance_releases_existing_verification_without_replay as one_check_handoff,
)
from test_verification_reuse_native import queued_build

from firefly_forge.agent_checkpoints import AgentCheckpoints
from firefly_forge.journal import Journal
from firefly_forge.native_terminal_completions import NativeAcceptance
from firefly_forge.verification_result_reuse import PreparedVerificationReuse
from firefly_forge.workspace_tools import WorkspaceTools

pytest_plugins = ["test_invalid_read_continuity_native"]


@pytest.fixture(scope="module", autouse=True)
def fictional_identity_window():
    """Sign finite fictional authority for the explicitly chosen one-hour lab observation."""
    import test_bridge_completion_native as fixtures

    original = fixtures.oidc_fixture

    async def configured(*args, **kwargs):
        values = await original(*args, **kwargs)
        issue = values[-1]

        def signed(*arguments, **claims):
            claims.setdefault("exp", int(time.time()) + 3600)
            return issue(*arguments, **claims)

        return (*values[:-1], signed)

    with pytest.MonkeyPatch.context() as changed:
        changed.setattr(fixtures, "oidc_fixture", configured)
        yield


async def fresh_dispatch(c, monkeypatch):
    provider = c.engine.provider_factory("bridge")
    model = provider.model()
    original = model.client._client._transport
    old = {e["idempotency_key"] for e in await c.journal.entries(c.run_id) if e["kind"] == "agent_consume"}
    posts, consumed = [], []
    c.fresh_native_posts, c.fresh_native_consumes = posts, consumed
    consume = AgentCheckpoints._consume

    def transport(request):
        if request.method == "GET":
            return original.handler(request)
        posts.append(request.content)
        assert len(posts) == 1
        return httpx2.Response(
            200,
            json=result(
                json.dumps(dict(summary="Current native read-only final report", qa_report=None, worker_feedback=None))
            ),
        )

    @wraps(consume)
    async def consume_new(self, run, intent, body, current, reader):
        assert intent["idempotency_key"] not in old
        consumed.append(intent["idempotency_key"])
        return await consume(self, run, intent, body, current, reader)

    def forbidden(callback):
        @wraps(callback)
        async def refuse(*args, **kwargs):
            raise AssertionError("No prior source callback may replay")

        return refuse

    monkeypatch.setattr(AgentCheckpoints, "_consume", consume_new)
    monkeypatch.setattr(WorkspaceTools, "read_file", forbidden(WorkspaceTools.read_file))
    monkeypatch.setattr(WorkspaceTools, "write_file", forbidden(WorkspaceTools.write_file))
    monkeypatch.setattr(model.client._client, "_transport", httpx2.MockTransport(transport))
    with mock_model_requests(model):
        await c.engine.execute_run(c.run_id)
    return await c.store.get_run(c.run_id), posts, consumed


@pytest.mark.parametrize("stage", ["prepare", "commit"])
async def test_native_raw_retained_proof_numeric_drift_cannot_receive_terminal_acceptance(
    native_continuity, monkeypatch, stage
):
    c = native_continuity
    await c.store.update(c.run_id, repair_budget=1)
    provider = c.engine.provider_factory("bridge")
    execute, consume = provider.execute, AgentCheckpoints._consume
    await one_check_handoff(c, monkeypatch)
    monkeypatch.setattr(provider, "execute", execute)
    monkeypatch.setattr(AgentCheckpoints, "_consume", consume)
    prefix = copy.deepcopy(await c.journal.entries(c.run_id))
    original_receipt, original_commit = c.engine._receipt, NativeAcceptance.commit
    mutated = []

    async def drift():
        run = await c.store.get_run(c.run_id)
        retained = copy.deepcopy(run.get("usage", {}).get("retained_source"))
        if retained is not None and not mutated:
            assert type(retained["attempt"]) is int
            retained["attempt"] = float(retained["attempt"])
            await c.store.update(c.run_id, usage={**run["usage"], "retained_source": retained})
            mutated.append(True)

    @wraps(original_receipt)
    async def receipt(*args, **kwargs):
        if stage == "prepare":
            await drift()
        return await original_receipt(*args, **kwargs)

    @wraps(original_commit)
    async def commit(self, acceptance):
        if stage == "commit" and self.retained_source is not None:
            await drift()
        return await original_commit(self, acceptance)

    monkeypatch.setattr(c.engine, "_receipt", receipt)
    monkeypatch.setattr(NativeAcceptance, "commit", commit)
    final, posts, consumed = await fresh_dispatch(c, monkeypatch)
    entries = await c.journal.entries(c.run_id)
    assert mutated == [True]
    assert final["status"] != "succeeded", "Raw float proof must not compare equal to typed integer custody"
    assert len(posts) == len(consumed) == 1
    assert entries[: len(prefix)] == prefix
    assert sum(e["kind"] == "terminal_completion_receipt" for e in entries) == 1
    assert not any(e["kind"].startswith("verification_reuse_") for e in entries)
    assert len(await c.journal.sandbox_executions(c.run_id)) == 12
    assert final["repair_budget"] == 1 and final["attempt"] == 3


@pytest.mark.skipif(sys.platform != "darwin", reason="Confined source-check reuse requires Darwin host")
async def test_native_reuse_revalidates_private_inputs_and_current_actor_then_commits_one_concurrent_intent(
    native_continuity, monkeypatch, tmp_path
):
    c = native_continuity
    from firefly_forge import verification_scope_attestation as attestation
    from firefly_forge.identity_persistence import ForgeIdentityUserRepository
    from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied

    dependency = tmp_path / "declared-tool-input"
    dependency.mkdir()
    marker = dependency / "tool-data.txt"
    marker.write_text("version one\n")
    prerequisite = tmp_path / "neutral-prerequisite.json"
    prerequisite.write_text('{"version":1}\n')
    monkeypatch.setenv("QA_REUSE_PREREQUISITE_FILE", str(prerequisite))
    roots = attestation._roots
    monkeypatch.setattr(attestation, "_roots", lambda *args: [*roots(*args), dependency])
    calls, _, _, prefix = await queued_build(c, monkeypatch, mixed=False)
    begin = PreparedVerificationReuse.begin
    probes, concurrent = [], []

    class ProbeRollback(Exception):
        pass

    @wraps(begin)
    async def guarded(self):
        if probes:
            return await begin(self)
        with monkeypatch.context() as changed:
            changed.setenv("QA_REUSE_OWNED_INPUT", "changed")
            with pytest.raises(ValueError, match="inputs changed"):
                await self.revalidate()
            probes.append("environment")
        marker.write_text("version two\n")
        with pytest.raises(ValueError, match="inputs changed"):
            await self.revalidate()
        marker.write_text("version one\n")
        probes.append("dependency")
        prerequisite.write_text('{"version":2}\n')
        with pytest.raises(ValueError, match="inputs changed"):
            await self.revalidate()
        prerequisite.write_text('{"version":1}\n')
        probes.append("prerequisite")
        with monkeypatch.context() as changed:
            changed.setattr(c.engine.sandbox, "credential_registry", lambda: {"API_KEY": "fictional-changed-key"})
            with pytest.raises(ValueError, match="inputs changed"):
                await self.revalidate()
        probes.append("credential")
        with pytest.raises(ProbeRollback):
            async with c.store._unit():
                actor = await ForgeIdentityUserRepository().find_by_id(CURRENT_PRINCIPAL.get().id)
                actor.enabled = False
                await ForgeIdentityUserRepository().save(actor)
                with pytest.raises(AccessDenied):
                    await self.current()
                probes.append("actor")
                raise ProbeRollback
        with pytest.raises(ProbeRollback):
            async with c.store._unit():
                actor = await ForgeIdentityUserRepository().find_by_id(CURRENT_PRINCIPAL.get().id)
                actor.api_roles_expires_at = datetime.now(UTC) - timedelta(seconds=1)
                await ForgeIdentityUserRepository().save(actor)
                with pytest.raises(AccessDenied):
                    await self.current()
                probes.append("expired_actor")
                raise ProbeRollback
        append = c.journal._append

        @wraps(append)
        async def rollback(*args, **kwargs):
            value = await append(*args, **kwargs)
            if args[3] == "verification_reuse_intent":
                raise ProbeRollback
            return value

        with monkeypatch.context() as changed:
            changed.setattr(c.journal, "_append", rollback)
            with pytest.raises(ProbeRollback):
                await begin(self)
        assert not any(e["kind"] == "verification_reuse_intent" for e in await c.journal.entries(c.run_id))
        probes.append("rollback")
        submissions = await asyncio.gather(begin(self), begin(self), begin(self), return_exceptions=True)
        assert submissions == [None, None, None], submissions
        concurrent.extend(e for e in await c.journal.entries(c.run_id) if e["kind"] == "verification_reuse_intent")
        assert len(concurrent) == 1
        return await begin(self)

    monkeypatch.setattr(PreparedVerificationReuse, "begin", guarded)
    final, posts, consumed = await fresh_dispatch(c, monkeypatch)
    assert final["status"] == "succeeded", final.get("error")
    assert (
        probes == ["environment", "dependency", "prerequisite", "credential", "actor", "expired_actor", "rollback"]
        and len(concurrent) == 1
    )
    assert len(posts) == len(consumed) == 1 and len(calls) == 7
    entries = await c.journal.entries(c.run_id)
    assert entries[: len(prefix)] == prefix and Journal.verify_chain(entries)
    assert sum(e["kind"] == "verification_reuse_intent" for e in entries) == 1
    assert sum(e["kind"] == "verification_reuse_receipt" for e in entries) == 1
    assert len(await c.journal.sandbox_executions(c.run_id)) == 18


@pytest.mark.parametrize("drift", ["source", "owner"])
@pytest.mark.skipif(sys.platform != "darwin", reason="Confined source-check reuse requires Darwin host")
async def test_native_reuse_refuses_current_source_or_owner_drift_without_new_command(
    native_continuity, monkeypatch, drift
):
    c = native_continuity
    calls, _, _, prefix = await queued_build(c, monkeypatch, mixed=False)
    begin = PreparedVerificationReuse.begin
    if drift == "source":
        (c.engine.workspaces / c.run_id / "existing.py").write_text("VALUE = 99\n")
    else:

        @wraps(begin)
        async def revoke(self):
            await c.store.release_ownership()
            return await begin(self)

        monkeypatch.setattr(PreparedVerificationReuse, "begin", revoke)
    try:
        final, posts, consumed = await fresh_dispatch(c, monkeypatch)
    except RuntimeError as error:
        assert drift == "owner" and "ownership" in str(error).lower()
        final = await c.store.get_run(c.run_id)
        posts, consumed = c.fresh_native_posts, c.fresh_native_consumes
    assert final["status"] != "succeeded"
    assert len(calls) == 7 and len(await c.journal.sandbox_executions(c.run_id)) == 18
    entries = await c.journal.entries(c.run_id)
    assert entries[: len(prefix)] == prefix
    assert not any(e["kind"] == "verification_reuse_intent" for e in entries)
    assert final["repair_budget"] == 1 and final["attempt"] == 3
    assert len(posts) == len(consumed) == (0 if drift == "source" else 1)


@pytest.mark.skipif(sys.platform != "darwin", reason="Confined source-check reuse requires Darwin host")
async def test_native_failed_handoff_cannot_be_promoted_to_reused_passed_checks(native_continuity, monkeypatch):
    c = native_continuity
    calls, _, _, prefix = await queued_build(c, monkeypatch, mixed=False, failed_check=True)
    final, posts, consumed = await fresh_dispatch(c, monkeypatch)
    assert final["status"] != "succeeded"
    assert len(posts) == len(consumed) == 1 and len(calls) == 7
    entries = await c.journal.entries(c.run_id)
    assert entries[: len(prefix)] == prefix
    assert not any(e["kind"].startswith("verification_reuse_") for e in entries)
    assert len(await c.journal.sandbox_executions(c.run_id)) == 18


@pytest.mark.skipif(sys.platform != "darwin", reason="Confined source-check reuse requires Darwin host")
async def test_native_unknown_handoff_result_is_never_reexecuted_or_reused(native_continuity, monkeypatch):
    c = native_continuity
    from firefly_forge.runner_backends import HostRunnerBackend
    from firefly_forge.verification_handoffs import VerificationHandoffs

    run = HostRunnerBackend.run
    dispatched = []

    @wraps(run)
    async def lost(self, *args, **kwargs):
        result = await run(self, *args, **kwargs)
        dispatched.append(result.exit_code)
        raise RuntimeError("Fictional lost return after the owned command exited")

    monkeypatch.setattr(HostRunnerBackend, "run", lost)
    calls, _, final, prefix = await queued_build(c, monkeypatch, mixed=False, expect_held=True)
    assert dispatched == [0] and len(calls) == 1
    assert final["verification_handoff"]["status"] == "held"
    before = await c.journal.sandbox_executions(c.run_id)
    assert len(before) == 12
    await VerificationHandoffs(c.store, c.journal, c.engine).execute(c.run_id)
    assert await c.journal.sandbox_executions(c.run_id) == before
    entries = await c.journal.entries(c.run_id)
    assert entries[: len(prefix)] == prefix
    assert dispatched == [0] and len(c.posts) == 4
    assert not any(e["kind"].startswith("verification_reuse_") for e in entries)


@pytest.mark.skipif(sys.platform != "darwin", reason="Confined source-check reuse requires Darwin host")
@pytest.mark.parametrize("late", ["environment", "dependency", "source"])
async def test_native_late_scope_drift_rolls_back_verify_and_reuse_acceptance(
    native_continuity, monkeypatch, tmp_path, late
):
    c = native_continuity
    from firefly_forge import verification_scope_attestation as attestation

    dependency = tmp_path / "late-dependency"
    dependency.mkdir()
    marker = dependency / "tool-data.txt"
    marker.write_text("version one\n")
    roots = attestation._roots
    monkeypatch.setattr(attestation, "_roots", lambda *args: [*roots(*args), dependency])
    calls, _, _, prefix = await queued_build(c, monkeypatch, mixed=False)
    receipt = c.engine._receipt
    drifted = []

    @wraps(receipt)
    async def late_change(identifier, key, evidence, **kwargs):
        if not drifted and "checks" in evidence:
            if late == "environment":
                monkeypatch.setenv("QA_REUSE_LATE_ENV", "changed")
            elif late == "dependency":
                marker.write_text("version two\n")
            else:
                (c.engine.workspaces / c.run_id / "existing.py").write_text("VALUE = 99\n")
            drifted.append(True)
        return await receipt(identifier, key, evidence, **kwargs)

    monkeypatch.setattr(c.engine, "_receipt", late_change)
    final, posts, consumed = await fresh_dispatch(c, monkeypatch)
    assert drifted == [True] and final["status"] != "succeeded"
    assert len(posts) == len(consumed) == 1 and len(calls) == 7
    assert len(await c.journal.sandbox_executions(c.run_id)) == 18
    entries = await c.journal.entries(c.run_id)
    assert entries[: len(prefix)] == prefix and Journal.verify_chain(entries)
    assert sum(e["kind"] == "verification_reuse_intent" for e in entries) == 1
    assert not any(e["kind"] == "verification_reuse_receipt" for e in entries)
    assert not any(e["kind"] == "receipt" and e["task_id"] == "verify" for e in entries)
    assert not final.get("final_verification")
    assert final["attempt"] == final["implementation_rounds"] == 3 and final["repair_budget"] == 1
