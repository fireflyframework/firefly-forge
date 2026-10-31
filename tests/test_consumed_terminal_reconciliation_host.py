# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""An explicit same-action reconciliation accepts only the uncharged native cut."""

import asyncio
import copy
from types import MethodType
from uuid import uuid4

import pytest
from test_bridge_inflight_cancel import Rows
from test_consumed_terminal_completion_host import (  # noqa: F401
    completed_legacy_final,
    consent,
    consumed_terminal,
    fresh_checkpoint_consumer,
    frozen_original_write_grant,
)

from firefly_forge.known_worker_completions import KnownWorkerCompletions
from firefly_forge.project_access import CURRENT_PRINCIPAL
from firefly_forge.store import Store

pytest_plugins = ["test_invalid_read_retained_epoch_engine_host"]


@pytest.fixture(autouse=True)
def memory_owner_identity(monkeypatch):
    from test_bridge_inflight_cancel import MemoryStore

    monkeypatch.setattr(MemoryStore, "source_budget_admission_unit", MemoryStore._unit, raising=False)
    monkeypatch.setattr(
        MemoryStore,
        "execution_ownership_identity",
        property(
            lambda store: {
                "backend_pid": 271,
                "backend_start": "2026-10-31 00:00:00+00",
                "connection_instance": id(store.execution_ownership_token),
            }
        ),
        raising=False,
    )


async def stopped(env, *, held=True):
    from firefly_forge.consumed_terminal_completions import claimed_execution

    env.store.run.data.setdefault("id", env.run_id)
    env.store.event_records = Rows()
    env.store.get_run = MethodType(Store.get_run, env.store)
    env.store.event = MethodType(Store.event, env.store)
    await env.store.event(env.run_id, "retained_activity", "Retained native prefix")
    api = KnownWorkerCompletions(env.store, env.journal, env.engine)
    candidate = await api.candidate(env.run_id)
    request_id = uuid4().hex
    await api.request(env.run_id, request_id, consent(candidate))
    token = CURRENT_PRINCIPAL.set(None)
    try:
        assert await env.store.claim(env.run_id)
        execution = await claimed_execution(env.engine, env.run_id)
        await execution.attest_runner()
    finally:
        CURRENT_PRINCIPAL.reset(token)
    if held:
        handoff = {
            **env.store.run.data["verification_handoff"],
            "status": "held",
            "error": "Verification needs explicit reconciliation or renewed authority before continuation.",
        }
        await env.journal._append(
            env.run_id, "implement", handoff["id"] + ":held", "verification_handoff_outcome", handoff
        )
        task = await env.journal._task(env.run_id, "implement")
        task.status = "paused"
        task.data = {k: v for k, v in task.data.items() if k != "verification_handoff"}
        env.store.run.data.update(verification_handoff=handoff, display_status="paused_worker_request")
    env.store.run.status = "paused"
    env.store.run.data.update(status="paused", error="Consumed terminal original scope or invocation changed")
    return api, request_id


def reconcile_input(candidate):
    from firefly_forge.known_worker_completion_models import ConsumedTerminalReconciliationInputV1

    return ConsumedTerminalReconciliationInputV1(
        version=1,
        candidate_digest=candidate["digest"],
        acknowledge_same_claimed_action=True,
        acknowledge_no_execution_grant=True,
        acknowledge_local_postprocessing=True,
    )


@pytest.mark.parametrize("held", [False, True])
async def test_reconcile_same_claim_without_replay_or_second_charge(consumed_terminal, monkeypatch, held):  # noqa: F811
    env = consumed_terminal
    api, request_id = await stopped(env, held=held)
    before = copy.deepcopy(await env.store.get_run(env.run_id))
    prefix = await env.journal.entries(env.run_id)
    posts = len(env.posts)

    async def forbidden(*args, **kwargs):
        raise AssertionError("Reconciliation cannot dispatch models, tools, QA or verification")

    monkeypatch.setattr(env.provider, "execute", forbidden)
    monkeypatch.setattr(env.engine, "_project_command", forbidden)
    monkeypatch.setattr(env.engine, "_independent_review", forbidden)
    candidate = await api.reconciliation_candidate(env.run_id, request_id)
    body = reconcile_input(candidate)
    receipt = await api.reconcile(env.run_id, request_id, body)
    assert receipt["settlement"]["state"] == "accepted", (await env.store.get_run(env.run_id)).get("error")
    assert await api.reconcile(env.run_id, request_id, body) == receipt
    after = await env.store.get_run(env.run_id)
    entries = await env.journal.entries(env.run_id)
    assert entries[: len(prefix)] == prefix
    assert after["attempt"] == before["attempt"]
    assert after["implementation_rounds"] == before["implementation_rounds"] + 1
    assert after["usage"]["requests"] == before["usage"]["requests"] + 2
    assert after["verification_handoff"] == before["verification_handoff"]
    assert after["events"][: len(before["events"])] == before["events"]
    assert sum(e["stage"] == "runner_attestation" for e in after["events"]) == 1
    assert [e["kind"] for e in entries].count("consumed_terminal_claim") == 1
    assert [e["kind"] for e in entries].count("consumed_terminal_reconciliation") == 1
    assert [e["kind"] for e in entries].count("consumed_terminal_result") == 1
    record = next(e for e in entries if e["kind"] == "consumed_terminal_reconciliation")
    terminal = next(e for e in reversed(entries) if e["kind"] == "terminal_completion_receipt")
    assert all(
        c["reconciliation_record_digest"] == record["digest"] for c in terminal["payload"]["proof"]["completions"]
    )
    assert_receipt_reconciliation_binding(entries)
    assert len(env.posts) == posts
    assert env.run_id not in env.engine.active


async def test_reconciliation_refuses_foreign_current_cuts(consumed_terminal, monkeypatch):  # noqa: F811
    from firefly_forge.retained_verified_source import prepare_retained_source

    env = consumed_terminal
    api, request_id = await stopped(env)
    run, rows = copy.deepcopy(env.store.run.data), copy.deepcopy(env.journal.records.rows)
    task = await env.journal._task(env.run_id, "implement")
    task_status, task_data = task.status, copy.deepcopy(task.data)
    event_rows = copy.deepcopy(env.store.event_records.rows)
    content = (env.workspace / "existing.py").read_bytes()
    with pytest.raises(ValueError, match="exact next attempt"):
        await prepare_retained_source(env.store, env.journal, await env.store.get_run(env.run_id), env.workspace)
    for change in (
        "usage",
        "attempt",
        "task",
        "held",
        "foreign",
        "original_handoff",
        "event",
        "source",
        "index",
        "active",
    ):
        try:
            if change == "usage":
                env.store.run.data["usage"]["requests"] += 1
            elif change == "attempt":
                env.store.run.data["attempt"] += 1
            elif change == "task":
                task.attempt += 1
            elif change == "held":
                env.store.run.data["verification_handoff"]["commands_count"] += 1
            elif change == "foreign":
                await env.journal._append(env.run_id, "implement", "foreign", "foreign", {})
            elif change == "original_handoff":
                original = next(
                    row
                    for row in env.journal.records.rows
                    if row.kind == "verification_handoff_outcome" and row.data["payload"].get("status") == "completed"
                )
                original.data["payload"]["failed_commands"] = 1
            elif change == "event":
                await env.store.event(env.run_id, "foreign", "Unrelated activity")
            elif change == "source":
                (env.workspace / "existing.py").write_text("VALUE = 999\n")
            elif change == "index":
                reviewed = await api.reconciliation_candidate(env.run_id, request_id)
                index = env.workspace / ".git" / "index"
                index.write_bytes(index.read_bytes() + b"changed")
            else:
                env.engine.active[env.run_id] = asyncio.create_task(asyncio.Event().wait())
            with pytest.raises((ValueError, RuntimeError)):
                if change == "index":
                    await api.reconcile(env.run_id, request_id, reconcile_input(reviewed))
                else:
                    await api.reconciliation_candidate(env.run_id, request_id)
        finally:
            env.store.run.data = copy.deepcopy(run)
            env.journal.records.rows = copy.deepcopy(rows)
            task.status, task.data, task.attempt = task_status, copy.deepcopy(task_data), run["attempt"]
            env.store.event_records.rows = copy.deepcopy(event_rows)
            (env.workspace / "existing.py").write_bytes(content)
            if change == "index":
                index.write_bytes(index.read_bytes()[:-7])
            active = env.engine.active.pop(env.run_id, None)
            if active:
                active.cancel()
                await asyncio.gather(active, return_exceptions=True)
    assert await env.journal.entries(env.run_id) == [row.data for row in rows]


async def test_reviewed_reconciliation_binds_execution_owner(consumed_terminal):  # noqa: F811
    env = consumed_terminal
    api, request_id = await stopped(env)
    candidate = await api.reconciliation_candidate(env.run_id, request_id)
    before = copy.deepcopy((env.store.run.data, [r.data for r in env.journal.records.rows]))
    env.store.execution_ownership_token = object()
    with pytest.raises(ValueError, match="candidate changed"):
        await api.reconcile(env.run_id, request_id, reconcile_input(candidate))
    assert (env.store.run.data, [r.data for r in env.journal.records.rows]) == before


async def test_reviewed_reconciliation_refuses_runtime_profile_policy_and_index_drift(consumed_terminal, monkeypatch):  # noqa: F811
    import firefly_forge.consumed_terminal_reconciliations as reconciliation

    env = consumed_terminal
    api, request_id = await stopped(env)
    candidate = await api.reconciliation_candidate(env.run_id, request_id)
    before = copy.deepcopy((env.store.run.data, [r.data for r in env.journal.records.rows]))
    for field in ("runtime_contract", "current_profile_seal", "policy_digest"):
        with monkeypatch.context() as drift:
            drift.setattr(
                reconciliation,
                field,
                lambda field=field: {"changed": True} if field == "current_profile_seal" else "f" * 64,
            )
            with pytest.raises(ValueError):
                await api.reconcile(env.run_id, request_id, reconcile_input(candidate))
        assert (env.store.run.data, [r.data for r in env.journal.records.rows]) == before
    token = CURRENT_PRINCIPAL.set(None)
    try:
        with pytest.raises(Exception, match="operator"):
            await api.reconcile(env.run_id, request_id, reconcile_input(candidate))
    finally:
        CURRENT_PRINCIPAL.reset(token)
    assert (env.store.run.data, [r.data for r in env.journal.records.rows]) == before


def assert_receipt_reconciliation_binding(entries):
    from firefly_forge.journal import ZERO_HASH, Journal, digest
    from firefly_forge.native_terminal_completion_models import validate_native_receipt

    terminal = next(e for e in reversed(entries) if e["kind"] == "terminal_completion_receipt")
    validate_native_receipt(terminal, entries)
    for field in ("missing", "foreign", "runtime"):
        forged = copy.deepcopy(terminal)
        proof = forged["payload"]["proof"]
        if field == "runtime":
            proof["runtime_contract"] = "f" * 64
        else:
            for closure in proof["completions"]:
                if field == "missing":
                    closure.pop("reconciliation_record_digest")
                else:
                    closure["reconciliation_record_digest"] = "f" * 64
        proof["digest"] = digest({k: v for k, v in proof.items() if k != "digest"})
        with pytest.raises(ValueError):
            validate_native_receipt(forged, entries)

    # Rebuild a fixture history with the exact retained Q4 runtime/profile hashes.
    # No stored native row is changed: this tests historical proof attribution only.
    replacements, historical = {}, []

    def replace(value):
        if isinstance(value, dict):
            return {key: replace(item) for key, item in value.items()}
        if isinstance(value, list):
            return [replace(item) for item in value]
        return replacements.get(value, value) if isinstance(value, str) else value

    for original in entries:
        entry = replace(copy.deepcopy(original))
        payload = entry["payload"]
        if entry["kind"] == "consumed_terminal_recovery":
            candidate = payload["candidate"]
            proof = candidate["proof"]
            old = proof["digest"]
            proof["current_runtime_contract"] = "7be930c77bd09f27a688965f9ca6682c07476219e2cc010ff352feef14b102d1"
            proof["current_profile_digest"] = "35c4d6f769ac4f8bb142c344c741b85a3bf2b886248e052e6e6278b71576815e"
            proof["digest"] = digest({k: v for k, v in proof.items() if k != "digest"})
            replacements[old] = proof["digest"]
            payload["request"]["proof_digest"] = proof["digest"]
            old = candidate["digest"]
            candidate["digest"] = digest({k: v for k, v in candidate.items() if k != "digest"})
            replacements[old] = candidate["digest"]
            payload["request"]["candidate_digest"] = candidate["digest"]
        elif entry["kind"] == "consumed_terminal_reconciliation":
            candidate = payload["candidate"]
            candidate["run_digest"] = digest(payload["run"])
            candidate["journal_digest"] = digest({"entries": historical})
            old = candidate["digest"]
            candidate["digest"] = digest({k: v for k, v in candidate.items() if k != "digest"})
            replacements[old] = candidate["digest"]
            payload["request"]["candidate_digest"] = candidate["digest"]
        elif entry["kind"] == "terminal_completion_receipt" and any(
            c.get("custody") == "consumed_terminal_v2" for c in payload["proof"]["completions"]
        ):
            proof = payload["proof"]
            proof["digest"] = digest({k: v for k, v in proof.items() if k != "digest"})
            entry["idempotency_key"] = "terminal-completion:" + proof["acceptance_record_digest"][:40]
        entry["previous_hash"] = historical[-1]["digest"] if historical else ZERO_HASH
        entry["digest"] = digest({k: v for k, v in entry.items() if k != "digest"})
        replacements[original["digest"]] = entry["digest"]
        historical.append(entry)
    assert Journal.verify_chain(historical)
    historical_terminal = next(e for e in reversed(historical) if e["kind"] == "terminal_completion_receipt")
    validated = validate_native_receipt(historical_terminal, historical)
    witness = next(e for e in historical if e["kind"] == "consumed_terminal_recovery")
    assert validated.proof.runtime_contract != witness["payload"]["candidate"]["proof"]["current_runtime_contract"]
