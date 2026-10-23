# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""A paused completed provider turn is not a completed implementation task."""

# ruff: noqa: F401, F811
import copy
import hashlib
import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from test_source_budget_service_host import allowance_request, answered_four
from test_source_edit_continuations_host import controlled_metadata
from test_workspace_dependency_reconciliation import legacy_workspace
from test_workspace_reconciliation_journal import environment_journal
from test_workspace_reconciliation_service import reconciliation_service

from firefly_forge import source_budget_allowances as module
from firefly_forge.activity_models import project_activity


async def paused_invocation(env, tmp_path, monkeypatch):
    env, parent, path, history = await answered_four(env, tmp_path, monkeypatch, consumed=True)
    run = env.store.run.data
    entries = await env.recoveries._entries(env.root.name)
    dispatch = [e for e in entries if e["kind"] == "provider_dispatch_intent"][-1]
    run["summary"] = json.dumps({"feedback": run["worker_feedback"]}, sort_keys=True)
    run["usage"] = {"input_tokens": 1, "output_tokens": 1}
    payloads = [
        {"type": "thread.started", "thread_id": "public-paused-invocation"},
        {"type": "turn.started"},
        {"type": "item.completed", "item": {"id": "final", "type": "agent_message", "text": run["summary"]}},
        {"type": "turn.completed", "usage": run["usage"]},
    ]
    prior = await env.store.event_records.provider_budget_events(UUID(env.root.name))
    payload = env.store.event_content_records.payload
    boundary = datetime.fromisoformat(dispatch["created_at"])
    events, contents = [], {}
    for offset, data in enumerate(payloads, 1):
        event_id = uuid4().hex
        projection, retained = project_activity(env.root.name, event_id, "stdout", json.dumps(data), retain=True)
        events.append(
            {
                "id": event_id,
                "stage": "stdout",
                "created_at": (boundary + timedelta(microseconds=offset)).isoformat(),
                **projection,
            }
        )
        contents[event_id] = retained

    async def records(run_id):
        return prior + [SimpleNamespace(id=UUID(e["id"]), data=copy.deepcopy(e)) for e in events]

    async def read(run_id, event_id, size, sha):
        if event_id.hex not in contents:
            return await payload(run_id, event_id, size, sha)
        raw = contents[event_id.hex]
        assert len(raw) == size and hashlib.sha256(raw).hexdigest() == sha
        return raw

    env.store.event_records.provider_budget_events = records
    env.store.event_content_records.payload = read
    return env, parent, path, history, events, contents


async def test_real_consumed_paused_turn_can_review_allowance_without_claiming_implementation_complete(
    reconciliation_service, tmp_path, monkeypatch
):
    env, parent, path, history, *_ = await paused_invocation(reconciliation_service, tmp_path, monkeypatch)
    service = module.SourceBudgetReadmissions(env.service)
    before = copy.deepcopy(env.store.run.data)
    review = await service.candidate(
        env.root.name,
        parent["request"]["request_id"],
        path,
        module.SourceBudgetProposal(
            version=1, read_file_lifetime_limit=20, activation_ceiling=5, additional_activations=1
        ),
    )
    observed = review["budget_review"]["paused_provider_observation"]
    assert observed["output"]["implementation_success"] is False
    assert observed["output"]["process_exit_attested"] is False
    assert observed["output"]["usage_digest"] == module.digest(before["usage"])
    assert observed["history_digest"] == review["budget_review"]["history"]["digest"]
    assert env.store.run.data == before and env.task.status == "paused"
    receipt = await service.admit(env.root.name, parent["request"]["request_id"], allowance_request(review, path))
    assert env.task.status == "queued" and env.task.attempt == 5
    assert receipt["candidate"]["source_edit"]["remaining_commands"] == 7


@pytest.mark.parametrize(
    "change",
    [
        "missing_turn",
        "summary",
        "usage",
        "feedback",
        "latest_dispatch",
        "unknown_process",
        "cancel",
        "unresolved",
        "known_response",
        "key",
        "actor",
        "active",
    ],
)
async def test_consumed_reservation_alone_never_authorizes_an_unproven_or_active_turn(
    reconciliation_service, tmp_path, monkeypatch, change
):
    import asyncio

    env, parent, path, _, events, _ = await paused_invocation(reconciliation_service, tmp_path, monkeypatch)
    run = env.store.run.data
    active = None
    if change == "missing_turn":
        events.pop()
    elif change == "summary":
        run["summary"] += " "
    elif change == "usage":
        run["usage"] = {"input_tokens": 2, "output_tokens": 1}
    elif change == "feedback":
        run["worker_feedback"] = {**run["worker_feedback"], "summary": "Changed accepted question"}
    elif change == "latest_dispatch":
        await env.journal._append(
            env.root.name, "implement", "f" * 32 + ":provider", "provider_dispatch_intent", {"task_key": "f" * 32}
        )
    elif change == "unknown_process":
        await env.journal._append(
            env.root.name,
            "implement",
            "unknown-process",
            "provider_failure_observation",
            {"task_key": env.task.data["idempotency_key"], "process": None},
        )
    elif change == "cancel":
        run["cancel_request"] = {"status": "requested"}
    elif change == "unresolved":

        async def unresolved(run_id):
            return True

        env.recoveries.engine.tool_broker.unresolved = unresolved
    elif change == "known_response":
        env.task.data["known_worker_response"] = False
    elif change == "key":
        run["provider_recovery"]["task_key"] = "f" * 32
    elif change == "actor":
        run["provider_recovery"]["actor"] = {**run["provider_recovery"]["actor"], "user_id": "f" * 32}
    else:
        active = asyncio.get_running_loop().create_future()
        env.recoveries.engine.active[env.root.name] = active
    before = len(env.journal.records.rows)
    try:
        with pytest.raises(ValueError):
            await module.SourceBudgetReadmissions(env.service).candidate(
                env.root.name,
                parent["request"]["request_id"],
                path,
                module.SourceBudgetProposal(
                    version=1, read_file_lifetime_limit=20, activation_ceiling=5, additional_activations=1
                ),
            )
    finally:
        if active is not None:
            active.cancel()
    assert len(env.journal.records.rows) == before and env.task.attempt == 4


@pytest.mark.parametrize("change", ["absent", "dispatch", "history", "success", "exit", "task", "extra"])
async def test_injected_or_modified_paused_proof_is_never_a_resume_flag(
    reconciliation_service, tmp_path, monkeypatch, change
):
    env, parent, path, *_ = await paused_invocation(reconciliation_service, tmp_path, monkeypatch)
    service = module.SourceBudgetReadmissions(env.service)
    review = await service.candidate(
        env.root.name,
        parent["request"]["request_id"],
        path,
        module.SourceBudgetProposal(
            version=1, read_file_lifetime_limit=20, activation_ceiling=5, additional_activations=1
        ),
    )
    budget = copy.deepcopy(review["budget_review"])
    proof = budget["paused_provider_observation"]
    if change == "absent":
        budget["paused_provider_observation"] = None
    elif change == "dispatch":
        proof["dispatch_digest"] = "f" * 64
    elif change == "history":
        proof["history_digest"] = "f" * 64
    elif change == "success":
        proof["output"]["implementation_success"] = True
    elif change == "exit":
        proof["output"]["process_exit_attested"] = True
    elif change == "task":
        proof["task_digest"] = "f" * 64
    else:
        proof["caller_approved"] = True
    if isinstance(budget["paused_provider_observation"], dict):
        proof["digest"] = module.digest({k: v for k, v in proof.items() if k != "digest"})
    entries = await env.recoveries._entries(env.root.name)
    actor = {k: review["actor"][k] for k in ("user_id", "authentication_method")}
    with pytest.raises(ValueError):
        module.budget_continuation_link(env.store.run.data, env.recoveries._task_view(env.task), entries, actor, budget)


def test_allowance_contract_binds_the_actual_completed_turn_observer(monkeypatch):
    from pathlib import Path

    before = module.admission_contract_digest()
    read = Path.read_bytes

    def changed(path):
        result = read(path)
        return result + b"\n# public changed observer\n" if path.name == "source_result_readmission.py" else result

    monkeypatch.setattr(Path, "read_bytes", changed)
    assert module.admission_contract_digest() != before
