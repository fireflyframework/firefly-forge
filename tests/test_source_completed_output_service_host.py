# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Explicit finite recovery of retained output; no native process or provider is invoked."""

# ruff: noqa: F401, F811
import copy
from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import pytest
from test_source_completed_output_host import output_fixture
from test_source_edit_continuations_host import controlled_metadata
from test_source_human_readmission_host import answered_source, human_request
from test_workspace_dependency_reconciliation import legacy_workspace
from test_workspace_reconciliation_journal import environment_journal
from test_workspace_reconciliation_service import reconciliation_service

from firefly_forge import source_result_readmission as module
from firefly_forge.activity_models import project_activity
from firefly_forge.journal import digest
from firefly_forge.source_edit_continuations import source_edit_receipt
from firefly_forge.source_edit_model_catalog import model_catalog_path
from firefly_forge.source_human_readmission import SourceHumanReadmissions, _ancestral_unknown, validated_source_plan


async def completed_source(env, tmp_path, monkeypatch):
    env, parent = await answered_source(env, tmp_path, monkeypatch, observed=True, sanitized=True)
    path = str(model_catalog_path("fixture-model"))
    human = SourceHumanReadmissions(env.service)
    review = await human.candidate(env.root.name, parent["request"]["request_id"], path)
    parent = await human.admit(env.root.name, parent["request"]["request_id"], human_request(review, path))
    key = uuid5(NAMESPACE_URL, f"forge:{env.root.name}:implement:3").hex
    env.store.run.status = "developing"
    env.store.run.data.update(status="developing")
    env.task.status = "running"
    env.task.data["idempotency_key"] = key
    await env.journal._append(env.root.name, "implement", key, "intent", {"attempt": 3})
    dispatch = await env.journal._append(
        env.root.name,
        "implement",
        key + ":provider",
        "provider_dispatch_intent",
        {"version": 1, "task_key": key, "attempt": 3},
    )
    env.store.run.data["provider_dispatch"] = {
        "task_key": key,
        "cut": env.recoveries._cut(env.store.run.data),
        "actor": parent["actor"],
    }
    sample, _, contents = output_fixture()
    events, actual = [], {}
    boundary = datetime.fromisoformat(dispatch["created_at"])
    for offset, event in enumerate(sample["events"], 1):
        raw = contents[event["id"]]
        projection, retained = project_activity(env.root.name, event["id"], "stdout", raw.decode(), retain=True)
        events.append(
            {
                "id": event["id"],
                "stage": "stdout",
                "created_at": (boundary + timedelta(milliseconds=offset)).isoformat(),
                **projection,
            }
        )
        actual[event["id"]] = retained
    prior_records = await env.store.event_records.provider_budget_events(UUID(env.root.name))
    prior_payload = env.store.event_content_records.payload

    async def records(run_id):
        assert run_id.hex == env.root.name
        return [
            SimpleNamespace(id=r.id, data={"created_at": (boundary - timedelta(seconds=1)).isoformat(), **r.data})
            for r in prior_records
        ] + [SimpleNamespace(id=UUID(e["id"]), data=copy.deepcopy(e)) for e in events]

    async def payload(run_id, event_id, size, sha):
        import hashlib

        if event_id.hex not in actual:
            return await prior_payload(run_id, event_id, size, sha)
        raw = actual[event_id.hex]
        assert len(raw) == size and hashlib.sha256(raw).hexdigest() == sha
        return raw

    env.store.event_records = SimpleNamespace(provider_budget_events=records)
    env.store.event_content_records = SimpleNamespace(payload=payload)
    env.store.run.status = "failed"
    env.store.run.data.update(
        status="failed",
        implementation_rounds=3,
        summary=sample["summary"],
        usage={"input_tokens": 100, "output_tokens": 20},
        error="Downstream feedback validation failed",
    )
    return env, parent, path, events, actual


def request(review, path, **changes):
    values = dict(
        version=1,
        request_id=uuid4().hex,
        candidate_digest=review["digest"],
        admission_contract_digest=review["admission_contract_digest"],
        previous_catalog_path=path,
        valid_for_seconds=900,
        acknowledge_output_not_success=True,
        acknowledge_no_exit_attestation=True,
        acknowledge_original_budgets=True,
        acknowledge_no_replay=True,
        acknowledge_new_finite_attempt=True,
    )
    return module.SourceCompletedOutputInput(**{**values, **changes})


async def test_completed_invalid_output_reserves_exact_final_attempt_without_claiming_success(
    reconciliation_service, tmp_path, monkeypatch
):
    env, parent, path, events, contents = await completed_source(reconciliation_service, tmp_path, monkeypatch)
    assert hasattr(module, "SourceCompletedOutputReadmissions"), "Missing explicit observed-output native admission"
    service = module.SourceCompletedOutputReadmissions(env.service)
    old_entries = copy.deepcopy(await env.recoveries._entries(env.root.name))
    old_run = copy.deepcopy(env.store.run.data)
    index = (env.root / ".git/index").read_bytes()
    review = await service.candidate(env.root.name, parent["request"]["request_id"], path)
    body = request(review, path)
    receipt = await service.admit(env.root.name, parent["request"]["request_id"], body)
    assert receipt == await service.get(env.root.name, parent["request"]["request_id"], body.request_id)
    assert receipt == await service.admit(env.root.name, parent["request"]["request_id"], body)
    assert env.task.status == "queued" and env.task.attempt == 4 and env.store.run.status == "queued"
    assert receipt["effect"]["no_environment_effect"] is True
    assert review["observation"]["implementation_success"] is False
    assert review["observation"]["process_exit_attested"] is False
    assert receipt["candidate"]["source_edit"]["remaining_commands"] == 7
    assert receipt["candidate"]["source_edit"]["command_limit"] == 35
    assert (env.root / ".git/index").read_bytes() == index
    entries = await env.recoveries._entries(env.root.name)
    assert entries[: len(old_entries)] == old_entries
    assert not any(e["kind"] == "provider_failure_closed" for e in entries[len(old_entries) :])
    for key in ("commands", "implementation_rounds", "repair_budget", "worker_response", "provider_dispatch"):
        assert env.store.run.data[key] == old_run[key]
    assert await source_edit_receipt(env.recoveries, await env.store.get_run(env.root.name), qualify=True) == receipt
    key = uuid5(NAMESPACE_URL, f"forge:{env.root.name}:implement:4").hex
    assert await env.recoveries.begin(env.root.name) == {"idempotency_key": key, "disposition": "execute", "attempt": 4}
    new = next(
        e
        for e in entries
        if e["kind"] == "workspace_reconciliation_receipt" and e["idempotency_key"] == body.request_id
    )
    assert (
        module.validate_completed_output_parent(entries, new, receipt["actor"])["digest"]
        == env.store.run.data["provider_recovery"]["parent_receipt_digest"]
    )
    assert _ancestral_unknown(entries, new, receipt["actor"])
    assert (
        validated_source_plan(entries, new, receipt["actor"], env.store.run.data)["plan"]
        == receipt["candidate"]["plan"]
    )


@pytest.mark.parametrize(
    "change", ["missing_content", "summary", "source", "task", "rounds", "new_dispatch", "unknown", "identity"]
)
async def test_unproven_completed_output_or_scope_drift_never_creates_admission(
    reconciliation_service, tmp_path, monkeypatch, change
):
    from firefly_forge.project_access import AccessDenied

    env, parent, path, events, contents = await completed_source(reconciliation_service, tmp_path, monkeypatch)
    assert hasattr(module, "SourceCompletedOutputReadmissions"), "Missing explicit observed-output native admission"
    service = module.SourceCompletedOutputReadmissions(env.service)
    review = await service.candidate(env.root.name, parent["request"]["request_id"], path)
    body = request(review, path)
    if change == "missing_content":
        contents.clear()
    elif change == "summary":
        env.store.run.data["summary"] = "Different output"
    elif change == "source":
        (env.root / "frontend/app.js").write_text("changed source\n")
    elif change == "task":
        env.task.attempt = 4
    elif change == "rounds":
        env.store.run.data["implementation_rounds"] = 4
    elif change == "new_dispatch":
        await env.journal._append(
            env.root.name, "implement", "f" * 32, "provider_dispatch_intent", {"task_key": "f" * 32, "attempt": 4}
        )
    elif change == "unknown":
        await env.journal._append(
            env.root.name, "implement", "f" * 32, "workspace_reconciliation_intent", {"unknown": True}
        )
    else:
        env.identity.enabled = False
    with pytest.raises((ValueError, KeyError, AccessDenied)):
        await service.admit(env.root.name, parent["request"]["request_id"], body)
    assert env.store.run.status == "failed" and not any(
        r.data["idempotency_key"] == body.request_id for r in env.journal.records.rows
    )


@pytest.mark.parametrize("change", ["identity", "source", "summary", "task", "expiry"])
async def test_admission_wait_and_final_commit_recheck_custody(reconciliation_service, tmp_path, monkeypatch, change):
    from firefly_forge.project_access import AccessDenied

    env, parent, path, events, contents = await completed_source(reconciliation_service, tmp_path, monkeypatch)
    service = module.SourceCompletedOutputReadmissions(env.service)
    review = await service.candidate(env.root.name, parent["request"]["request_id"], path)
    body = request(review, path)
    if change == "expiry":
        original = env.journal._append

        async def append(*args):
            event = await original(*args)
            if args[3] == "workspace_reconciliation_receipt":
                monkeypatch.setattr(module, "clock_ms", lambda: event["payload"]["expires_at_ms"])
            return event

        monkeypatch.setattr(env.journal, "_append", append)
    else:

        async def admission(value):
            if change == "identity":
                env.identity.enabled = False
            elif change == "source":
                (env.root / "frontend/app.js").write_text("drift\n")
            elif change == "summary":
                env.store.run.data["summary"] = "Changed output"
            else:
                env.task.attempt = 4

        monkeypatch.setattr(env.store, "admit_credentials", admission)
    with pytest.raises((ValueError, AccessDenied)):
        await service.admit(env.root.name, parent["request"]["request_id"], body)
    # Native SQL controls separately prove rollback; the memory fixture cannot claim it.
    if change != "expiry":
        assert not any(r.data["idempotency_key"] == body.request_id for r in env.journal.records.rows)


@pytest.mark.parametrize("change", ["request", "expiry", "review_parent", "review_task", "observation", "plan"])
async def test_completed_receipt_ancestry_refuses_changed_consent_or_observation(
    reconciliation_service, tmp_path, monkeypatch, change
):
    env, parent, path, events, contents = await completed_source(reconciliation_service, tmp_path, monkeypatch)
    service = module.SourceCompletedOutputReadmissions(env.service)
    review = await service.candidate(env.root.name, parent["request"]["request_id"], path)
    body = request(review, path)
    await service.admit(env.root.name, parent["request"]["request_id"], body)
    entries = await env.recoveries._entries(env.root.name)
    receipt = next(
        e
        for e in entries
        if e["kind"] == "workspace_reconciliation_receipt" and e["idempotency_key"] == body.request_id
    )
    observed = next(e for e in entries if e["kind"] == "source_output_readmission")
    if change == "request":
        receipt["payload"]["completed_output_request"]["request_id"] = uuid4().hex
    elif change == "expiry":
        receipt["created_at"] = datetime.fromtimestamp(receipt["payload"]["expires_at_ms"] / 1000, UTC).isoformat()
    elif change == "review_parent":
        observed["payload"]["review"]["parent_receipt_digest"] = "a" * 64
    elif change == "review_task":
        observed["payload"]["review"]["previous_task"]["attempt"] = 2
    elif change == "observation":
        observed["payload"]["observation"]["process_exit_attested"] = True
    else:
        receipt["payload"]["candidate"]["plan"]["base_revision"] = "a" * 40
    from firefly_forge.project_access import AccessDenied

    with pytest.raises((ValueError, AccessDenied)):
        module.validate_completed_output_parent(entries, receipt, receipt["payload"]["actor"])
