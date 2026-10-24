# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Explicit partial checkpoints preserve the failed implementation and frozen policy."""

import copy
from uuid import NAMESPACE_URL, uuid5

import pytest
from test_git_delivery_local import delivery_case as delivery_case
from test_git_delivery_operational import operational as operational

from firefly_forge.git_delivery_models import GitDeliveryInputV1
from firefly_forge.journal import digest


def admission():
    return {
        "version": 1,
        "candidate_digest": "a" * 64,
        "policy_digest": "b" * 64,
        "failed_feedback_digest": "c" * 64,
        "expected_run_digest": "d" * 64,
        "journal_digest": "e" * 64,
        "source_digest": "f" * 64,
        "valid_for_seconds": 600,
        "acknowledge_partial_unverified": True,
        "acknowledge_preserve_failed_run": True,
        "acknowledge_no_execution_grant": True,
    }


def test_legacy_request_serialization_is_byte_and_digest_compatible():
    old = {"version": 1, "request_id": "1" * 32, "expected_head": "2" * 40, "expected_source_tree": "3" * 40}
    assert GitDeliveryInputV1.model_validate(old).model_dump(mode="json") == old
    assert digest(GitDeliveryInputV1.model_validate(old).model_dump(mode="json")) == digest(old)


def test_explicit_adoption_is_typed_and_requires_source_cas_without_preparing_index():
    value = {"version": 1, "request_id": "1" * 32, "expected_head": "2" * 40, "explicit_adoption": admission()}
    model = GitDeliveryInputV1.model_validate(value)
    assert model.model_dump(mode="json") == value
    for field in [
        "acknowledge_partial_unverified",
        "acknowledge_preserve_failed_run",
        "acknowledge_no_execution_grant",
    ]:
        invalid = copy.deepcopy(value)
        invalid["explicit_adoption"][field] = False
        with pytest.raises(ValueError):
            GitDeliveryInputV1.model_validate(invalid)
    with pytest.raises(ValueError):
        GitDeliveryInputV1.model_validate({"version": 1, "request_id": "1" * 32, "expected_head": "2" * 40})


def failed_case():
    run_id = "1" * 32
    attempt = 6
    key = uuid5(NAMESPACE_URL, f"forge:{run_id}:implement:{attempt}").hex
    feedback = {
        "id": "partial-unit",
        "role_id": "implementer",
        "task_id": "implement",
        "attempt": attempt,
        "disposition": "failed",
        "summary": "Migration repaired; remaining implementation is incomplete.",
        "evidence_paths": [],
        "request": None,
    }
    feedback.update(protocol="source-budget-continuation-v1", allowance_id="4" * 32, allowance_digest="5" * 64)
    run = {
        "id": run_id,
        "status": "failed",
        "active_task": "implement",
        "implementation_rounds": attempt,
        "worker_feedback": feedback,
        "summary": "retained native final JSON",
        "usage": {"input_tokens": 2, "output_tokens": 1},
        "provider_dispatch": {"task_key": key},
        "execution_actor": {"version": 1, "user_id": "2" * 32},
    }
    task = {"id": "implement", "status": "running", "attempt": attempt, "idempotency_key": key}
    entries = []
    for kind, payload, identity in [
        ("intent", {"attempt": attempt}, key),
        ("provider_dispatch_intent", {"task_key": key, "attempt": attempt}, key + ":provider"),
        ("worker_feedback", feedback, "feedback-key"),
    ]:
        e = {
            "run_id": run_id,
            "sequence": len(entries) + 1,
            "task_id": "implement",
            "idempotency_key": identity,
            "kind": kind,
            "payload": copy.deepcopy(payload),
            "created_at": "2026-10-23T22:00:00+00:00",
            "previous_hash": entries[-1]["digest"] if entries else "0" * 64,
        }
        e["digest"] = digest(e)
        entries.append(e)
    import hashlib

    observation = {
        "version": 1,
        "protocol": "native_completed_turn_v1",
        "dispatch_digest": entries[1]["digest"],
        "summary_sha256": hashlib.sha256(run["summary"].encode()).hexdigest(),
        "usage_digest": digest(run["usage"]),
        "events": [{"event_id": "3" * 32}],
        "implementation_success": False,
        "process_exit_attested": False,
    }
    return run, task, entries, observation


def test_failed_unit_proof_binds_current_task_feedback_dispatch_and_completed_observation():
    from firefly_forge.git_failed_unit_adoption import failed_unit_proof

    run, task, entries, observation = failed_case()
    proof = failed_unit_proof(run, task, entries, observation)
    assert proof["failed_feedback_digest"] == entries[-1]["digest"]
    assert proof["task"] == task and proof["observation"] == observation
    assert proof["verification_status"] == "partial_unverified"


@pytest.mark.parametrize(
    "mutation",
    ["feedback", "attempt", "task_key", "status", "pending", "dispatch", "summary", "usage", "success", "exit"],
)
def test_failed_unit_proof_refuses_drift_and_false_success(mutation):
    from firefly_forge.git_failed_unit_adoption import failed_unit_proof

    run, task, entries, observation = failed_case()
    if mutation == "feedback":
        run["worker_feedback"]["summary"] = "different"
    elif mutation == "attempt":
        task["attempt"] = 5
    elif mutation == "task_key":
        task["idempotency_key"] = "9" * 32
    elif mutation == "status":
        run["status"] = "paused"
    elif mutation == "pending":
        run["worker_request"] = {"status": "pending"}
    elif mutation == "dispatch":
        observation["dispatch_digest"] = "9" * 64
    elif mutation == "summary":
        run["summary"] = "changed"
    elif mutation == "usage":
        run["usage"]["output_tokens"] = 99
    elif mutation == "success":
        observation["implementation_success"] = True
    elif mutation == "exit":
        observation["process_exit_attested"] = True
    with pytest.raises(ValueError):
        failed_unit_proof(run, task, entries, observation)


@pytest.fixture
def failed_delivery(operational, monkeypatch):
    import json
    from datetime import datetime, timedelta
    from types import SimpleNamespace
    from uuid import uuid4

    from test_git_delivery_local import git

    from firefly_forge.activity_models import project_activity
    from firefly_forge.git_delivery_policy import GitDeliveryPolicies
    from firefly_forge.project_access import AccessDenied, ProjectAccess

    c = operational
    run, task, entries, _ = failed_case()
    c.state["run"].update({k: v for k, v in run.items() if k not in {"execution_actor"}})
    c.state["run"]["git_delivery_policy"] = None
    c.state["entries"] = entries
    c.task = task

    async def plan(_):
        return {"tasks": [copy.deepcopy(c.task)]}

    async def executions(_):
        return []

    async def records(_):
        return [
            SimpleNamespace(data={k: v for k, v in e.items() if k != "digest"}, digest=e["digest"])
            for e in c.state["entries"]
        ]

    c.service.journal.plan = plan
    c.service.journal.sandbox_executions = executions
    c.service.journal.records = SimpleNamespace(find_for_run=records)

    async def snapshot(_self, _project):
        return copy.deepcopy(c.project_policy)

    async def authority(_self, _actor, _project, _permission):
        if not c.state["allowed"]:
            raise AccessDenied("Controlled current-user revocation")
        return True

    monkeypatch.setattr(GitDeliveryPolicies, "snapshot", snapshot)
    monkeypatch.setattr(ProjectAccess, "authorize_actor", authority)
    c.project_policy = {
        "policy": {
            "version": 1,
            "enabled": True,
            "every_units": 1,
            "on_pause": True,
            "remote": "local",
            "max_checkpoints": 50,
        },
        "digest": "b" * 64,
    }
    payloads = [
        {"type": "turn.started"},
        {"type": "item.completed", "item": {"id": "final", "type": "agent_message", "text": run["summary"]}},
        {"type": "turn.completed", "usage": run["usage"]},
    ]
    contents = {}
    events = []
    boundary = datetime.fromisoformat(entries[1]["created_at"])
    for n, payload in enumerate(payloads, 1):
        eid = uuid4().hex
        projection, raw = project_activity(run["id"], eid, "stdout", json.dumps(payload), retain=True)
        contents[eid] = raw
        events.append(
            {"id": eid, "stage": "stdout", "created_at": (boundary + timedelta(seconds=n)).isoformat(), **projection}
        )

    async def observed(_):
        return [SimpleNamespace(data=e) for e in events]

    async def content(_run, eid, size, fingerprint):
        raw = contents[eid.hex]
        assert len(raw) == size
        return raw

    c.service.store.event_records = SimpleNamespace(provider_budget_events=observed)
    c.service.store.event_content_records = SimpleNamespace(payload=content)
    (c.root / "product.py").write_text("value = 2\n")
    (c.root / "migration.py").write_text("def apply(): return 2\n")
    c.head_before = git(c.root, "rev-parse", "HEAD")
    c.index_before = (c.root / ".git/index").read_bytes()
    return c


def request_from(candidate):
    return GitDeliveryInputV1.model_validate(
        {
            "version": 1,
            "request_id": "a" * 32,
            "expected_head": candidate["expected_head"],
            "explicit_adoption": {
                "version": 1,
                "candidate_digest": candidate["digest"],
                "policy_digest": candidate["policy_digest"],
                "failed_feedback_digest": candidate["failed_feedback_digest"],
                "expected_run_digest": candidate["expected_run_digest"],
                "journal_digest": candidate["journal_digest"],
                "source_digest": candidate["source_digest"],
                "valid_for_seconds": 600,
                "acknowledge_partial_unverified": True,
                "acknowledge_preserve_failed_run": True,
                "acknowledge_no_execution_grant": True,
            },
        }
    )


async def test_owner_queue_adopts_and_commits_real_git_without_marking_failed_unit_done(failed_delivery):
    from test_git_delivery_local import git

    from firefly_forge.git_failed_unit_adoption import KIND

    c = failed_delivery
    candidate = await c.controller.adoption_candidate("1" * 32)
    assert (c.root / ".git/index").read_bytes() == c.index_before
    assert git(c.root, "rev-parse", "HEAD") == c.head_before
    body = request_from(candidate)
    admitted = await c.controller.request("1" * 32, body)
    assert admitted["status"] == "queued" and not c.state["run"].get("git_failed_unit_adoption")
    await c.controller.tickets.execute("a" * 32)
    result = await c.controller.ticket("1" * 32, "a" * 32)
    assert result["status"] == "completed", result
    assert c.state["run"]["status"] == "failed" and c.state["run"]["git_delivery_policy"] is None
    assert c.task["status"] == "running" and c.task["attempt"] == 6
    assert result["result"]["verification_status"] == "partial_unverified"
    assert git(c.root, "rev-parse", "HEAD") != c.head_before
    assert git(c.root, "log", "-1", "--pretty=%s").startswith("[UNVERIFIED] ")
    assert git(c.root, "show", "HEAD:migration.py") == "def apply(): return 2"
    assert [e["kind"] for e in c.state["entries"]][-3:] == [KIND, "git_delivery_intent", "git_delivery_receipt"]
    assert (await c.controller.request("1" * 32, body))["status"] == "completed"
    assert len([e for e in c.state["entries"] if e["kind"] == KIND]) == 1
    with pytest.raises(ValueError, match="unverified"):
        await c.service.final_start(c.state["run"], candidate["actor"], {})


@pytest.mark.parametrize("drift", ["source", "actor", "policy", "task", "unknown_tool"])
async def test_admitted_failed_checkpoint_refuses_drift_before_git_effect(failed_delivery, drift):
    from types import SimpleNamespace

    from test_git_delivery_local import git

    c = failed_delivery
    candidate = await c.controller.adoption_candidate("1" * 32)
    await c.controller.request("1" * 32, request_from(candidate))
    if drift == "source":
        (c.root / "product.py").write_text("value = 99\n")
    elif drift == "actor":
        c.state["allowed"] = False
    elif drift == "policy":
        c.project_policy["digest"] = "9" * 64
    elif drift == "task":
        c.task["attempt"] = 7
    elif drift == "unknown_tool":

        async def unresolved(_):
            return True

        c.service.engine.tool_broker = SimpleNamespace(unresolved=unresolved)
    await c.controller.tickets.execute("a" * 32)
    result = await c.controller.ticket("1" * 32, "a" * 32) if drift != "actor" else c.rows[next(iter(c.rows))].data
    assert result["status"] == "failed"
    assert git(c.root, "rev-parse", "HEAD") == c.head_before
    assert not any(e["kind"] == "git_delivery_intent" for e in c.state["entries"])


async def test_lost_response_after_real_commit_retains_unknown_and_never_replays(failed_delivery, monkeypatch):
    from test_git_delivery_local import git

    c = failed_delivery
    candidate = await c.controller.adoption_candidate("1" * 32)
    body = request_from(candidate)
    await c.controller.request("1" * 32, body)
    original = c.module.checked

    async def lost(argv, *args, **kwargs):
        result = await original(argv, *args, **kwargs)
        if "commit" in argv:
            raise OSError("Controlled response loss after the actual local commit")
        return result

    monkeypatch.setattr(c.module, "checked", lost)
    await c.controller.tickets.execute("a" * 32)
    assert (await c.controller.ticket("1" * 32, "a" * 32))["status"] == "needs_reconciliation"
    committed = git(c.root, "rev-parse", "HEAD")
    assert committed != c.head_before
    assert (await c.controller.request("1" * 32, body))["status"] == "needs_reconciliation"
    assert git(c.root, "rev-parse", "HEAD") == committed
    with pytest.raises(ValueError):
        await c.controller.adoption_candidate("1" * 32)
    assert len([e for e in c.state["entries"] if e["kind"] == "git_delivery_intent"]) == 1
    assert not any(e["kind"] == "git_delivery_receipt" for e in c.state["entries"])


async def test_expired_queue_consent_never_becomes_fresh_at_worker_dispatch(failed_delivery, monkeypatch):
    from test_git_delivery_local import git

    from firefly_forge import git_failed_unit_adoption as adoption

    c = failed_delivery
    candidate = await c.controller.adoption_candidate("1" * 32)
    await c.controller.request("1" * 32, request_from(candidate))
    monkeypatch.setattr(adoption, "clock_ms", lambda: 10**16)
    await c.controller.tickets.execute("a" * 32)
    assert (await c.controller.ticket("1" * 32, "a" * 32))["status"] == "failed"
    assert git(c.root, "rev-parse", "HEAD") == c.head_before
    assert not c.state["run"].get("git_failed_unit_adoption")


def test_failed_checkpoint_model_facts_do_not_claim_verification_or_success():
    from firefly_forge.communication import CommunicationGitDeliveryV1, communication_facts

    facts = communication_facts(
        {"status": "failed"},
        git_delivery=CommunicationGitDeliveryV1(
            checkpoint_revision="a" * 40,
            source_tree="b" * 40,
            sequence=1,
            changed_file_count=4,
            implementation_disposition="failed",
        ),
    )
    assert facts["git_delivery"]["verification"] == "not_run"
    assert facts["git_delivery"]["purpose"] == "unverified_failed_checkpoint"
    assert facts["git_delivery"]["implementation_disposition"] == "failed"
    assert "publication" not in facts


async def test_failed_checkpoint_cannot_enter_ready_phase_directly(failed_delivery):
    c = failed_delivery
    c.state["run"]["git_failed_unit_adoption"] = {"request_id": "a" * 32, "receipt_digest": "b" * 64}
    with pytest.raises(ValueError, match="unverified"):
        await c.service.final_ready({"run": c.state["run"], "actor": {}}, {})


def test_historical_normal_checkpoint_facts_keep_exact_bytes_and_digest():
    import json

    from firefly_forge.communication import CommunicationGitDeliveryV1, communication_facts

    context = CommunicationGitDeliveryV1(
        checkpoint_revision="a" * 40, source_tree="b" * 40, sequence=1, changed_file_count=4
    )
    expected = {
        "checkpoint_revision": "a" * 40,
        "source_tree": "b" * 40,
        "sequence": 1,
        "changed_file_count": 4,
        "verification": "pending",
        "purpose": "intermediate_draft_pull_request",
    }
    actual = communication_facts({"status": "paused"}, git_delivery=context)["git_delivery"]
    assert json.dumps(actual, sort_keys=True, separators=(",", ":")) == json.dumps(
        expected, sort_keys=True, separators=(",", ":")
    )
    assert digest(actual) == digest(expected)
