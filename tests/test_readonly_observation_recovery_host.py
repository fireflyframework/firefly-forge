# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Consumed successful model turns retain known results and unknown observations separately."""

import copy
import json

import httpx2
import pytest
from test_agentic_checkpoint_activation_host import result
from test_bridge_inflight_cancel import RUN
from test_rejected_boundary_recovery_host import evidence, refused_claim

pytest_plugins = ["test_invalid_read_native_protocol_host"]


async def failed_observation(c, monkeypatch):
    from firefly_forge.invalid_read_lease import ContinuityLeasePolicyV1, write_policy
    from firefly_forge.provider_rejection_models import RejectedBoundaryRecoveryInputV1
    from firefly_forge.run_evidence import EvidenceUnavailable, RunEvidence

    failed, ordinary = await refused_claim(c)
    write_policy(
        c.store.configuration,
        ContinuityLeasePolicyV1(version=1, enabled=True, max_window_seconds=180, allow_pre_admission_recovery=True),
    )
    reviewed = evidence(c, failed)
    candidate = await c.continuity.rejections.candidate(RUN, failed["id"], reviewed)
    body = RejectedBoundaryRecoveryInputV1(
        version=1,
        candidate_digest=candidate["digest"],
        evidence=reviewed,
        valid_for_seconds=180,
        acknowledge_reviewed_provider_epoch=True,
        acknowledge_no_original_response_or_tool_replay=True,
        acknowledge_same_request_attempt_and_budgets=True,
        acknowledge_finite_ownership_lease=True,
    )
    await c.continuity.rejections.request(RUN, failed["id"], "c" * 32, body)
    calls = []

    async def missing(self, run_id, query, *, task_id):
        calls.append(query.model_dump())
        raise EvidenceUnavailable("evidence_integrity")

    monkeypatch.setattr(RunEvidence, "for_worker", missing)

    def transport(request):
        if request.method == "GET":
            return ordinary(request)
        c.posts.append(request.content)
        response = result(tool=True)
        response["choices"][0]["message"]["tool_calls"] = [
            {"id": "retained-list", "type": "function", "function": {"name": "list_files", "arguments": "{}"}},
            {
                "id": "missing-evidence",
                "type": "function",
                "function": {"name": "read_run_evidence", "arguments": '{"query":{"kind":"journal","limit":2}}'},
            },
        ]
        return httpx2.Response(200, json=response)

    c.provider.model().client._client._transport = httpx2.MockTransport(transport)
    failures = []
    execute = c.continuity.rejections.execute

    async def observed_execute(ticket):
        try:
            return await execute(ticket)
        except Exception as error:
            failures.append(repr(error))
            raise

    with monkeypatch.context() as observed:
        observed.setattr(c.continuity.rejections, "execute", observed_execute)
        await c.continuity.execute(failed["id"])
    ticket = await c.continuity.get(RUN, failed["id"])
    assert ticket["status"] == "needs_reconciliation" and len(calls) == 1, failures
    entries = copy.deepcopy([r.data for r in c.journal.records.rows])
    assert [e["kind"] for e in entries[-8:]] == [
        "bridge_request_intent",
        "agent_checkpoint",
        "bridge_request_response",
        "agent_consume",
        "agentic_tool_intent",
        "agentic_tool_result",
        "agentic_tool_intent",
        "agentic_tool_unknown",
    ]
    return ticket, entries, calls


@pytest.mark.parametrize("continuity_case", [True], indirect=True)
async def test_retained_result_and_unavailable_observation_reconstruct_without_callback(continuity_case, monkeypatch):
    from firefly_forge.agentic_checkpoint_boundary import BoundaryCheckpoint
    from firefly_forge.agentic_checkpoint_invocation import runtime_contract
    from firefly_forge.readonly_observation_reconstruction import prepare_observation, reconstruct_observation

    c = continuity_case
    ticket, entries, calls = await failed_observation(c, monkeypatch)
    run = await c.store.get_run(RUN)
    # The journal task's public shape comes from the same native reader used by admission.
    _, task, _ = await c.continuity.recoveries._native(RUN)
    prepared = prepare_observation(run, task, entries, accepted_runtime=runtime_contract())
    before = copy.deepcopy(entries)
    derived = await reconstruct_observation(prepared)
    assert len(calls) == 1 and calls[0]["kind"] == "journal" and calls[0]["limit"] == 2
    assert [r.data for r in c.journal.records.rows] == before
    original = prepared.checkpoint
    assert derived.usage.requests == original.usage.requests + 1
    assert derived.usage.tool_calls == original.usage.tool_calls + 2
    assert derived.record.limits == original.record.limits
    assert derived.history[: len(original.history)] == original.history
    outcomes = {p.tool_call_id: p.content for p in derived.history[-1].parts}
    known = next(e["payload"]["result"] for e in entries[-4:] if e["kind"] == "agentic_tool_result")
    assert outcomes["retained-list"] == known
    assert outcomes["missing-evidence"] == {
        "status": "observation_unavailable",
        "reason": "original_read_result_not_retained",
    }
    assert BoundaryCheckpoint.decode(derived.payload, derived.digest).digest == derived.digest
    assert derived.digest != original.digest and ticket["checkpoint_digest"] == original.digest


@pytest.mark.parametrize("continuity_case", [True], indirect=True)
async def test_same_ticket_observation_recovery_reaches_new_turn_without_original_callbacks(
    continuity_case, monkeypatch
):
    from firefly_forge.invalid_read_lease import ContinuityLeasePolicyV1, write_policy
    from firefly_forge.readonly_observation_models import ReadonlyObservationRecoveryInputV1

    c = continuity_case
    ticket, entries, calls = await failed_observation(c, monkeypatch)
    service = c.continuity.observations
    candidate = await service.candidate(RUN, ticket["id"])
    assert candidate["admission"] == "HOLD" and "readonly_observation_recovery_disabled" in candidate["holds"]
    write_policy(
        c.store.configuration,
        ContinuityLeasePolicyV1(
            version=1,
            enabled=True,
            max_window_seconds=180,
            allow_pre_admission_recovery=True,
            allow_readonly_observation_recovery=True,
        ),
    )
    candidate = await service.candidate(RUN, ticket["id"])
    assert candidate["admission"] == "READY"
    body = ReadonlyObservationRecoveryInputV1(
        version=1,
        candidate_digest=candidate["digest"],
        valid_for_seconds=180,
        acknowledge_unavailable_observation=True,
        acknowledge_no_original_callback_replay=True,
        acknowledge_same_request_attempt_and_budgets=True,
        acknowledge_finite_ownership_lease=True,
    )
    response = await service.request(RUN, ticket["id"], "e" * 32, body)
    assert response["status"] == "queued" and await service.request(RUN, ticket["id"], "e" * 32, body) == response
    original = copy.deepcopy([r.data for r in c.journal.records.rows][: len(entries)])
    turns = []

    def resumed(request):
        if request.method == "GET":
            from test_bridge_completion_callbacks import CAPABILITY

            return httpx2.Response(200, json={"completionRecovery": CAPABILITY})
        assert request.method == "POST"
        c.posts.append(request.content)
        turns.append(json.loads(request.content))
        return httpx2.Response(
            200,
            json=result(
                json.dumps(
                    {
                        "summary": "Reviewed observation continued",
                        "worker_feedback": {
                            "id": "after-observation",
                            "role_id": "implementer",
                            "task_id": "implement",
                            "attempt": 2,
                            "disposition": "clarification",
                            "summary": "Known and unknown preserved",
                            "request": {
                                "id": "after-observation",
                                "kind": "product_definition",
                                "question": "Proceed?",
                                "context": "Controlled fixture",
                            },
                        },
                        "qa_report": None,
                    }
                )
            ),
        )

    c.provider.model().client._client._transport = httpx2.MockTransport(resumed)
    await c.continuity.execute(ticket["id"])
    receipt = await service.status(RUN, ticket["id"])
    assert receipt["status"] == "completed", (
        (await c.continuity.get(RUN, ticket["id"])).get("result"),
        c.store.run.data.get("error"),
    )
    assert len(turns) == 1 and len(calls) == 1 and len(c.journal.sandboxes.rows) == 11
    assert [r.data for r in c.journal.records.rows][: len(entries)] == original
    assert c.store.run.data["attempt"] == 2 and c.store.run.data["repair_budget"] == 2
    final = await c.continuity.get(RUN, ticket["id"])
    assert all(ticket[k] == final[k] for k in ["checkpoint", "binding", "admission_seal"])
    await c.continuity.execute(ticket["id"])
    assert len(turns) == 1
    from firefly_forge.invalid_read_dispatch_guard import assert_generic_dispatch_allowed

    c.store.execution_requests = c.requests
    await assert_generic_dispatch_allowed(c.store, c.journal, await c.store.get_run(RUN))


@pytest.mark.parametrize("continuity_case", [True], indirect=True)
async def test_original_read_custody_mutations_and_later_effects_remain_held(continuity_case, monkeypatch):
    from firefly_forge.agentic_checkpoint_invocation import runtime_contract
    from firefly_forge.journal import Journal, digest
    from firefly_forge.readonly_observation_reconstruction import prepare_observation

    c = continuity_case
    ticket, entries, calls = await failed_observation(c, monkeypatch)
    run, task, _ = await c.continuity.recoveries._native(RUN)

    def rechain(rows):
        values = copy.deepcopy(rows)
        remap = {}
        for index, e in enumerate(values):
            old = e["digest"]
            e["sequence"] = index + 1
            e["previous_hash"] = values[index - 1]["digest"] if index else "0" * 64
            if "intent_digest" in e["payload"] and e["payload"]["intent_digest"] in remap:
                e["payload"]["intent_digest"] = remap[e["payload"]["intent_digest"]]
            e["digest"] = digest({k: v for k, v in e.items() if k != "digest"})
            remap[old] = e["digest"]
        return values

    mutations = [
        lambda es: es[-1]["payload"].__setitem__("version", True),
        lambda es: es[-1]["payload"].__setitem__("reason", "authorized"),
        lambda es: es[-2]["payload"].__setitem__("arguments", {"query": {"limit": 9}}),
        lambda es: es[-2]["payload"].__setitem__("raw_arguments", "{}"),
        lambda es: es[-2]["payload"].__setitem__("checkpoint_digest", "f" * 64),
        lambda es: es[-2]["payload"].__setitem__("invocation_id", "f" * 32),
        lambda es: es[-2]["payload"].__setitem__("source_before", {}),
        lambda es: es[-2]["payload"]["authority"].__setitem__("actor", {}),
        lambda es: es[-3]["payload"].__setitem__("result", {"fabricated": "success"}),
        lambda es: es[-3]["payload"].__setitem__("parent_effects", [{"kind": "sandbox_receipt"}]),
        lambda es: es[-3].__setitem__("kind", "agentic_tool_unknown"),
        lambda es: es.append(copy.deepcopy(es[-2])),
        lambda es: es.append({**es[-1], "kind": "unknown_effect"}),
    ]
    for mutate in mutations:
        changed = copy.deepcopy(entries)
        mutate(changed)
        changed = rechain(changed)
        assert Journal.verify_chain(changed)
        with pytest.raises((ValueError, KeyError)):
            prepare_observation(run, task, changed, accepted_runtime=runtime_contract())
    assert len(calls) == 1 and [r.data for r in c.journal.records.rows] == entries


@pytest.mark.parametrize("continuity_case", [True], indirect=True)
async def test_resealed_observation_witness_cannot_change_sdk_metadata_or_any_usage(continuity_case, monkeypatch):
    import hashlib

    from firefly_forge.agentic_checkpoint_boundary import BoundaryCheckpoint, _checkpoint_payload, _encoded
    from firefly_forge.journal import Journal, digest
    from firefly_forge.readonly_observation_recoveries import settled_observation

    c = continuity_case
    ticket, _, _ = await failed_observation(c, monkeypatch)
    from readonly_observation_native_fixtures import enable_observation, observation_input

    enable_observation(c)
    candidate = await c.continuity.observations.candidate(RUN, ticket["id"])
    await c.continuity.observations.request(RUN, ticket["id"], "e" * 32, observation_input(candidate))
    entries = copy.deepcopy(await c.journal.entries(RUN))
    assert settled_observation(entries[-1], entries)
    mutations = {
        "input_tokens": lambda raw: raw["usage"].__setitem__("input_tokens", 0),
        "output_tokens": lambda raw: raw["usage"].__setitem__("output_tokens", 0),
        "cost": lambda raw: raw["usage"].__setitem__("cost", "0"),
        "details": lambda raw: raw["usage"].__setitem__("details", {"fabricated_counter": 42}),
        "model": lambda raw: raw["history"][-2].__setitem__("model_name", "fabricated-model"),
        "provider": lambda raw: raw["history"][-2].__setitem__("provider_name", "fabricated-provider"),
        "response_usage": lambda raw: raw["history"][-2]["usage"].__setitem__("input_tokens", 0),
        "response_metadata": lambda raw: raw["history"][-2].__setitem__("metadata", {"fabricated": True}),
        "return_metadata": lambda raw: raw["history"][-1]["parts"][0].__setitem__("metadata", {"fabricated": True}),
        "known_result_json_type": lambda raw: raw["history"][-1]["parts"][0]["content"].__setitem__("complete", 1),
        "request_history_json_type": lambda raw: raw["request_history"][-1]["parts"][0]["content"].__setitem__(
            "complete", 1
        ),
        "request_instructions": lambda raw: raw["history"][-1].__setitem__("instructions", "Fabricated instruction"),
    }
    accepted = []
    for name, mutate in mutations.items():
        rows = copy.deepcopy(entries)
        p = rows[-1]["payload"]
        checkpoint = BoundaryCheckpoint.decode(p["checkpoint"].encode(), p["checkpoint_digest"])
        raw = json.loads(checkpoint.canonical_payload)
        mutate(raw)
        if name != "request_history_json_type":
            raw["request_history"] = copy.deepcopy(raw["history"])
        encoded = _checkpoint_payload(_encoded(raw))
        p["checkpoint"] = encoded.decode()
        p["checkpoint_digest"] = hashlib.sha256(encoded).hexdigest()
        p["digest"] = digest({k: v for k, v in p.items() if k != "digest"})
        rows[-1]["digest"] = digest({k: v for k, v in rows[-1].items() if k != "digest"})
        assert Journal.verify_chain(rows)
        try:
            settled_observation(rows[-1], rows)
        except ValueError:
            continue
        accepted.append(name)
    assert accepted == []
    assert await c.journal.entries(RUN) == entries


@pytest.mark.parametrize("continuity_case", [True], indirect=True)
async def test_sdk_observation_projection_preserves_existing_cost_and_usage_details(continuity_case, monkeypatch):
    import hashlib

    from firefly_forge.agentic_checkpoint_boundary import _checkpoint_payload, _encoded
    from firefly_forge.agentic_checkpoint_invocation import runtime_contract
    from firefly_forge.readonly_observation_reconstruction import (
        PreparedObservation,
        observation_projection,
        prepare_observation,
        reconstruct_observation,
    )

    c = continuity_case
    _, entries, calls = await failed_observation(c, monkeypatch)
    run, task, _ = await c.continuity.recoveries._native(RUN)
    prepared = prepare_observation(run, task, entries, accepted_runtime=runtime_contract())
    data = prepared.data
    original = json.loads(prepared.checkpoint.canonical_payload)
    original["usage"].update(cost="1.25", details={"retained_reasoning": 7}, cache_read_tokens=2)
    encoded = _checkpoint_payload(_encoded(original))
    data["checkpoint"]["snapshot"] = encoded.decode()
    data["checkpoint"]["checkpoint_digest"] = hashlib.sha256(encoded).hexdigest()
    response = json.loads(data["response"])
    response["usage"].update(
        prompt_tokens_details={"cached_tokens": 2, "cache_write_tokens": 1},
        completion_tokens_details={"reasoning_tokens": 1, "audio_tokens": 2},
    )
    response["service_tier"] = "default"
    data["response"] = json.dumps(response)
    controlled = PreparedObservation(_encoded(data))
    derived = await reconstruct_observation(controlled)
    history, usage = observation_projection(controlled)
    assert derived.record.history[-2:] == history and derived.record.usage == usage
    assert usage["cost"] == "1.25" and usage["details"]["retained_reasoning"] == 7
    assert usage["details"]["reasoning_tokens"] == 1 and usage["details"]["audio_tokens"] == 2
    assert usage["cache_read_tokens"] == 4 and usage["cache_write_tokens"] == 1
    assert len(calls) == 1 and await c.journal.entries(RUN) == entries
