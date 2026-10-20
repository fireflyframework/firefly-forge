# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import copy
from types import SimpleNamespace
from uuid import uuid4

import pytest
from test_automatic_verification_journal import Memory

from firefly_forge.configuration import ConfigurationStore, ProviderConfiguration
from firefly_forge.git_workspace import metadata_digest
from firefly_forge.identity import Principal
from firefly_forge.identity_persistence import ForgeIdentityUserRepository
from firefly_forge.journal import Journal
from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied, ProjectAccess
from firefly_forge.provider_recoveries import ProviderRecoveries
from firefly_forge.provider_recovery_models import (
    ProviderAttemptClosed,
    ProviderProcessOutcome,
    ProviderRecoveryAdmission,
)


@pytest.fixture
async def recovery(tmp_path, monkeypatch):
    memory = Memory()
    memory.schema = "host_provider_recovery"
    memory.configuration = ConfigurationStore(tmp_path / "private")
    memory.credential_registry = dict
    memory.reject_credentials = lambda value: None
    memory.run.data.update(
        id=memory.id.hex,
        status="failed",
        agent="codex",
        active_task="implement",
        attempt=2,
        implementation_rounds=1,
        repair_budget=0,
        error="Provider failed",
        repository_id=uuid4().hex,
        **memory.configuration.snapshot("codex"),
    )
    memory.run.status = "failed"
    memory.task.status, memory.task.attempt = "running", 2
    memory.task.data = {"idempotency_key": "b" * 32}
    workspace = tmp_path / "workspaces" / memory.id.hex
    (workspace / ".git").mkdir(parents=True)
    (workspace / ".git" / "HEAD").write_text("a" * 40 + "\n")
    (workspace / "partial.py").write_text("VALUE = 1\n")
    memory.run.data["git_metadata_digest"] = metadata_digest(workspace)
    journal = Journal(memory, records=memory)

    async def task_rows(*args):
        return [memory.task]

    journal.tasks = SimpleNamespace(save=memory.save, find_for_run=task_rows)

    async def task(*args):
        return memory.task

    async def authorized(*args, **kwargs):
        return True

    async def empty(*args):
        return []

    async def identity(*args, **kwargs):
        return SimpleNamespace(enabled=True, issuer="local", subject="operator")

    journal._task = task
    journal.sandbox_executions = empty
    memory.assert_campaign_current = authorized
    monkeypatch.setattr(ProjectAccess, "current_administrator", authorized)
    monkeypatch.setattr(ProjectAccess, "authorize_execution", authorized)
    monkeypatch.setattr(ProjectAccess, "authorize_actor", authorized)
    monkeypatch.setattr(ForgeIdentityUserRepository, "find_by_id", identity)
    actor = Principal(uuid4(), "local", "operator", authentication_method="local_bootstrap")
    token = CURRENT_PRINCIPAL.set(actor)
    await journal._append(memory.id.hex, "implement", "a" * 32, "intent", {})
    await journal._append(memory.id.hex, "implement", "b" * 32, "intent", {})
    engine = SimpleNamespace(workspaces=workspace.parent, active={}, tool_broker=None)
    service = ProviderRecoveries(memory, journal, engine)
    yield SimpleNamespace(memory=memory, journal=journal, service=service, engine=engine, workspace=workspace)
    CURRENT_PRINCIPAL.reset(token)


def body(candidate, **changes):
    return ProviderRecoveryAdmission.model_validate(
        {
            "request_id": "c" * 32,
            "candidate_digest": candidate["digest"],
            "agent": "codex",
            "legacy_attestation": {
                "provenance": "legacy_operator_attestation",
                "evidence_digest": "e" * 64,
                "trusted_operator_observation": True,
                "process_terminated": True,
                "process_group_drained": True,
                "no_accepted_final_result": True,
                "exit_code": -25,
            },
            **changes,
        }
    )


def configure_executable(e, tmp_path):
    executable = tmp_path / "official-cli"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o755)
    e.memory.configuration.update(
        "codex", ProviderConfiguration(values={"executable": str(executable), "model": "different-model"})
    )
    return executable


async def test_explicit_cli_adoption_changes_only_one_dispatch_executable_and_keeps_exact_receipt(recovery, tmp_path):
    e = recovery
    executable = configure_executable(e, tmp_path)
    original = e.memory.configuration.load_snapshot(e.memory.run.data["config_revision"])
    revision = e.memory.run.data["config_revision"]
    ordinary = await e.service.candidate(e.memory.id.hex, "codex")
    assert "executable_adoption" not in ordinary
    candidate = await e.service.candidate(e.memory.id.hex, "codex", adopt_current_executable=True)
    assert candidate["executable_adoption"]["path"] == str(executable)
    assert candidate["digest"] != ordinary["digest"]
    request = body(candidate, adopt_current_executable=True)
    receipt = await e.service.admit(e.memory.id.hex, request)
    assert receipt == await e.service.admit(e.memory.id.hex, request)
    assert receipt == await e.service.get(e.memory.id.hex, request.request_id)
    assert e.memory.run.data["config_revision"] == revision
    assert e.memory.configuration.load_snapshot(revision) == original
    state = e.memory.run.data["provider_recovery"]
    selected = e.service.execution_settings(e.memory.run.data, original, state["task_key"])
    assert selected == {**original, "FORGE_CODEX_EXECUTABLE": str(executable)}
    assert e.service.execution_settings(e.memory.run.data, original, "f" * 32) == original
    e.memory.run.data["provider_recovery"]["status"] = "completed"
    assert e.service.execution_settings(e.memory.run.data, original, state["task_key"]) == original


@pytest.mark.parametrize("review_choice", [False, True])
async def test_reviewed_cli_choice_cannot_change_during_admission(recovery, tmp_path, review_choice):
    configure_executable(recovery, tmp_path)
    candidate = await recovery.service.candidate(
        recovery.memory.id.hex, "codex", adopt_current_executable=review_choice
    )
    with pytest.raises(ValueError, match="candidate changed"):
        await recovery.service.admit(
            recovery.memory.id.hex, body(candidate, adopt_current_executable=not review_choice)
        )
    assert recovery.memory.run.status == "failed"


@pytest.mark.parametrize("boundary", ["admission", "dispatch", "launch", "result"])
async def test_adopted_executable_byte_drift_refuses_every_boundary(recovery, tmp_path, boundary):
    e = recovery
    executable = configure_executable(e, tmp_path)
    candidate = await e.service.candidate(e.memory.id.hex, "codex", adopt_current_executable=True)
    request = body(candidate, adopt_current_executable=True)
    if boundary != "admission":
        await e.service.admit(e.memory.id.hex, request)
        state = e.memory.run.data["provider_recovery"]
        if boundary in {"launch", "result"}:
            await e.service.dispatch(e.memory.id.hex, state["task_key"])
            e.memory.run.status = e.memory.run.data["status"] = "developing"
    executable.write_text("#!/bin/sh\nexit 9\n")
    with pytest.raises(ValueError, match="changed|cut|context"):
        if boundary == "admission":
            await e.service.admit(e.memory.id.hex, request)
        elif boundary == "dispatch":
            await e.service.dispatch(e.memory.id.hex, state["task_key"])
        elif boundary == "launch":
            await e.service.assert_launch(e.memory.id.hex, state["task_key"])
        else:
            await e.service.assert_result(e.memory.id.hex, state["task_key"])


@pytest.mark.parametrize("unsafe", ["relative", "symlink", "writable", "directory", "workspace", "oversize"])
async def test_cli_adoption_rejects_untrusted_executable_paths(recovery, tmp_path, unsafe):
    e = recovery
    executable = configure_executable(e, tmp_path)
    if unsafe == "relative":
        configured = "official-cli"
    elif unsafe == "symlink":
        link = tmp_path / "alias"
        link.symlink_to(executable)
        configured = str(link)
    elif unsafe == "directory":
        configured = str(tmp_path)
    elif unsafe == "workspace":
        target = e.workspace / "cli"
        target.write_bytes(executable.read_bytes())
        target.chmod(0o755)
        configured = str(target)
    else:
        if unsafe == "writable":
            executable.chmod(0o777)
        else:
            with executable.open("r+b") as stream:
                stream.truncate(512 * 1024 * 1024 + 1)
        configured = str(executable)
    e.memory.configuration.update("codex", ProviderConfiguration(values={"executable": configured}))
    with pytest.raises(ValueError):
        await e.service.candidate(e.memory.id.hex, "codex", adopt_current_executable=True)


async def test_same_failed_run_gets_one_distinct_reserved_dispatch_and_exact_readback(recovery):
    e = recovery
    before = copy.deepcopy([r.data for r in e.memory.rows])
    candidate = await e.service.candidate(e.memory.id.hex, "codex")
    assert candidate["dispatches_used"] == 2 and candidate["next_attempt"] == 3
    assert candidate["process_evidence"] == "legacy_operator_attestation_required"
    request = body(candidate)
    receipt = await e.service.admit(e.memory.id.hex, request)
    assert e.memory.run.data["status"] == "queued"
    assert e.memory.run.data["implementation_rounds"] == 1 and e.memory.run.data["repair_budget"] == 0
    assert e.memory.task.attempt == 3 and e.memory.task.data["idempotency_key"] != "b" * 32
    assert [r.data for r in e.memory.rows][:2] == before
    assert receipt["process_evidence"]["provenance"] == "legacy_operator_attestation"
    assert receipt == await e.service.get(e.memory.id.hex, request.request_id)
    assert receipt == await e.service.admit(e.memory.id.hex, request)
    assert (e.workspace / "partial.py").read_text() == "VALUE = 1\n"
    with pytest.raises(ValueError):
        await e.service.admit(e.memory.id.hex, body(candidate, request_id="d" * 32))
    with pytest.raises(ValueError):
        await e.service.admit(e.memory.id.hex, body(candidate, candidate_digest="0" * 64))
    dispatch = await e.service.begin(e.memory.id.hex)
    assert dispatch["disposition"] == "execute"
    await e.service.dispatch(e.memory.id.hex, dispatch["idempotency_key"])
    with pytest.raises(ValueError, match="consum|dispatch"):
        await e.service.dispatch(e.memory.id.hex, dispatch["idempotency_key"])
    with pytest.raises(KeyError):
        await e.journal.receipt(e.memory.id.hex, "b" * 32, {"source_tree": "forged-late-result"})


@pytest.mark.parametrize("drift", ["source", "head", "policy", "provider", "failure", "actor", "active", "sandbox"])
async def test_changed_candidate_or_current_authority_refuses_before_queue(recovery, monkeypatch, drift):
    e = recovery
    candidate = await e.service.candidate(e.memory.id.hex, "codex")
    if drift == "source":
        (e.workspace / "partial.py").write_text("VALUE = 2\n")
    if drift == "head":
        (e.workspace / ".git" / "HEAD").write_text("f" * 40 + "\n")
    if drift == "policy":
        monkeypatch.setattr(e.service, "_policy_digest", lambda: "9" * 64)
    if drift == "provider":
        monkeypatch.setattr(e.service, "_provider_digest", lambda run: "8" * 64)
    if drift == "failure":
        e.memory.run.data["error"] = "different failure"
    if drift == "actor":

        async def denied(*args):
            raise AccessDenied("revoked")

        monkeypatch.setattr(ProjectAccess, "current_administrator", denied)
    if drift == "active":
        e.engine.active[e.memory.id.hex] = SimpleNamespace(done=lambda: False)
    if drift == "sandbox":

        async def unknown(*args):
            return [{"effects": {"start": {"status": "intent"}}}]

        e.journal.sandbox_executions = unknown
    with pytest.raises((ValueError, AccessDenied)):
        await e.service.admit(e.memory.id.hex, body(candidate))
    assert e.memory.run.status == "failed" and e.memory.task.attempt == 2


async def test_changed_source_after_admission_refuses_reserved_dispatch(recovery):
    e = recovery
    candidate = await e.service.candidate(e.memory.id.hex, "codex")
    await e.service.admit(e.memory.id.hex, body(candidate))
    dispatch = await e.service.begin(e.memory.id.hex)
    (e.workspace / "partial.py").write_text("CHANGED = True\n")
    with pytest.raises(ValueError, match="cut|source"):
        await e.service.dispatch(e.memory.id.hex, dispatch["idempotency_key"])
    assert not any(r.kind == "provider_dispatch_intent" for r in e.memory.rows)


async def test_unknown_native_group_cannot_be_overridden_by_legacy_attestation(recovery):
    e = recovery
    outcome = ProviderProcessOutcome(exit_code=-25, leader_waited=True, streams_drained=True, group_absent=False)
    await e.service.record_process(e.memory.id.hex, "b" * 32, outcome)
    assert e.memory.run.status == "needs_reconciliation"
    e.memory.run.status = e.memory.run.data["status"] = "failed"
    with pytest.raises(ValueError, match="unknown|reconciliation"):
        await e.service.candidate(e.memory.id.hex, "codex")


async def test_native_receipt_and_four_intent_ceiling_are_enforced(recovery):
    e = recovery
    outcome = ProviderProcessOutcome(exit_code=-25, leader_waited=True, streams_drained=True, group_absent=True)
    await e.service.record_process(e.memory.id.hex, "b" * 32, outcome)
    candidate = await e.service.candidate(e.memory.id.hex, "codex")
    assert candidate["process_evidence"] == "native_cli_process_receipt"
    with pytest.raises(ValueError):
        await e.service.admit(e.memory.id.hex, body(candidate))
    for key in ["f" * 32, "0" * 32]:
        await e.journal._append(e.memory.id.hex, "implement", key, "intent", {})
    with pytest.raises(ValueError, match="budget|four|4"):
        await e.service.candidate(e.memory.id.hex, "codex")


@pytest.mark.parametrize(
    "outcome", [None, ProviderProcessOutcome(exit_code=0, leader_waited=True, streams_drained=True, group_absent=True)]
)
async def test_future_unknown_or_zero_exit_failure_is_never_eligible_for_legacy_attestation(recovery, outcome):
    e = recovery
    await e.service.record_process(e.memory.id.hex, "b" * 32, outcome)
    e.memory.run.status = e.memory.run.data["status"] = "failed"
    with pytest.raises(ValueError, match="unknown|reconciliation"):
        await e.service.candidate(e.memory.id.hex, "codex")


async def test_late_process_error_cannot_replace_completed_original_result(recovery):
    e = recovery
    await e.journal.receipt(e.memory.id.hex, "b" * 32, {"source_tree": "known-result"})
    with pytest.raises(ProviderAttemptClosed, match="completed|active"):
        await e.service.record_process(
            e.memory.id.hex,
            "b" * 32,
            ProviderProcessOutcome(exit_code=-25, leader_waited=True, streams_drained=True, group_absent=True),
        )
    assert e.memory.task.status == "done"


async def test_startup_never_relabels_legacy_failed_provider_before_explicit_admission(recovery):
    e = recovery

    async def pending():
        return [SimpleNamespace(run_id=e.memory.id, task_id="implement")]

    e.journal.tasks.find_pending_recovery = pending
    assert await e.journal.recover() == 0
    assert e.memory.run.status == "failed" and e.memory.task.status == "running"


async def test_operator_context_change_after_candidate_read_refuses_before_admission(recovery, monkeypatch):
    e = recovery
    candidate = await e.service.candidate(e.memory.id.hex, "codex")
    original = e.service._settled

    async def changed(*args, **kwargs):
        await original(*args, **kwargs)
        CURRENT_PRINCIPAL.set(None)

    monkeypatch.setattr(e.service, "_settled", changed)
    with pytest.raises(AccessDenied):
        await e.service.admit(e.memory.id.hex, body(candidate))
    assert e.memory.run.status == "failed"


async def test_normal_provider_dispatch_is_recorded_once_before_call_and_has_final_launch_cut(recovery):
    e = recovery
    e.memory.run.status = e.memory.run.data["status"] = "developing"
    await e.service.normal_dispatch(e.memory.id.hex, "b" * 32)
    assert len([r for r in e.memory.rows if r.kind == "provider_dispatch_intent"]) == 1
    with pytest.raises(ValueError, match="dispatch|consum"):
        await e.service.normal_dispatch(e.memory.id.hex, "b" * 32)
    await e.service.assert_launch(e.memory.id.hex, "b" * 32)
    (e.workspace / "partial.py").write_text("changed after emit await\n")
    with pytest.raises(ValueError, match="cut|source"):
        await e.service.assert_launch(e.memory.id.hex, "b" * 32)


async def test_new_dispatch_without_failure_observation_cannot_use_legacy_attestation(recovery):
    e = recovery
    await e.journal._append(
        e.memory.id.hex,
        "implement",
        "b" * 32 + ":provider",
        "provider_dispatch_intent",
        {"version": 1, "task_key": "b" * 32},
    )
    with pytest.raises(ValueError, match="unknown|reconciliation"):
        await e.service.candidate(e.memory.id.hex, "codex")


async def test_real_result_can_change_source_but_not_settle_an_old_reserved_attempt(recovery):
    e = recovery
    candidate = await e.service.candidate(e.memory.id.hex, "codex")
    await e.service.admit(e.memory.id.hex, body(candidate))
    state = await e.service.begin(e.memory.id.hex)
    key = state["idempotency_key"]
    e.memory.run.status = e.memory.run.data["status"] = "developing"
    await e.service.dispatch(e.memory.id.hex, key)
    (e.workspace / "partial.py").write_text("VALUE = 2\n")
    await e.service.assert_result(e.memory.id.hex, key)
    with pytest.raises(ValueError, match="receipt|completed"):
        await e.service.complete(e.memory.id.hex, key)
    await e.journal.receipt(e.memory.id.hex, key, {"source_tree": "new-source"})
    await e.service.complete(e.memory.id.hex, key)
    assert await e.service.begin(e.memory.id.hex) is None
    with pytest.raises(ValueError, match="active|current|closed"):
        await e.service.assert_result(e.memory.id.hex, "b" * 32)


@pytest.mark.parametrize("drift", ["index", "head", "source"])
async def test_only_native_preparation_index_staging_can_refresh_reserved_cut(recovery, drift):
    e = recovery
    candidate = await e.service.candidate(e.memory.id.hex, "codex")
    await e.service.admit(e.memory.id.hex, body(candidate))

    async def inspect():
        path = e.workspace / {"index": ".git/index", "head": ".git/HEAD", "source": "partial.py"}[drift]
        path.write_text("native-stage\n")
        return {"source_tree": "a" * 40}

    if drift == "index":
        assert await e.service.prepare_workspace(e.memory.id.hex, inspect) == {"source_tree": "a" * 40}
        assert (await e.service.get(e.memory.id.hex, "c" * 32))["candidate"] == candidate
        await e.service.dispatch(e.memory.id.hex, (await e.service.begin(e.memory.id.hex))["idempotency_key"])
    else:
        with pytest.raises(ValueError, match="cut|source|Git"):
            await e.service.prepare_workspace(e.memory.id.hex, inspect)
        assert not any(row.kind == "provider_workspace_prepared" for row in e.memory.rows)


async def test_candidate_is_bound_to_the_reviewing_native_operator(recovery, monkeypatch):
    e = recovery
    candidate = await e.service.candidate(e.memory.id.hex, "codex")
    actor = Principal(uuid4(), "local", "other-operator", authentication_method="local_bootstrap")

    async def other_identity(*args, **kwargs):
        return SimpleNamespace(enabled=True, issuer=actor.issuer, subject=actor.subject)

    monkeypatch.setattr(ForgeIdentityUserRepository, "find_by_id", other_identity)
    token = CURRENT_PRINCIPAL.set(actor)
    try:
        with pytest.raises(ValueError, match="candidate"):
            await e.service.admit(e.memory.id.hex, body(candidate))
    finally:
        CURRENT_PRINCIPAL.reset(token)
    assert e.memory.run.status == "failed"


@pytest.mark.parametrize("mode", ["normal", "recovery"])
async def test_final_launch_refuses_local_drift_during_native_commit_before_any_child(recovery, monkeypatch, mode):
    from contextlib import asynccontextmanager

    from firefly_forge import providers

    e = recovery
    key = "b" * 32
    if mode == "recovery":
        candidate = await e.service.candidate(e.memory.id.hex, "codex")
        await e.service.admit(e.memory.id.hex, body(candidate))
        key = (await e.service.begin(e.memory.id.hex))["idempotency_key"]
        await e.service.dispatch(e.memory.id.hex, key)
        e.memory.run.status = e.memory.run.data["status"] = "developing"
    else:
        e.memory.run.status = e.memory.run.data["status"] = "developing"
        await e.service.normal_dispatch(e.memory.id.hex, key)
    original = e.memory._unit

    @asynccontextmanager
    async def drift_after_commit():
        async with original() as value:
            yield value
        (e.workspace / "partial.py").write_text("changed during commit\n")

    created = []

    async def create(*args, **kwargs):
        created.append(True)
        raise AssertionError("No child is permitted")

    async def guard():
        await e.service.assert_launch(e.memory.id.hex, key)

    async def emit(*args):
        pass

    e.memory._unit = drift_after_commit
    monkeypatch.setattr(providers.asyncio, "create_subprocess_exec", create)
    with providers.cli_dispatch_scope(guard), pytest.raises(ValueError, match="cut|source"):
        await providers.run_process(["unused"], e.workspace, emit, timeout=1)
    assert not created


async def test_late_worker_feedback_cannot_pause_the_replacement_attempt(recovery):
    e = recovery
    candidate = await e.service.candidate(e.memory.id.hex, "codex")
    await e.service.admit(e.memory.id.hex, body(candidate))
    with pytest.raises(ValueError, match="Late worker feedback"):
        await e.journal.worker_feedback(
            e.memory.id.hex,
            {
                "id": "late-old",
                "role_id": "implementer",
                "task_id": "implement",
                "attempt": 2,
                "disposition": "blocked",
                "summary": "Old completion",
                "request": {"id": "old-question", "kind": "verification", "question": "Run checks?", "context": "Old"},
            },
        )
    assert e.memory.run.status == "queued" and e.memory.task.attempt == 3
    assert not any(row.kind == "worker_feedback" for row in e.memory.rows)


async def test_new_reserved_attempt_that_never_launched_cannot_be_attested_as_historical(recovery):
    e = recovery
    candidate = await e.service.candidate(e.memory.id.hex, "codex")
    await e.service.admit(e.memory.id.hex, body(candidate))
    e.memory.run.status = e.memory.run.data["status"] = "failed"
    e.memory.task.status = "failed"
    with pytest.raises(ValueError, match="unknown|reconciliation"):
        await e.service.candidate(e.memory.id.hex, "codex")
