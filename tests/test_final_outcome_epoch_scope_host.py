# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Completed final-result custody must retain its exact historical selection epoch."""

import copy
import hashlib
import json
import traceback
from types import SimpleNamespace
from uuid import uuid4

import pytest
import test_final_outcome_continuation_host as final_fixture

pytest_plugins = ["test_invalid_read_retained_epoch_engine_host"]


@pytest.fixture(autouse=True)
def frozen_original_write_grant(monkeypatch):
    import test_invalid_read_native_protocol_host as protocol

    original = protocol.continuity_case.__wrapped__

    def advanced(tmp_path, patch, request):
        return original(tmp_path, patch, SimpleNamespace(param=True))

    monkeypatch.setattr(protocol.continuity_case, "__wrapped__", advanced)


@pytest.fixture
async def completed_legacy_final(retained_epoch_engine_case, monkeypatch):
    from firefly_forge.agent_checkpoints import AgentCheckpoints

    c = retained_epoch_engine_case
    c.run_id = c.store.run.id.hex
    c.store.execution_requests = c.requests
    from firefly_forge.agentic_checkpoint_boundary import BoundaryCheckpoint, _checkpoint_payload
    from firefly_forge.invalid_read_continuity import RETAINED_SDK_RUNTIME_CONTRACT
    from firefly_forge.journal import ZERO_HASH, digest

    previous, rebound = ZERO_HASH, {}
    for row in c.journal.records.rows:
        value = row.data
        old_digest = value["digest"]
        if row.kind == "agent_checkpoint":
            payload = value["payload"]
            checkpoint = BoundaryCheckpoint.decode(payload["snapshot"].encode(), payload["checkpoint_digest"])
            record = json.loads(checkpoint.canonical_payload)
            record["compatibility"]["pydantic-ai-slim"] = "2.58.0"
            record["usage"].update(output_reasoning_tokens=0, cache_write_1h_tokens=0)
            record["contract"]["parameters"].update(
                _telemetry_mcp_url_ids=[], _telemetry_mcp_url_toolset_ids=[], _telemetry_mcp_url_tool_names=[]
            )
            physical = _checkpoint_payload(
                json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
            )
            payload.update(snapshot=physical.decode(), checkpoint_digest=hashlib.sha256(physical).hexdigest())
            payload["invocation"]["contract"] = RETAINED_SDK_RUNTIME_CONTRACT
        if row.kind == "agent_consume":
            for key in ("checkpoint_record_digest", "response_record_digest"):
                value["payload"][key] = rebound.get(value["payload"][key], value["payload"][key])
        value["previous_hash"] = previous
        value["digest"] = previous = digest({k: v for k, v in value.items() if k != "digest"})
        row.digest, row.previous_hash = previous, value["previous_hash"]
        rebound[old_digest] = previous
    monkeypatch.setattr(final_fixture, "RUN", c.run_id)
    original_result = final_fixture.result

    def selected_result(*args, **kwargs):
        response = original_result(*args, **kwargs)
        response["model"] = c.provider.settings["FORGE_BRIDGE_MODEL"]
        return response

    monkeypatch.setattr(final_fixture, "result", selected_result)
    failed = await final_fixture.consumed_blocker(c, monkeypatch)
    service = c.continuity.final_outcomes
    body = await final_fixture.reviewed_action(c, failed)
    await service.request(c.run_id, failed["id"], uuid4().hex, body)

    async def forbidden(*args, **kwargs):
        raise AssertionError("A completed final result cannot replay inference or consumption")

    monkeypatch.setattr(c.provider, "execute", forbidden)
    monkeypatch.setattr(AgentCheckpoints, "_consume", forbidden)
    await c.continuity.execute(failed["id"])
    ticket = await c.continuity.get(c.run_id, failed["id"])
    assert ticket["status"] == "completed", ticket.get("result")
    assert (await c.store.get_run(c.run_id))["status"] == "paused"
    c.completed_ticket = ticket
    return c


async def test_completed_legacy_final_reaches_verification_candidate_without_scope_leak(completed_legacy_final):
    from firefly_forge.checkpoint_mode_runtime import prepare_current_mode
    from firefly_forge.verification_handoffs import VerificationHandoffs

    c = completed_legacy_final
    before = copy.deepcopy(await c.journal.entries(c.run_id))
    handoffs = VerificationHandoffs(c.store, c.journal, c.engine)
    candidate = await handoffs.candidates(c.run_id, "bridge")
    assert candidate["run_id"] == c.run_id and candidate["commands_count"] == len(c.store.run.data["commands"])
    assert await c.journal.entries(c.run_id) == before and len(c.posts) == 3
    with pytest.raises(ValueError, match="policy epoch"):
        await prepare_current_mode(c.journal, c.run_id)


async def test_completed_final_allows_explicit_current_policy_adoption_and_checks_without_replay(
    completed_legacy_final, monkeypatch
):
    from firefly_forge.tool_broker import FactoryToolPolicyInput, ToolInvocationBroker
    from firefly_forge.verification_handoffs import VerificationHandoffs
    from firefly_forge.verification_models import VerificationAdoption

    c = completed_legacy_final
    before = copy.deepcopy(await c.journal.entries(c.run_id))
    original = copy.deepcopy(await c.store.get_run(c.run_id))
    from firefly_forge import verification_scope_attestation

    async def no_native_transfer(store):
        assert store is c.store and getattr(store, "native_location_state", None) is None
        return None

    monkeypatch.setattr(verification_scope_attestation, "current_native_location", no_native_transfer)
    broker = ToolInvocationBroker(c.store, c.journal, c.store.configuration.directory.parent)
    old = broker.snapshot()
    settings = copy.deepcopy(old["settings"])
    settings["automatic_verification"]["max_continuations"] = 6
    updated = broker.configure(FactoryToolPolicyInput.model_validate(settings))
    assert updated["settings"] == settings and old["revision"] != updated["revision"]
    handoffs = VerificationHandoffs(c.store, c.journal, c.engine)
    candidate = await handoffs.candidates(c.run_id, "bridge")
    request = VerificationAdoption(
        request_id=uuid4().hex,
        **{
            k: candidate[k]
            for k in (
                "pending_request_digest",
                "source_digest",
                "commands_digest",
                "policy_digest",
                "provider_digest",
                "agent",
            )
        },
    )
    adopted = await handoffs.adopt(c.run_id, request)
    assert adopted["policy_snapshot"] == updated
    observed = []
    hold = c.journal.verification_hold

    async def record_hold(*args, **kwargs):
        observed.append(traceback.format_exc())
        return await hold(*args, **kwargs)

    monkeypatch.setattr(c.journal, "verification_hold", record_hold)
    await handoffs.execute(c.run_id)
    current = await c.store.get_run(c.run_id)
    assert current["status"] == "queued" and current["verification_handoff"].get("status") == "completed", json.dumps(
        {
            "exception": observed,
            "refusal": current.get("verification_refusal"),
            "handoff": current.get("verification_handoff"),
            "error": current.get("error"),
        }
    )
    assert current["verification_handoff"]["max_continuations"] == 6
    assert current["usage"] == original["usage"]
    assert current["sandbox_snapshot"] == original["sandbox_snapshot"]
    assert current["tool_policy_snapshot"] == original["tool_policy_snapshot"]
    assert len(c.posts) == 3
    entries = await c.journal.entries(c.run_id)
    assert entries[: len(before)] == before
    assert [e for e in entries if e["kind"] == "agent_consume"] == [e for e in before if e["kind"] == "agent_consume"]


def test_completed_final_authority_is_sealed_in_runtime_and_admission(monkeypatch):
    from pathlib import Path

    from test_source_edit_v14_transition_host import declared_names

    from firefly_forge import source_edit_qualification
    from firefly_forge.agentic_checkpoint_invocation import runtime_contract
    from firefly_forge.checkpoint_mode_runtime import policy_digest as mode_policy
    from firefly_forge.source_budget_allowances import admission_contract_digest
    from firefly_forge.unknown_command_acknowledgements import policy_digest as ack_policy

    root = Path(source_edit_qualification.__file__).parent
    name = "completed_final_epoch.py"
    runtime = declared_names(root / "agentic_checkpoint_invocation.py", "runtime_contract", 21)
    admission = declared_names(root / "source_budget_allowances.py", "admission_contract_digest", 35)
    assert runtime.count(name) == admission.count(name) == 1
    before = runtime_contract(), admission_contract_digest(), mode_policy(), ack_policy()
    read = Path.read_bytes
    monkeypatch.setattr(
        Path, "read_bytes", lambda p: read(p) + b"\nchanged-native-authority\n" if p.name == name else read(p)
    )
    assert runtime_contract() != before[0] and admission_contract_digest() != before[1]
    assert mode_policy() != before[2] and ack_policy() != before[3]


async def test_completed_epoch_refuses_corrupt_custody_scope_usage_and_current_revocations(
    completed_legacy_final, monkeypatch
):
    from firefly_forge import invalid_read_continuities
    from firefly_forge.completed_final_epoch import validated_completed_final
    from firefly_forge.invalid_read_lease import ContinuityLeasePolicyV1, write_policy
    from firefly_forge.project_access import AccessDenied
    from firefly_forge.verification_handoffs import VerificationHandoffs

    c = completed_legacy_final
    saved_run = copy.deepcopy(await c.store.get_run(c.run_id))
    saved_records = copy.deepcopy(c.journal.records.rows)
    saved_requests = copy.deepcopy(c.requests.records.rows)
    saved_events = copy.deepcopy(c.requests.journal.rows)
    config_path = c.store.configuration.directory / "invalid-read-continuity-policy.json"
    saved_policy = c.store.configuration._read(config_path)
    for change in (
        "missing_terminal",
        "terminal_payload",
        "broken_chain",
        "foreign_scope",
        "provider",
        "configuration",
        "usage_rewind",
        "usage_float",
        "ticket_failed",
        "missing_service",
        "foreign_request_store",
        "revoked_policy",
        "unqualified",
        "actor",
        "cancelled",
    ):
        c.store.run.data, c.store.run.status = copy.deepcopy(saved_run), saved_run["status"]
        c.store.execution_requests = c.requests
        c.journal.records.rows = copy.deepcopy(saved_records)
        c.requests.records.rows, c.requests.journal.rows = copy.deepcopy((saved_requests, saved_events))
        c.store.configuration._write(config_path, saved_policy)
        with monkeypatch.context() as patch:
            if change == "missing_terminal":
                c.journal.records.rows = [r for r in c.journal.records.rows if r.kind != "terminal_completion_receipt"]
            elif change == "terminal_payload":
                next(r for r in c.journal.records.rows if r.kind == "terminal_completion_receipt").data["payload"][
                    "proof"
                ]["task_key"] = "foreign-task"
            elif change == "broken_chain":
                c.journal.records.rows[-1].data["previous_hash"] = "0" * 64
            elif change == "foreign_scope":
                c.store.run.data["brief"] = "Another scope"
            elif change == "provider":
                c.store.run.data["agent"] = "claude"
            elif change == "configuration":
                c.store.run.data["config_revision"] = uuid4().hex
            elif change == "usage_rewind":
                c.store.run.data["usage"]["requests"] -= 1
            elif change == "usage_float":
                c.store.run.data["usage"]["requests"] = float(c.store.run.data["usage"]["requests"])
            elif change == "ticket_failed":
                c.requests.records.rows[0].status = c.requests.records.rows[0].data["status"] = "failed"
            elif change == "missing_service":
                c.store.execution_requests = None
            elif change == "foreign_request_store":
                patch.setattr(c.requests, "store", SimpleNamespace())
            elif change == "revoked_policy":
                write_policy(c.store.configuration, ContinuityLeasePolicyV1(version=1, enabled=False))
            elif change == "cancelled":
                c.store.run.data["cancel_request"] = {"status": "requested"}
            elif change == "actor":
                patch.setattr(c.user, "enabled", False)
            else:

                def revoked():
                    raise ValueError("Unqualified current closure")

                patch.setattr(invalid_read_continuities, "current_profile_seal", revoked)
            with pytest.raises((ValueError, AccessDenied), match=".+"):
                await VerificationHandoffs(c.store, c.journal, c.engine).candidates(c.run_id, "bridge")
            assert len(c.posts) == 3, change
    c.store.run.data, c.store.run.status = copy.deepcopy(saved_run), saved_run["status"]
    c.journal.records.rows, c.requests.records.rows, c.requests.journal.rows = copy.deepcopy(
        (saved_records, saved_requests, saved_events)
    )
    c.store.configuration._write(config_path, saved_policy)
    c.store.execution_requests = c.requests
    assert await validated_completed_final(c.journal, await c.store.get_run(c.run_id)) is not None


async def test_completed_epoch_revalidates_exit_and_restores_context_on_exception(completed_legacy_final, monkeypatch):
    from firefly_forge import invalid_read_continuities
    from firefly_forge.checkpoint_mode_runtime import mode_execution_scope, prepare_current_mode

    c = completed_legacy_final
    seal = invalid_read_continuities.current_profile_seal()
    with pytest.raises(ValueError, match="custody|epoch"):
        async with mode_execution_scope(c.journal, c.run_id):
            monkeypatch.setattr(invalid_read_continuities, "current_profile_seal", lambda: {**seal, "digest": "f" * 64})
    with pytest.raises(ValueError, match="policy epoch"):
        await prepare_current_mode(c.journal, c.run_id)
    monkeypatch.setattr(invalid_read_continuities, "current_profile_seal", lambda: seal)
    with pytest.raises(RuntimeError, match="caller interruption"):
        async with mode_execution_scope(c.journal, c.run_id):
            raise RuntimeError("caller interruption")
    with pytest.raises(ValueError, match="policy epoch"):
        await prepare_current_mode(c.journal, c.run_id)
    assert len(c.posts) == 3


async def test_completed_final_claim_window_is_not_a_later_verification_deadline(completed_legacy_final, monkeypatch):
    from firefly_forge import final_outcome_continuations
    from firefly_forge.verification_handoffs import VerificationHandoffs

    c = completed_legacy_final
    final = next(e for e in await c.journal.entries(c.run_id) if e["kind"] == "final_outcome_continuation")
    monkeypatch.setattr(
        final_outcome_continuations,
        "time",
        SimpleNamespace(time=lambda: final["payload"]["lease"]["expires_at_ms"] / 1000 + 10000),
    )
    result = await VerificationHandoffs(c.store, c.journal, c.engine).candidates(c.run_id, "bridge")
    assert result["run_id"] == c.run_id and len(c.posts) == 3
