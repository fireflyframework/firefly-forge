# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Consumed terminal recovery accepts an exact result without another provider call."""

import copy
import json
from types import MethodType
from uuid import uuid4

import httpx2
import pytest
import test_final_outcome_epoch_scope_host as completed
from pydantic_ai import models
from test_agentic_checkpoint_activation_host import result
from test_bridge_completion_callbacks import CAPABILITY
from test_bridge_completion_native import mock_model_requests
from test_final_outcome_epoch_scope_host import completed_legacy_final, frozen_original_write_grant  # noqa: F401
from test_queued_retained_mode_epoch_host import fresh_checkpoint_consumer  # noqa: F401

from firefly_forge.known_worker_completions import KnownWorkerCompletions
from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied
from firefly_forge.store import Store

pytest_plugins = ["test_invalid_read_retained_epoch_engine_host"]


@pytest.fixture
async def consumed_terminal(completed_legacy_final, monkeypatch, fresh_checkpoint_consumer, request):  # noqa: F811
    import firefly_forge.retained_verified_source as retained
    from firefly_forge.agent_checkpoints import AgentCheckpoints

    env = completed_legacy_final
    native_source = retained.prepare_retained_source
    env.original_consume = fresh_checkpoint_consumer
    await completed.test_completed_final_allows_explicit_current_policy_adoption_and_checks_without_replay(
        env, monkeypatch
    )
    native_consume = env.original_consume
    monkeypatch.setattr(AgentCheckpoints, "_consume", native_consume)
    before = copy.deepcopy(await env.store.get_run(env.run_id))
    expected_attempt = before["attempt"] + 1
    env.initial_posts = len(env.posts)
    report = {
        "summary": "The exact retained source already has passing native checks",
        "worker_feedback": {
            "id": "consumed-terminal-worker",
            "role_id": "implementer",
            "task_id": "implement",
            "attempt": expected_attempt,
            "disposition": "completed",
            "summary": "The retained checks are complete",
            "evidence_paths": [],
            "request": None,
        },
        "qa_report": copy.deepcopy(getattr(request, "param", None)),
    }

    def transport(request):
        if request.method == "GET":
            return httpx2.Response(200, json={"completionRecovery": CAPABILITY})
        env.posts.append(request.content)
        count = len(env.posts) - env.initial_posts
        assert count <= 2, "The consumed result cannot be requested again"
        response = result(tool=True) if count == 1 else result(json.dumps(report))
        response["model"] = env.provider.settings["FORGE_BRIDGE_MODEL"]
        if count == 1:
            function = response["choices"][0]["message"]["tool_calls"][0]["function"]
            function.update(name="read_file", arguments='{"path":"existing.py"}')
        return httpx2.Response(200, json=response)

    async def execute(*args, **kwargs):
        return await type(env.provider).execute(env.provider, *args, **kwargs)

    async def fail_before_result(*args, **kwargs):
        assert CURRENT_PRINCIPAL.get() is None
        raise RuntimeError("Controlled interruption before AgentResult acceptance")

    env.provider.model().client._client._transport = httpx2.MockTransport(transport)
    monkeypatch.setattr(env.provider, "execute", execute)
    monkeypatch.setattr(retained, "prepare_retained_source", fail_before_result)
    token = CURRENT_PRINCIPAL.set(None)
    try:
        with models.override_allow_model_requests(False), mock_model_requests(env.provider.model()):
            await env.engine.execute_run(env.run_id)
    finally:
        CURRENT_PRINCIPAL.reset(token)
        monkeypatch.setattr(retained, "prepare_retained_source", native_source)
    env.failed = copy.deepcopy(await env.store.get_run(env.run_id))
    env.entries = await env.journal.entries(env.run_id)
    assert env.failed["status"] == "failed", env.failed.get("error")
    assert env.failed["implementation_pending"] is True
    assert env.entries[-1]["kind"] == "agent_consume"
    assert env.failed["usage"] == before["usage"]
    assert len(env.posts) == env.initial_posts + 2
    env.report = report
    env.store.reject_credentials = MethodType(Store.reject_credentials, env.store)
    env.store.claim = MethodType(Store.claim, env.store)
    env.store.assert_campaign_current = MethodType(Store.assert_campaign_current, env.store)

    async def admit(value):
        env.store.reject_credentials(value)

    env.store.admit_credentials = admit
    yield env


async def test_exact_final_consumed_before_agent_result_has_a_versioned_candidate(consumed_terminal):
    env = consumed_terminal
    before = copy.deepcopy((env.store.run.data, [row.data for row in env.journal.records.rows]))
    candidate = await KnownWorkerCompletions(env.store, env.journal).candidate(env.run_id)
    assert candidate["version"] == 2
    assert candidate["proof"]["verification"] == "consumed_terminal_output_known_only"
    assert candidate["proof"]["task_attempt"] == env.failed["attempt"]
    assert (env.store.run.data, [row.data for row in env.journal.records.rows]) == before
    assert len(env.posts) == env.initial_posts + 2


def consent(candidate):
    from firefly_forge.known_worker_completion_models import ConsumedTerminalInputV2

    return ConsumedTerminalInputV2(
        version=2,
        candidate_digest=candidate["digest"],
        policy_digest=candidate["policy_digest"],
        expected_run_digest=candidate["run_digest"],
        journal_digest=candidate["journal_digest"],
        proof_digest=candidate["proof"]["digest"],
        acknowledge_known_worker_output=True,
        acknowledge_no_execution_grant=True,
        acknowledge_local_postprocessing=True,
    )


async def test_local_terminal_acceptance_charges_once_and_pauses_without_provider_or_verification(
    consumed_terminal, monkeypatch
):
    env = consumed_terminal
    api = KnownWorkerCompletions(env.store, env.journal, env.engine)
    candidate = await api.candidate(env.run_id)
    request, request_id = consent(candidate), uuid4().hex
    before = copy.deepcopy(await env.store.get_run(env.run_id))
    reserved = await api.request(env.run_id, request_id, request)
    assert reserved["state"] == "queued" and reserved["same_task_attempt"] == before["attempt"]
    assert (await env.store.get_run(env.run_id))["usage"] == before["usage"]
    assert await api.request(env.run_id, request_id, request) == reserved

    async def forbidden(*args, **kwargs):
        raise AssertionError("Terminal acceptance cannot replay coding, checks or independent QA")

    monkeypatch.setattr(env.provider, "execute", forbidden)
    monkeypatch.setattr(env.engine, "_project_command", forbidden)
    monkeypatch.setattr(env.engine, "_independent_review", forbidden)
    token = CURRENT_PRINCIPAL.set(None)
    try:
        await env.engine.execute_run(env.run_id)
    finally:
        CURRENT_PRINCIPAL.reset(token)
    current = await env.store.get_run(env.run_id)
    assert current["status"] == "paused", current.get("error")
    assert current["attempt"] == before["attempt"]
    assert current["implementation_rounds"] == before["implementation_rounds"] + 1
    assert current["implementation_pending"] is False
    assert current["usage"]["runtime_report"] == env.report
    assert current["usage"]["requests"] == before["usage"]["requests"] + 2
    assert current["usage"]["input_tokens"] == before["usage"]["input_tokens"] + 22
    assert current["usage"]["output_tokens"] == before["usage"]["output_tokens"] + 6
    assert current["verification_handoff"] == before["verification_handoff"]
    entries = await env.journal.entries(env.run_id)
    assert entries[: len(env.entries)] == env.entries
    assert len([e for e in entries if e["kind"] == "consumed_terminal_result"]) == 1
    assert len([e for e in entries if e["kind"] == "consumed_terminal_claim"]) == 1
    assert {e["idempotency_key"] for e in entries if e["kind"] == "agent_consume"} == {
        e["idempotency_key"] for e in env.entries if e["kind"] == "agent_consume"
    }
    assert len(env.posts) == env.initial_posts + 2
    accepted = await api.get(env.run_id, request_id)
    assert accepted["state"] == "accepted"
    assert await api.request(env.run_id, request_id, request) == accepted
    assert (await env.store.get_run(env.run_id))["usage"] == current["usage"]


async def test_witness_rejects_rehashed_usage_and_report_substitution(consumed_terminal):
    from firefly_forge.consumed_terminal_completions import validate_witness
    from firefly_forge.journal import digest

    env = consumed_terminal
    api = KnownWorkerCompletions(env.store, env.journal)
    candidate = await api.candidate(env.run_id)
    await api.request(env.run_id, uuid4().hex, consent(candidate))
    entries = await env.journal.entries(env.run_id)
    witness = entries[-1]
    assert validate_witness(witness, entries) == witness["payload"]
    for part in ("before", "report"):
        changed = copy.deepcopy(witness)
        payload = changed["payload"]
        if part == "before":
            payload["run"]["usage"]["requests"] += 1
            payload["candidate"]["run_digest"] = digest(payload["run"])
            payload["request"]["expected_run_digest"] = payload["candidate"]["run_digest"]
        else:
            payload["usage"]["runtime_report"]["summary"] = "Another report"
            payload["candidate"]["proof"]["usage_after_digest"] = digest(payload["usage"])
        proof = payload["candidate"]["proof"]
        proof["digest"] = digest({k: v for k, v in proof.items() if k != "digest"})
        payload["request"]["proof_digest"] = proof["digest"]
        payload["candidate"]["digest"] = digest({k: v for k, v in payload["candidate"].items() if k != "digest"})
        payload["request"]["candidate_digest"] = payload["candidate"]["digest"]
        changed["digest"] = digest({k: v for k, v in changed.items() if k != "digest"})
        with pytest.raises(ValueError, match="witness"):
            validate_witness(changed, [*entries[:-1], changed])


async def test_request_refuses_replacement_owner_during_writer(consumed_terminal, monkeypatch):
    env = consumed_terminal
    api = KnownWorkerCompletions(env.store, env.journal)
    candidate = await api.candidate(env.run_id)
    original = api.completions._writer

    async def replace_owner(*args, **kwargs):
        await original(*args, **kwargs)
        env.store.execution_ownership_token = object()

    monkeypatch.setattr(api.completions, "_writer", replace_owner)
    with pytest.raises((ValueError, PermissionError), match="owner"):
        await api.request(env.run_id, uuid4().hex, consent(candidate))
    assert (await env.store.get_run(env.run_id))["status"] == "failed"
    assert await env.journal.entries(env.run_id) == env.entries


async def test_candidate_refuses_current_scope_counters_columns_and_source_changes(consumed_terminal):
    env = consumed_terminal
    api = KnownWorkerCompletions(env.store, env.journal)
    baseline = copy.deepcopy(env.store.run.data)
    posts = len(env.posts)
    for changes in (
        {"active_task": "verify"},
        {"status": "cancelled"},
        {"implementation_pending": False},
        {"attempt": False},
        {"implementation_rounds": 0},
        {"usage": {}},
        {"usage": {**baseline["usage"], "requests": False}},
        {"usage": {**baseline["usage"], "input_tokens": baseline["usage"]["input_tokens"] + 1}},
        {"execution_actor": {**baseline["execution_actor"], "user_id": uuid4().hex}},
        {"config_revision": "f" * 64},
    ):
        env.store.run.data = {**copy.deepcopy(baseline), **changes}
        with pytest.raises((ValueError, PermissionError, AccessDenied, KeyError)):
            await api.candidate(env.run_id)
    env.store.run.data = copy.deepcopy(baseline)
    env.user.enabled = False
    try:
        with pytest.raises(AccessDenied):
            await api.candidate(env.run_id)
    finally:
        env.user.enabled = True
    row = env.journal.records.rows[-1]
    kind = row.kind
    row.kind = "foreign_column"
    try:
        with pytest.raises(ValueError, match="columns"):
            await api.candidate(env.run_id)
    finally:
        env.journal.records.rows[-1].kind = kind
    path = env.workspace / "existing.py"
    content = path.read_bytes()
    try:
        path.write_bytes(content + b"\nCHANGED = True\n")
        with pytest.raises(ValueError):
            await api.candidate(env.run_id)
    finally:
        path.write_bytes(content)
    token = CURRENT_PRINCIPAL.set(None)
    try:
        with pytest.raises(AccessDenied):
            await api.candidate(env.run_id)
    finally:
        CURRENT_PRINCIPAL.reset(token)
    assert len(env.posts) == posts
    assert env.store.run.data == baseline
    from firefly_forge.known_worker_completions import _entries

    assert _entries(env.journal.records.rows, env.run_id) == env.entries


async def test_reviewed_action_refuses_current_policy_drift_before_claim(consumed_terminal, monkeypatch):
    import firefly_forge.consumed_terminal_completions as recovery
    from firefly_forge.journal import digest
    from firefly_forge.known_worker_completion_models import ConsumedTerminalReceiptV2

    env = consumed_terminal
    api = KnownWorkerCompletions(env.store, env.journal, env.engine)
    candidate = await api.candidate(env.run_id)
    request_id = uuid4().hex
    reserved = await api.request(env.run_id, request_id, consent(candidate))
    assert ConsumedTerminalReceiptV2.model_validate(reserved).same_task_attempt == env.failed["attempt"]
    for value in (1, False, "true"):
        with pytest.raises(ValueError):
            ConsumedTerminalReceiptV2.model_validate({**reserved, "no_execution_grant": value})
    forged = copy.deepcopy(candidate)
    forged["run_id"] = uuid4().hex
    forged["digest"] = digest({k: v for k, v in forged.items() if k != "digest"})
    from firefly_forge.known_worker_completion_models import ConsumedTerminalCandidateV2

    with pytest.raises(ValueError):
        ConsumedTerminalCandidateV2.model_validate(forged)
    monkeypatch.setattr(recovery, "policy_digest", lambda: "0" * 64)
    token = CURRENT_PRINCIPAL.set(None)
    try:
        await env.engine.execute_run(env.run_id)
    finally:
        CURRENT_PRINCIPAL.reset(token)
    current = await env.store.get_run(env.run_id)
    assert current["status"] == "needs_reconciliation"
    assert current["usage"] == env.failed["usage"]
    assert current["implementation_rounds"] == env.failed["implementation_rounds"]
    assert not [e for e in await env.journal.entries(env.run_id) if e["kind"] == "consumed_terminal_claim"]
    assert (await api.request(env.run_id, request_id, consent(candidate)))["state"] == "queued"
    assert len(env.posts) == env.initial_posts + 2


async def test_interruption_after_charging_cannot_requeue_or_charge_again(consumed_terminal, monkeypatch):
    env = consumed_terminal
    api = KnownWorkerCompletions(env.store, env.journal, env.engine)
    candidate = await api.candidate(env.run_id)
    body, request_id = consent(candidate), uuid4().hex
    await api.request(env.run_id, request_id, body)

    async def interrupted(*args, **kwargs):
        raise RuntimeError("Controlled interruption after native usage acceptance")

    async def forbidden(*args, **kwargs):
        raise AssertionError("Recovery cannot dispatch another inference")

    monkeypatch.setattr(env.journal, "worker_feedback", interrupted)
    monkeypatch.setattr(env.provider, "execute", forbidden)
    token = CURRENT_PRINCIPAL.set(None)
    try:
        await env.engine.execute_run(env.run_id)
    finally:
        CURRENT_PRINCIPAL.reset(token)
    current = await env.store.get_run(env.run_id)
    assert current["status"] == "needs_reconciliation", current.get("error")
    assert current["usage"]["requests"] == env.failed["usage"]["requests"] + 2
    assert current["implementation_pending"] is False
    assert current["implementation_rounds"] == env.failed["attempt"]
    assert (await api.request(env.run_id, request_id, body))["state"] == "claimed"
    entries = await env.journal.entries(env.run_id)
    assert len([e for e in entries if e["kind"] == "consumed_terminal_result"]) == 1
    assert len([e for e in entries if e["kind"] == "consumed_terminal_claim"]) == 1
    await env.engine.execute_run(env.run_id)
    assert await env.store.get_run(env.run_id) == current
    assert await env.journal.entries(env.run_id) == entries
    assert len(env.posts) == env.initial_posts + 2


async def test_actual_v3_consumed_frontier_preserves_duration_and_rejects_rehashed_cross_duration(
    tmp_path, monkeypatch
):
    from test_agentic_checkpoint_activation_host import activated
    from test_bridge_inflight_cancel import RUN
    from test_known_worker_completion_proof_host import rechain

    from firefly_forge.agentic_checkpoint_invocation import CheckpointInvocation, checkpoint_invocation
    from firefly_forge.agentic_runtime import execute_agentic
    from firefly_forge.agentic_tool_custody import AgenticToolCustody, agentic_tool_custody_scope
    from firefly_forge.known_final_outcome_proof import prepare_final_outcome
    from firefly_forge.work_duration_models import WorkDurationPolicyV2
    from firefly_forge.workspace_tools import ToolRuntimeContext

    env = await activated(tmp_path, monkeypatch)
    policy = WorkDurationPolicyV2()
    duration = policy.model_dump(mode="json")
    env.store.run.data.update(work_duration=duration, status="developing", active_task="implement", attempt=1)
    env.store.run.status = "developing"
    env.store.run.data["sandbox_snapshot"] = {"configuration": {"work_duration": duration}}
    run = await env.store.get_run(RUN)
    task = await env.journal.intent(RUN, "implement", payload={"config_revision": run["config_revision"]})
    posts = []
    report = dict(summary="Unchanged owned source", worker_feedback=None, qa_report=None)

    def transport(request):
        if request.method == "GET":
            return httpx2.Response(200, json={"completionRecovery": CAPABILITY})
        posts.append(request.content)
        assert len(posts) <= 2
        body = result(tool=True) if len(posts) == 1 else result(json.dumps(report))
        return httpx2.Response(200, json=body)

    async def guard():
        await env.store.assert_ownership()

    async def emit(*args):
        pass

    env.model.client._client._transport = httpx2.MockTransport(transport)
    env.model.client._client._mounts = {}
    invocation = CheckpointInvocation(env.store, env.journal, run, task, env.workspace, guard)
    context = ToolRuntimeContext(
        env.workspace, env.workspace, allowed_tools=frozenset({"write_file"}), work_duration=policy
    )
    token = CURRENT_PRINCIPAL.set(env.principal)
    try:
        with (
            checkpoint_invocation(invocation),
            agentic_tool_custody_scope(AgenticToolCustody(env.store, env.journal, run, task, env.workspace, guard)),
            models.override_allow_model_requests(False),
            mock_model_requests(env.model),
        ):
            await execute_agentic(
                env.workspace,
                "Owned current work",
                "pyfly",
                "none",
                emit,
                model=env.model,
                runtime_context=context,
                allow_output_token_cap=False,
                settings={"FORGE_AGENTIC_CHECKPOINTS": "required", "FORGE_BRIDGE_COMPLETION_RECOVERY": "required"},
            )
        run = await env.store.get_run(RUN)
        entries = await env.journal.entries(RUN)
        assert all(e["payload"]["version"] == 3 for e in entries if e["kind"] == "agent_checkpoint")
        from firefly_forge.known_worker_completions import _task

        task = _task(await env.journal._task(RUN, "implement"))
        proven = (await prepare_final_outcome(run, task, entries)).data
        assert proven["report"] == report and proven["usage"]["requests"] == 2
        assert proven["state"]["files_written"] == ["service.py"]
        changed_run = copy.deepcopy(run)
        changed_run["work_duration"] = {**duration, "timeout_seconds": 120}
        changed_run["sandbox_snapshot"]["configuration"]["work_duration"] = changed_run["work_duration"]
        with pytest.raises(ValueError, match="duration"):
            await prepare_final_outcome(changed_run, task, entries)
        changed = copy.deepcopy(entries)
        checkpoints = [e for e in changed if e["kind"] == "agent_checkpoint"]
        checkpoints[-1]["payload"]["invocation"]["work_started_at_ms"] += 1
        rechain(changed)
        with pytest.raises(ValueError, match="invocation"):
            await prepare_final_outcome(run, task, changed)
        assert len(posts) == 2 and await env.journal.entries(RUN) == entries
    finally:
        CURRENT_PRINCIPAL.reset(token)
        await env.model.client.close()


def test_named_historical_runtime_requires_the_exact_measured_profile_and_dependencies(monkeypatch):
    from dataclasses import replace
    from types import SimpleNamespace

    import firefly_forge.source_edit_qualification as qualification
    from firefly_forge.consumed_terminal_completions import RECORDED_V30_RUNTIME, _runtime

    compatibility = {
        "pyfly": "26.10.7",
        "fireflyframework-agentic": "26.10.1",
        "pydantic-ai-slim": "2.55.0",
        "openai": "3.29.0",
    }
    checkpoint = SimpleNamespace(canonical_payload=json.dumps({"compatibility": compatibility}).encode())
    _runtime(RECORDED_V30_RUNTIME, checkpoint, "f" * 64)
    with pytest.raises(ValueError, match="compatibility"):
        _runtime("a" * 64, checkpoint, "f" * 64)
    checkpoint.canonical_payload = json.dumps({"compatibility": {**compatibility, "pyfly": "26.10.16"}}).encode()
    with pytest.raises(ValueError, match="compatibility"):
        _runtime(RECORDED_V30_RUNTIME, checkpoint, "f" * 64)
    checkpoint.canonical_payload = json.dumps({"compatibility": compatibility}).encode()
    original = [p for p in qualification._profiles() if p.version == 29][0]
    forged = replace(original, policy_digest="1" * 64)
    forged = replace(forged, proof=replace(forged.proof, contract_digest=qualification.profile_contract_digest(forged)))
    monkeypatch.setattr(qualification, "_profiles", lambda: (forged,))
    with pytest.raises(ValueError, match="compatibility"):
        _runtime(RECORDED_V30_RUNTIME, checkpoint, "f" * 64)


async def test_request_projection_failure_rolls_back_without_a_second_consume(consumed_terminal, monkeypatch):
    from firefly_forge.known_worker_completions import _entries

    env = consumed_terminal
    api = KnownWorkerCompletions(env.store, env.journal)
    candidate = await api.candidate(env.run_id)
    before = copy.deepcopy(env.store.run.data)
    save = env.store.run_records.save

    async def interrupted(row):
        await save(row)
        raise RuntimeError("Controlled native projection failure")

    monkeypatch.setattr(env.store.run_records, "save", interrupted)
    with pytest.raises(RuntimeError, match="projection failure"):
        await api.request(env.run_id, uuid4().hex, consent(candidate))
    assert env.store.run.data == before
    assert _entries(env.journal.records.rows, env.run_id) == env.entries
    assert len(env.posts) == env.initial_posts + 2


async def test_consumed_result_refuses_rehashed_missing_tool_effects(consumed_terminal):
    from test_known_worker_completion_proof_host import rechain

    from firefly_forge.checkpoint_mode_runtime import mode_execution_scope
    from firefly_forge.known_final_outcome_proof import prepare_final_outcome
    from firefly_forge.known_worker_completions import _task

    env = consumed_terminal
    entries = copy.deepcopy(env.entries)
    position = max(i for i, entry in enumerate(entries) if entry["kind"] == "agentic_tool_result")
    del entries[position]
    rechain(entries)
    task = _task(await env.journal._task(env.run_id, "implement"))
    async with mode_execution_scope(env.journal, env.run_id):
        with pytest.raises(ValueError):
            await prepare_final_outcome(env.failed, task, entries)
    assert len(env.posts) == env.initial_posts + 2


async def test_readback_cannot_claim_acceptance_or_ownership_without_native_records(consumed_terminal):
    env = consumed_terminal
    api = KnownWorkerCompletions(env.store, env.journal)
    candidate = await api.candidate(env.run_id)
    request_id = uuid4().hex
    queued = await api.request(env.run_id, request_id, consent(candidate))
    marker = copy.deepcopy(env.store.run.data["consumed_terminal_recovery"])
    for state in ("accepted", "claimed"):
        env.store.run.data["consumed_terminal_recovery"] = {
            **marker,
            "state": state,
            "claim_digest": "f" * 64,
        }
        with pytest.raises(ValueError, match="native"):
            await api.get(env.run_id, request_id)
    env.store.run.data["consumed_terminal_recovery"] = marker
    assert await api.get(env.run_id, request_id) == queued
    assert env.store.run.data["usage"] == env.failed["usage"]
    assert len(env.posts) == env.initial_posts + 2


async def test_accepted_terminal_later_explicit_resume_reaches_native_verification_without_reclaim(
    consumed_terminal, monkeypatch
):
    env = consumed_terminal
    await test_local_terminal_acceptance_charges_once_and_pauses_without_provider_or_verification(env, monkeypatch)
    accepted = copy.deepcopy(await env.store.get_run(env.run_id))
    await env.journal.resume(env.run_id)
    checkpoints = []

    async def next_verification(*args, **kwargs):
        checkpoints.append(True)
        raise RuntimeError("Controlled ordinary verification frontier reached")

    monkeypatch.setattr(env.engine, "_project_command", next_verification)
    token = CURRENT_PRINCIPAL.set(None)
    try:
        await env.engine.execute_run(env.run_id)
    finally:
        CURRENT_PRINCIPAL.reset(token)
    current = await env.store.get_run(env.run_id)
    assert checkpoints == [True], current.get("error")
    assert "ordinary verification frontier" in current["error"]
    assert current["usage"] == accepted["usage"]
    assert current["attempt"] == accepted["attempt"]
    assert len(env.posts) == env.initial_posts + 2
    entries = await env.journal.entries(env.run_id)
    assert sum(e["kind"] == "consumed_terminal_claim" for e in entries) == 1
    assert sum(e["kind"] == "consumed_terminal_result" for e in entries) == 1


async def test_consumed_terminal_uses_real_native_snapshot_staging(consumed_terminal, monkeypatch):
    from firefly_forge.consumed_terminal_completions import ConsumedTerminalExecution

    closed = []
    native_close = ConsumedTerminalExecution.close_snapshot

    def close(execution):
        native_close(execution)
        closed.append(execution._snapshot)

    monkeypatch.setattr(ConsumedTerminalExecution, "close_snapshot", close)
    env = consumed_terminal
    api = KnownWorkerCompletions(env.store, env.journal, env.engine)
    candidate = await api.candidate(env.run_id)
    await api.request(env.run_id, uuid4().hex, consent(candidate))

    async def forbidden(*args, **kwargs):
        raise AssertionError("Consumed acceptance cannot replay a provider or verification")

    monkeypatch.setattr(env.provider, "execute", forbidden)
    monkeypatch.setattr(env.engine, "_project_command", forbidden)
    monkeypatch.setattr(env.engine, "_independent_review", forbidden)
    token = CURRENT_PRINCIPAL.set(None)
    try:
        await env.engine.execute_run(env.run_id)
    finally:
        CURRENT_PRINCIPAL.reset(token)
    current = await env.store.get_run(env.run_id)
    assert current["status"] == "paused", current.get("error")
    assert current["usage"]["requests"] == env.failed["usage"]["requests"] + 2
    assert len(env.posts) == env.initial_posts + 2
    assert len(closed) == 1 and closed[0] is not None
    assert all(stream.closed for stream in closed[0].source_fence.streams)


@pytest.mark.parametrize("provider", ["bridge", "configured-worker-provider"])
async def test_startup_recovery_preserves_failed_consumed_implementation_for_any_provider(
    consumed_terminal, monkeypatch, provider
):
    env = consumed_terminal
    env.store.run.data["agent"] = provider

    async def pending():
        return [row for row in env.journal.tasks.rows if row.status in {"running", "needs_reconciliation"}]

    monkeypatch.setattr(env.journal.tasks, "find_pending_recovery", pending, raising=False)

    def state():
        return copy.deepcopy(
            (
                env.store.run.data,
                [(row.task_id, row.status, row.attempt, row.data) for row in env.journal.tasks.rows],
                [row.data for row in env.journal.records.rows],
                env.posts,
            )
        )

    before = state()
    assert await env.journal.recover() == 0
    assert await env.journal.recover() == 0
    assert state() == before
    if provider == "bridge":
        candidate = await KnownWorkerCompletions(env.store, env.journal).candidate(env.run_id)
        assert candidate["version"] == 2 and candidate["run_id"] == env.run_id
        assert state() == before


UNREQUESTED_QA = {
    "mode": "not_requested",
    "reviewed_states": [],
    "reference_sha256": [],
    "findings": [],
    "not_reviewed": [],
    "summary": "Browser review was not requested for this API change.",
}


@pytest.mark.parametrize("consumed_terminal", [UNREQUESTED_QA], indirect=True)
async def test_explicit_unrequested_qa_is_retained_without_granting_qa_authority(consumed_terminal, monkeypatch):
    await test_local_terminal_acceptance_charges_once_and_pauses_without_provider_or_verification(
        consumed_terminal, monkeypatch
    )
    current = await consumed_terminal.store.get_run(consumed_terminal.run_id)
    assert current["usage"]["runtime_report"]["qa_report"] == UNREQUESTED_QA
    assert current["usage"]["qa_report"] is None
    assert current["usage"]["browser_evidence"] == []


@pytest.mark.parametrize(
    "consumed_terminal",
    [
        {**UNREQUESTED_QA, "mode": "vision"},
        {**UNREQUESTED_QA, "mode": "dom_only"},
        {**UNREQUESTED_QA, "reviewed_states": ["home"]},
        {**UNREQUESTED_QA, "reference_sha256": ["a" * 64]},
        {**UNREQUESTED_QA, "not_reviewed": ["home"]},
        {
            **UNREQUESTED_QA,
            "findings": [
                {
                    "severity": "high",
                    "journey_id": "customer",
                    "state_id": "home",
                    "path": "page",
                    "expected": "Readable",
                    "observed": "Clipped",
                    "evidence_ids": ["unknown"],
                }
            ],
        },
    ],
    indirect=True,
    ids=["vision", "dom", "reviewed", "reference", "missing_state", "finding"],
)
async def test_terminal_recovery_refuses_unverified_browser_qa_claims(consumed_terminal):
    env = consumed_terminal
    before = copy.deepcopy((await env.store.get_run(env.run_id), await env.journal.entries(env.run_id)))
    with pytest.raises(ValueError, match="unchanged-source report"):
        await KnownWorkerCompletions(env.store, env.journal).candidate(env.run_id)
    assert (await env.store.get_run(env.run_id), await env.journal.entries(env.run_id)) == before
    assert len(env.posts) == env.initial_posts + 2
