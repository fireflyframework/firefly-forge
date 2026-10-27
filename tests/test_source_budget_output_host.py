# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Explicit known failed-output consent, without a fabricated question or provider replay."""

# ruff: noqa: F401,F811
from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import pytest
from test_source_budget_paused_invocation_host import paused_invocation
from test_source_budget_service_host import allowance_request
from test_source_edit_continuations_host import controlled_metadata
from test_workspace_dependency_reconciliation import legacy_workspace
from test_workspace_reconciliation_journal import environment_journal
from test_workspace_reconciliation_service import reconciliation_service

from firefly_forge import source_budget_allowances as module
from firefly_forge.activity_models import project_activity


async def known_failed(env, tmp_path, monkeypatch):
    from dataclasses import replace

    import source_edit_qualification_fixtures as profiles

    from firefly_forge.source_edit_qualification import profile_contract_digest

    original_profile = profiles.fixture_profile

    def future_profile(executable):
        profile = original_profile(executable)
        limits = (("read_file_lifetime_limit", 20), ("activation_ceiling", 6))
        profile = replace(profile, source_budget_limits=limits, proof=replace(profile.proof, budget_limits=limits))
        return replace(profile, proof=replace(profile.proof, contract_digest=profile_contract_digest(profile)))

    monkeypatch.setattr(profiles, "fixture_profile", future_profile)
    env, parent, path, history, *_ = await paused_invocation(env, tmp_path, monkeypatch)
    service = module.SourceBudgetReadmissions(env.service)
    proposal = module.SourceBudgetProposal(
        version=1, read_file_lifetime_limit=20, activation_ceiling=5, additional_activations=1
    )
    review = await service.candidate(env.root.name, parent["request"]["request_id"], path, proposal)
    parent = await service.admit(env.root.name, parent["request"]["request_id"], allowance_request(review, path))
    run = env.store.run.data
    key = uuid5(NAMESPACE_URL, f"forge:{env.root.name}:implement:5").hex
    env.task.status = "running"
    env.task.data = {"idempotency_key": key}
    run.update(status="developing", active_task="implement")
    await env.journal._append(env.root.name, "implement", key, "intent", {"attempt": 5})
    dispatch = await env.journal._append(
        env.root.name,
        "implement",
        key + ":provider",
        "provider_dispatch_intent",
        {"version": 1, "task_key": key, "attempt": 5},
    )
    run["provider_dispatch"] = {"task_key": key, "cut": env.recoveries._cut(run), "actor": parent["actor"]}
    summary = json.dumps(
        {
            "summary": "The earlier diagnostic was missing from this invocation",
            "feedback": {
                "id": "invalid-final-five",
                "role_id": "implementer",
                "task_id": "implement",
                "attempt": 5,
                "protocol": "source-budget-continuation-v1",
                "allowance_id": parent["request"]["request_id"],
                "allowance_digest": run["source_edit_continuation"]["receipt_digest"],
                "disposition": "blocked",
                "summary": "No new question was supplied",
                "request": None,
            },
        }
    )
    usage = {"input_tokens": 7, "output_tokens": 3}
    payloads = [
        {"type": "thread.started", "thread_id": "known-output-five"},
        {"type": "turn.started"},
        {"type": "item.completed", "item": {"id": "final", "type": "agent_message", "text": summary}},
        {"type": "turn.completed", "usage": usage},
    ]
    previous = await env.store.event_records.provider_budget_events(UUID(env.root.name))
    previous_payload = env.store.event_content_records.payload
    events, contents = [], {}
    boundary = datetime.fromisoformat(dispatch["created_at"])
    for offset, payload in enumerate(payloads, 1):
        event_id = uuid4().hex
        projection, retained = project_activity(env.root.name, event_id, "stdout", json.dumps(payload), retain=True)
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
        return previous + [SimpleNamespace(id=UUID(e["id"]), data=copy.deepcopy(e)) for e in events]

    async def payload(run_id, event_id, size, sha):
        if event_id.hex not in contents:
            return await previous_payload(run_id, event_id, size, sha)
        raw = contents[event_id.hex]
        assert len(raw) == size and hashlib.sha256(raw).hexdigest() == sha
        return raw

    env.store.event_records.provider_budget_events = records
    env.store.event_content_records.payload = payload
    for i, tool in enumerate(["read_file"] * 9 + ["list_files", "search_text"], 14):
        history.append(
            {
                "correlation_id": f"{i:032x}",
                "request_digest": f"{i + 100:064x}",
                "status": "succeeded",
                "admission": {"tool": tool},
            }
        )
    (env.root / "docs").mkdir(exist_ok=True)
    (env.root / "docs/CONTINUATION_STATUS.md").write_text("Retained model-authored status, not a successful repair.\n")
    run.update(
        status="failed",
        implementation_rounds=5,
        summary=summary,
        usage=usage,
        error="Worker feedback schema refused blocked with no request",
    )
    env.store.run.status = "failed"
    module.write_policy(
        env.store.configuration,
        module.SourceBudgetPolicy(
            enabled=True, max_read_file_lifetime_limit=20, max_activation_ceiling=6, max_additional_activations=2
        ),
    )
    return env, parent, path, history, events, contents


def proposal():
    return module.SourceBudgetProposal(
        version=1,
        basis="known_completed_output",
        read_file_lifetime_limit=20,
        activation_ceiling=6,
        additional_activations=1,
    )


def request(review, path):
    return module.SourceBudgetAllowanceInput(
        version=1,
        request_id=uuid4().hex,
        candidate_digest=review["digest"],
        admission_contract_digest=review["admission_contract_digest"],
        previous_catalog_path=path,
        valid_for_seconds=900,
        proposal=proposal(),
        acknowledge_lifetime_history_preserved=True,
        acknowledge_original_commands_and_final_reserve=True,
        acknowledge_one_new_canonical_activation=True,
        acknowledge_no_replay=True,
        acknowledge_output_not_success=True,
        acknowledge_no_exit_attestation=True,
        acknowledge_current_source_changes=True,
        reviewed_source_digest=review["cut"]["source_digest"],
        reviewed_patch_sha256=review["source_review"]["patch_sha256"],
    )


async def test_known_failed_fifth_requires_explicit_variant_then_queues_six_without_replaying(
    reconciliation_service, tmp_path, monkeypatch
):
    env, parent, path, history, *_ = await known_failed(reconciliation_service, tmp_path, monkeypatch)
    service = module.SourceBudgetReadmissions(env.service)
    with pytest.raises(ValueError):
        await service.candidate(
            env.root.name,
            parent["request"]["request_id"],
            path,
            module.SourceBudgetProposal(
                version=1, read_file_lifetime_limit=20, activation_ceiling=6, additional_activations=1
            ),
        )
    before = copy.deepcopy(env.store.run.data)
    entries = copy.deepcopy(await env.recoveries._entries(env.root.name))
    index = (env.root / ".git/index").read_bytes()
    review = await service.candidate(env.root.name, parent["request"]["request_id"], path, proposal())
    assert review["basis"] == "known_completed_output"
    assert review["observation"]["implementation_success"] is False
    assert review["observation"]["process_exit_attested"] is False
    assert "docs/CONTINUATION_STATUS.md" in review["source_review"]["untracked"]
    receipt = await service.admit(env.root.name, parent["request"]["request_id"], request(review, path))
    assert receipt == await service.get(
        env.root.name, parent["request"]["request_id"], receipt["request"]["request_id"]
    )
    assert receipt == await service.admit(
        env.root.name,
        parent["request"]["request_id"],
        module.SourceBudgetAllowanceInput.model_validate(receipt["human_request"]),
    )
    assert await env.recoveries.begin(env.root.name) is None
    assert env.store.run.status == "queued" and env.task.status == "queued" and env.task.attempt == 6
    assert receipt["candidate"]["source_budget_readmission"]["basis"] == "known_completed_output"
    assert (env.root / ".git/index").read_bytes() == index
    assert (await env.recoveries._entries(env.root.name))[: len(entries)] == entries
    assert env.store.run.data["implementation_rounds"] == 5
    for key in [
        "commands",
        "repair_budget",
        "worker_response",
        "worker_feedback",
        "provider_recovery",
        "summary",
        "usage",
    ]:
        assert env.store.run.data[key] == before[key]
    assert len(history) == 24 and sum(r["admission"]["tool"] == "read_file" for r in history) == 19
    assert receipt["candidate"]["source_edit"]["remaining_commands"] == 7
    assert receipt["candidate"]["source_edit"]["command_limit"] == 35
    tail = (await env.recoveries._entries(env.root.name))[len(entries) :]
    assert not any(
        e["kind"] in {"provider_dispatch_intent", "worker_feedback", "worker_response", "resume"} for e in tail
    )


@pytest.mark.parametrize(
    "change",
    ["missing_turn", "summary", "usage", "dispatch", "index", "recovery", "unknown", "policy", "cancel", "active"],
)
async def test_known_output_refuses_unknown_drift_or_authority_without_new_intent(
    reconciliation_service, tmp_path, monkeypatch, change
):
    import asyncio

    env, parent, path, history, events, _ = await known_failed(reconciliation_service, tmp_path, monkeypatch)
    run = env.store.run.data
    active = None
    if change == "missing_turn":
        events.pop()
    elif change == "summary":
        run["summary"] += "changed"
    elif change == "usage":
        run["usage"]["input_tokens"] += 1
    elif change == "dispatch":
        run["provider_dispatch"]["cut"]["source_digest"] = "f" * 64
    elif change == "index":
        with (env.root / ".git/index").open("ab") as stream:
            stream.write(b"foreign")
    elif change == "recovery":
        run["provider_recovery"]["status"] = "reserved"
    elif change == "unknown":
        await env.journal._append(
            env.root.name, "implement", "f" * 32, "workspace_reconciliation_intent", {"candidate": {}}
        )
    elif change == "policy":
        module.write_policy(env.store.configuration, module.SourceBudgetPolicy())
    elif change == "cancel":
        run["cancel_request"] = {"status": "requested"}
    else:
        active = asyncio.get_running_loop().create_future()
        env.recoveries.engine.active[env.root.name] = active
    before = len(env.journal.records.rows)
    try:
        with pytest.raises((ValueError, RuntimeError)):
            await module.SourceBudgetReadmissions(env.service).candidate(
                env.root.name, parent["request"]["request_id"], path, proposal()
            )
    finally:
        if active is not None:
            active.cancel()
    assert len(env.journal.records.rows) == before and env.task.attempt == 5
    assert len(history) == 24 and env.store.run.status == "failed"


@pytest.mark.parametrize("change", ["source", "history", "policy", "administrator", "credential"])
async def test_known_output_rechecks_exact_source_history_and_authority_after_credential_wait(
    reconciliation_service, tmp_path, monkeypatch, change
):
    from firefly_forge.project_access import AccessDenied
    from firefly_forge.store import Store

    env, parent, path, history, *_ = await known_failed(reconciliation_service, tmp_path, monkeypatch)
    service = module.SourceBudgetReadmissions(env.service)
    review = await service.candidate(env.root.name, parent["request"]["request_id"], path, proposal())
    before = len(env.journal.records.rows)

    async def changed(store, patch):
        if change == "source":
            (env.root / "docs/CONTINUATION_STATUS.md").write_text("A different public document.\n")
        elif change == "history":
            history.append(
                {
                    "correlation_id": "e" * 32,
                    "request_digest": "e" * 64,
                    "status": "succeeded",
                    "admission": {"tool": "read_file"},
                }
            )
        elif change == "policy":
            module.write_policy(env.store.configuration, module.SourceBudgetPolicy())
        elif change == "administrator":

            async def revoked(*args):
                raise AccessDenied("The operator was revoked during admission")

            monkeypatch.setattr(env.recoveries, "_operator", revoked)
        else:
            raise ValueError("Retired credential was found in a new public source file")

    monkeypatch.setattr(Store, "admit_source_patch", changed)
    with pytest.raises((ValueError, AccessDenied)):
        await service.admit(env.root.name, parent["request"]["request_id"], request(review, path))
    assert len(env.journal.records.rows) == before and env.task.attempt == 5 and env.store.run.status == "failed"


def test_legacy_budget_consent_serialization_stays_byte_and_digest_identical():
    legacy = {
        "version": 1,
        "request_id": "a" * 32,
        "candidate_digest": "b" * 64,
        "admission_contract_digest": "c" * 64,
        "previous_catalog_path": "/retained/package/resources/native/codex-0.163.0/source-only-model.json",
        "valid_for_seconds": 900,
        "proposal": {
            "version": 1,
            "read_file_lifetime_limit": 20,
            "activation_ceiling": 5,
            "additional_activations": 1,
        },
        "acknowledge_lifetime_history_preserved": True,
        "acknowledge_original_commands_and_final_reserve": True,
        "acknowledge_one_new_canonical_activation": True,
        "acknowledge_no_replay": True,
    }
    parsed = module.SourceBudgetAllowanceInput.model_validate(legacy).model_dump()
    assert parsed == legacy and module.digest(parsed) == module.digest(legacy)
    assert json.dumps(parsed, sort_keys=True).encode() == json.dumps(legacy, sort_keys=True).encode()
    assert "basis" not in parsed["proposal"]
    with pytest.raises(ValueError):
        module.SourceBudgetAllowanceInput.model_validate({**legacy, "proposal": proposal().model_dump()})
    with pytest.raises(ValueError):
        module.SourceBudgetAllowanceInput.model_validate({**legacy, "acknowledge_output_not_success": True})


def test_explicit_output_basis_round_trips_sdk_cli_without_new_authority(monkeypatch):
    import httpx
    from click.testing import CliRunner
    from test_original_recovery_sdk import connection

    from firefly_forge import cli

    calls = []

    def transport(request):
        calls.append(request)
        return httpx.Response(200, json={"review_required": True})

    with connection(transport) as client:
        client.source_budget_candidate("a" * 32, "b" * 32, "/retained/model.json", proposal())
    assert calls[0].url.params["basis"] == "known_completed_output"
    assert calls[0].url.params["activation_ceiling"] == "6"
    monkeypatch.setattr(cli, "client", lambda: connection(transport))
    result = CliRunner().invoke(
        cli.main,
        [
            "source-budget",
            "candidate",
            "a" * 32,
            "b" * 32,
            "--previous-catalog-path",
            "/retained/model.json",
            "--read-file-lifetime-limit",
            "20",
            "--activation-ceiling",
            "6",
            "--additional-activations",
            "1",
            "--basis",
            "known_completed_output",
        ],
    )
    assert result.exit_code == 0, result.output
    assert len(calls) == 2 and all(call.method == "GET" for call in calls)
    assert calls[1].url.params["basis"] == "known_completed_output"


def test_current_v10_declares_entire_new_allowance_module_without_relabeling_historical_registry():
    from firefly_forge import source_edit_qualification as qualification
    from firefly_forge.source_edit_supported_profiles import PROFILES

    assert qualification.LOADER_PROFILE == "codex-exec-restricted-source/v20"
    assert qualification.REGISTRY_VERSION == 19
    assert len(qualification.LOADER_MODULES) == 22
    assert "source_budget_output.py" in qualification.LOADER_MODULES
    assert "known_completed_output_finite_activation" in qualification.SOURCE_BUDGET_CHECKS
    historical = {profile.loader_profile: profile for profile in PROFILES}
    assert historical["codex-exec-restricted-source/v5"].proof.artifact_sha256 == (
        "5a29bcea8327bf04ee2aedbac99e15603e3aca8709ae9f40c60da965cf92f078"
    )
    assert historical["codex-exec-restricted-source/v6"].proof.artifact_sha256 == (
        "e0a21355651f9f0cff58a19e162c85a0d6eb1bc2bc0f52d0575e156ce05ed516"
    )


def test_new_versioned_source_prompt_distinguishes_known_output_from_answered_questions():
    from firefly_forge.prompts import prompt_text

    text = prompt_text(
        "implementation/source-edit-only",
        version="1.0.3",
        calls_limit="30",
        per_tool_limit="10",
        command_limit="35",
        cli_commands="21",
        native_commands="7",
        final_checks="7",
        capabilities=json.dumps({"total": {"used": 24, "limit": 30, "remaining": 6}}),
    )
    assert "explicit admission basis" in text and "known completed turn" in text
    assert "historical source-bound context" in text and "replays" in text
    assert "tool_budget_exhausted" in text and "Unknown" in text and '"used": 24' in text
    old = prompt_text(
        "implementation/source-edit-only",
        version="1.0.2",
        calls_limit="30",
        per_tool_limit="10",
        command_limit="35",
        cli_commands="21",
        native_commands="7",
        final_checks="7",
        capabilities="{}",
    )
    assert "binds its existing answered question" in old


@pytest.mark.parametrize("boundary", ["final_authority_wait", "final_source_manifest"])
async def test_known_output_refuses_consent_expiring_at_last_admission_boundary(
    reconciliation_service, tmp_path, monkeypatch, boundary
):
    from contextlib import asynccontextmanager

    from firefly_forge import assurance, source_budget_output

    env, parent, path, *_ = await known_failed(reconciliation_service, tmp_path, monkeypatch)
    service = module.SourceBudgetReadmissions(env.service)
    review = await service.candidate(env.root.name, parent["request"]["request_id"], path, proposal())
    body = request(review, path)
    clock = {"now": source_budget_output.clock_ms(), "checks": 0, "manifest_expired": False}
    monkeypatch.setattr(source_budget_output, "clock_ms", lambda: clock["now"])
    authorize = source_budget_output.SourceBudgetOutputReadmissions._assert_admission_current
    manifest = assurance._manifest
    unit = env.store.source_budget_admission_unit
    exits = []

    async def authority_wait(current, *args):
        await authorize(current, *args)
        clock["checks"] += 1
        if clock["checks"] == 3 and boundary == "final_authority_wait":
            clock["now"] += body.valid_for_seconds * 1000

    def source_manifest(*args, **kwargs):
        result = manifest(*args, **kwargs)
        if clock["checks"] == 3 and boundary == "final_source_manifest":
            clock["now"] += body.valid_for_seconds * 1000
            clock["manifest_expired"] = True
        return result

    @asynccontextmanager
    async def admission_unit():
        try:
            async with unit():
                yield
        except ValueError:
            exits.append("rollback_required")
            raise

    monkeypatch.setattr(
        source_budget_output.SourceBudgetOutputReadmissions, "_assert_admission_current", authority_wait
    )
    monkeypatch.setattr(assurance, "_manifest", source_manifest)
    monkeypatch.setattr(env.store, "source_budget_admission_unit", admission_unit)
    with pytest.raises(
        ValueError, match="public source, exact index or finite consent changed at the final native commit boundary"
    ):
        await service.admit(env.root.name, parent["request"]["request_id"], body)
    assert clock["checks"] == 3 and exits == ["rollback_required"]
    assert clock["manifest_expired"] is (boundary == "final_source_manifest")
