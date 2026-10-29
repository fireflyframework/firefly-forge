# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Controlled same-ticket handoffs; no PostgreSQL or provider acceptance claim."""

import copy
import json

import httpx2
import pytest
from test_agentic_checkpoint_activation_host import result
from test_bridge_completion_callbacks import CAPABILITY
from test_bridge_inflight_cancel import RUN
from test_invalid_read_native_protocol_host import admit

pytest_plugins = ["test_invalid_read_native_protocol_host"]


async def pending_after_write(c, monkeypatch):
    from types import MethodType

    from firefly_forge.agent_checkpoints import AgentCheckpoints

    async def budget(self, key):
        rows = [row for row in self.rows if row.request_id == key]
        return len(rows), sum(len(json.dumps(row.data).encode()) for row in rows)

    begin, rollback = c.manager.begin, c.manager.rollback

    async def begin_all(definition):
        unit = await begin(definition)
        unit.attributes["native_requests"] = copy.deepcopy(
            (c.requests.records.rows, c.requests.journal.rows, c.store.run.status)
        )
        return unit

    async def rollback_all(unit):
        await rollback(unit)
        c.requests.records.rows, c.requests.journal.rows, c.store.run.status = unit.attributes["native_requests"]

    monkeypatch.setattr(c.manager, "begin", begin_all)
    monkeypatch.setattr(c.manager, "rollback", rollback_all)
    c.requests.journal.custody_budget = MethodType(budget, c.requests.journal)
    ticket, _ = await admit(c)
    original = AgentCheckpoints._consume

    async def stop(self, run, intent, body, current, reader):
        if b"pending-read" in body:
            raise ValueError("Controlled ownership expiration before consumption")
        return await original(self, run, intent, body, current, reader)

    def transport(request):
        if request.method == "GET":
            return httpx2.Response(200, json={"completionRecovery": CAPABILITY})
        c.posts.append(request.content)
        response = result(tool=True)
        call = response["choices"][0]["message"]["tool_calls"][0]
        if len(c.posts) == 2:
            call.update(
                id="known-write",
                function={"name": "write_file", "arguments": '{"path":"existing.py","content":"VALUE = 2\\n"}'},
            )
        else:
            call.update(id="pending-read", function={"name": "read_file", "arguments": '{"path":"existing.py"}'})
        return httpx2.Response(200, json=response)

    c.provider.model().client._client._transport = httpx2.MockTransport(transport)
    with monkeypatch.context() as stopped:
        stopped.setattr(AgentCheckpoints, "_consume", stop)
        await c.continuity.execute(ticket["id"])
    ticket = await c.continuity.get(RUN, ticket["id"])
    assert ticket["status"] == "needs_reconciliation", ticket.get("result")
    entries = copy.deepcopy([row.data for row in c.journal.records.rows])
    assert [e["kind"] for e in entries[-3:]] == ["bridge_request_intent", "agent_checkpoint", "bridge_request_response"]
    assert (c.workspace / "existing.py").read_text() == "VALUE = 2\n"
    return ticket, entries


@pytest.mark.parametrize("continuity_case", [True], indirect=True)
async def test_pending_written_checkpoint_is_discoverable_but_default_off(continuity_case, monkeypatch):
    c = continuity_case
    ticket, entries = await pending_after_write(c, monkeypatch)
    assert hasattr(c.continuity, "checkpoints"), "Missing generic same-ticket checkpoint continuation"
    value = await c.continuity.checkpoints.candidate(RUN, ticket["id"])
    assert value["admission"] == "HOLD" and "checkpoint_continuation_disabled" in value["holds"]
    assert value["checkpoint_record_digest"] == entries[-2]["digest"]
    assert value["response_record_digest"] == entries[-1]["digest"]
    assert value["execution_authorized"] is False
    assert len(c.posts) == 3 and len(c.requests.records.rows) == 1
    assert [row.data for row in c.journal.records.rows] == entries


@pytest.mark.parametrize("continuity_case", [True], indirect=True)
async def test_same_ticket_consumes_retained_response_once_without_replaying_write(continuity_case, monkeypatch):
    c = continuity_case
    ticket, entries = await pending_after_write(c, monkeypatch)
    assert hasattr(c.continuity, "checkpoints"), "Missing generic same-ticket checkpoint continuation"
    from firefly_forge.checkpoint_continuation_models import CheckpointContinuationInputV1
    from firefly_forge.invalid_read_lease import ContinuityLeasePolicyV1, write_policy

    write_policy(
        c.store.configuration,
        ContinuityLeasePolicyV1(version=1, enabled=True, allow_checkpoint_continuation=True, max_window_seconds=180),
    )
    service = c.continuity.checkpoints
    candidate = await service.candidate(RUN, ticket["id"])
    assert candidate["admission"] == "READY", candidate["holds"]
    body = CheckpointContinuationInputV1(
        version=1,
        candidate_digest=candidate["digest"],
        valid_for_seconds=180,
        acknowledge_retained_response_consumed_once=True,
        acknowledge_no_completed_effect_replay=True,
        acknowledge_same_request_attempt_and_budgets=True,
        acknowledge_finite_ownership_lease=True,
    )
    saved = await service.request(RUN, ticket["id"], "f" * 32, body)
    assert saved["status"] == "queued" and await service.request(RUN, ticket["id"], "f" * 32, body) == saved
    turns = []
    failures = []
    authority = service._authority

    async def observed_authority(ticket, record, **kwargs):
        try:
            return await authority(ticket, record, **kwargs)
        except Exception:
            from firefly_forge.agentic_checkpoint_invocation import invocation_cut
            from firefly_forge.invalid_read_lease import policy_snapshot
            from firefly_forge.invalid_read_tickets import protected_scope

            run, task, _ = await service.base._native(RUN)
            failures.append(
                repr(
                    {
                        "protected": protected_scope(run) == ticket["protected_scope_digest"],
                        "invocation": invocation_cut(run) == ticket["binding"]["run_cut"],
                        "policy": policy_snapshot(c.store.configuration) == record["payload"]["lease_policy"],
                        "task": task,
                        "original_task": record["payload"]["task"],
                        "run_status": run["status"],
                    }
                )
            )
            raise

    monkeypatch.setattr(service, "_authority", observed_authority)
    original_execute = service.execute

    async def observed_execute(*args):
        try:
            return await original_execute(*args)
        except Exception:
            import traceback

            failures.append(traceback.format_exc())
            raise

    monkeypatch.setattr(service, "execute", observed_execute)
    from dataclasses import replace

    from firefly_forge.agent_checkpoints import AgentCheckpoints
    from firefly_forge.bridge_completions import BridgeCompletions

    old_scope = BridgeCompletions.scope

    def scope(self, *args, **kwargs):
        value = old_scope(self, *args, **kwargs)
        old_response = value.response

        async def response(*args):
            try:
                return await old_response(*args)
            except Exception as error:
                failures.append("response:" + repr(error))
                raise

        return replace(value, response=response)

    monkeypatch.setattr(BridgeCompletions, "scope", scope)
    restored_consume = AgentCheckpoints.consume_restored

    async def observed_restored(self, *args, **kwargs):
        try:
            return await restored_consume(self, *args, **kwargs)
        except Exception as error:
            failures.append("restored:" + repr(error))
            raise

    monkeypatch.setattr(AgentCheckpoints, "consume_restored", observed_restored)
    consume = AgentCheckpoints._consume

    async def observed(self, *args, **kwargs):
        try:
            return await consume(self, *args, **kwargs)
        except Exception as error:
            failures.append(repr(error))
            raise

    monkeypatch.setattr(AgentCheckpoints, "_consume", observed)

    def resumed(request):
        if request.method == "GET":
            return httpx2.Response(200, json={"completionRecovery": CAPABILITY})
        turns.append(json.loads(request.content))
        c.posts.append(request.content)
        return httpx2.Response(
            200,
            json=result(
                json.dumps(
                    {
                        "summary": "Retained checkpoint continued",
                        "worker_feedback": {
                            "id": "generic-handoff",
                            "role_id": "implementer",
                            "task_id": "implement",
                            "attempt": 2,
                            "disposition": "clarification",
                            "summary": "Prior write preserved",
                            "request": {
                                "id": "generic-handoff",
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
    current = await c.continuity.get(RUN, ticket["id"])
    assert current["status"] == "completed", json.dumps(
        [*failures, c.store.run.data.get("error"), len(turns), [r.kind for r in c.journal.records.rows[-8:]]]
    )
    assert len(turns) == 1 and len(c.requests.records.rows) == 1
    assert (c.workspace / "existing.py").read_text() == "VALUE = 2\n"
    assert [row.data for row in c.journal.records.rows][: len(entries)] == entries
    for key in ["admission_seal", "binding", "checkpoint", "lease", "request", "task_key"]:
        assert current[key] == ticket[key]
    consumes = [
        row.data
        for row in c.journal.records.rows
        if row.kind == "agent_consume" and row.idempotency_key == entries[-1]["idempotency_key"]
    ]
    assert len(consumes) == 1 and consumes[0]["payload"]["version"] == 2
    await c.continuity.execute(ticket["id"])
    assert len(turns) == 1
    from firefly_forge.invalid_read_dispatch_guard import assert_generic_dispatch_allowed

    c.store.execution_requests = c.requests
    await assert_generic_dispatch_allowed(c.store, c.journal, await c.store.get_run(RUN))


@pytest.mark.parametrize("continuity_case", [True], indirect=True)
async def test_frontier_rejects_reset_usage_and_omitted_known_write(continuity_case, monkeypatch):
    import hashlib

    from firefly_forge.agentic_checkpoint_boundary import BoundaryCheckpoint, _checkpoint_payload, _encoded
    from firefly_forge.checkpoint_continuation_proof import pending_checkpoint
    from firefly_forge.journal import digest

    c = continuity_case
    ticket, entries = await pending_after_write(c, monkeypatch)
    run, task, _ = await c.continuity.recoveries._native(RUN)
    assert pending_checkpoint(run, task, entries)["checkpoint"] == entries[-2]

    def chain(values):
        values = copy.deepcopy(values)
        for n, e in enumerate(values, 1):
            e["sequence"] = n
            e["previous_hash"] = values[n - 2]["digest"] if n > 1 else "0" * 64
            e["digest"] = digest({k: v for k, v in e.items() if k != "digest"})
        return values

    reset = copy.deepcopy(entries)
    p = reset[-2]["payload"]
    native = BoundaryCheckpoint.decode(p["snapshot"].encode(), p["checkpoint_digest"])
    value = native.record.model_dump(mode="json")
    value["usage"]["requests"] = 0
    body = _checkpoint_payload(_encoded(value))
    p["snapshot"] = body.decode()
    p["checkpoint_digest"] = hashlib.sha256(body).hexdigest()
    with pytest.raises(ValueError):
        pending_checkpoint(run, task, chain(reset))
    omitted = [e for e in entries if e["kind"] not in {"agentic_tool_intent", "agentic_tool_result"}]
    with pytest.raises(ValueError):
        pending_checkpoint(run, task, chain(omitted))


@pytest.mark.parametrize("continuity_case", [True], indirect=True)
async def test_frontier_rejects_token_cost_details_limits_and_written_state_drift(continuity_case, monkeypatch):
    import hashlib

    from firefly_forge.agentic_checkpoint_boundary import BoundaryCheckpoint, _checkpoint_payload, _encoded
    from firefly_forge.checkpoint_continuation_proof import pending_checkpoint
    from firefly_forge.journal import digest

    c = continuity_case
    _, entries = await pending_after_write(c, monkeypatch)
    run, task, _ = await c.continuity.recoveries._native(RUN)
    original = entries[-2]["payload"]
    native = BoundaryCheckpoint.decode(original["snapshot"].encode(), original["checkpoint_digest"])
    for field, value in [("input_tokens", 0), ("output_tokens", 0), ("cost", "1.25"), ("details", {"invented": 1})]:
        rows = copy.deepcopy(entries)
        p = rows[-2]["payload"]
        data = native.record.model_dump(mode="json")
        data["usage"][field] = value
        body = _checkpoint_payload(_encoded(data))
        p["snapshot"] = body.decode()
        p["checkpoint_digest"] = hashlib.sha256(body).hexdigest()
        for n, e in enumerate(rows, 1):
            e["sequence"] = n
            e["previous_hash"] = rows[n - 2]["digest"] if n > 1 else "0" * 64
            e["digest"] = digest({k: v for k, v in e.items() if k != "digest"})
        with pytest.raises(ValueError, match="usage|consumption"):
            pending_checkpoint(run, task, rows)


def continuation_input(candidate):
    from firefly_forge.checkpoint_continuation_models import CheckpointContinuationInputV1

    return CheckpointContinuationInputV1(
        version=1,
        candidate_digest=candidate["digest"],
        valid_for_seconds=180,
        acknowledge_retained_response_consumed_once=True,
        acknowledge_no_completed_effect_replay=True,
        acknowledge_same_request_attempt_and_budgets=True,
        acknowledge_finite_ownership_lease=True,
    )


def enable_checkpoint(c):
    from firefly_forge.invalid_read_lease import ContinuityLeasePolicyV1, write_policy

    write_policy(
        c.store.configuration,
        ContinuityLeasePolicyV1(version=1, enabled=True, allow_checkpoint_continuation=True, max_window_seconds=180),
    )


@pytest.mark.parametrize("continuity_case", [True], indirect=True)
async def test_concurrent_identical_actions_have_one_witness_and_no_dispatch(continuity_case, monkeypatch):
    import asyncio

    c = continuity_case
    ticket, entries = await pending_after_write(c, monkeypatch)
    enable_checkpoint(c)
    service = c.continuity.checkpoints
    body = continuation_input(await service.candidate(RUN, ticket["id"]))
    responses = await asyncio.gather(*(service.request(RUN, ticket["id"], "f" * 32, body) for _ in range(4)))
    assert all(r == responses[0] for r in responses)
    assert len([r for r in c.journal.records.rows if r.kind == "checkpoint_continuation"]) == 1
    assert len(c.posts) == 3 and len(c.requests.records.rows) == 1
    assert [r.data for r in c.journal.records.rows][: len(entries)] == entries
    with pytest.raises(ValueError):
        await service.request(RUN, ticket["id"], "e" * 32, body)


@pytest.mark.parametrize("continuity_case", [True], indirect=True)
@pytest.mark.parametrize("revocation", ["policy", "expired", "source"])
async def test_preclaim_authority_drift_never_exposes_response(continuity_case, monkeypatch, revocation):
    from types import SimpleNamespace

    from firefly_forge import checkpoint_continuations as module
    from firefly_forge.invalid_read_lease import ContinuityLeasePolicyV1, write_policy

    c = continuity_case
    ticket, entries = await pending_after_write(c, monkeypatch)
    enable_checkpoint(c)
    service = c.continuity.checkpoints
    body = continuation_input(await service.candidate(RUN, ticket["id"]))
    saved = await service.request(RUN, ticket["id"], "f" * 32, body)
    if revocation == "policy":
        write_policy(c.store.configuration, ContinuityLeasePolicyV1(version=1))
    elif revocation == "expired":
        monkeypatch.setattr(module, "time", SimpleNamespace(time=lambda: (saved["lease"]["expires_at_ms"] + 1) / 1000))
    else:
        (c.workspace / "existing.py").write_text("EXTERNAL = True\n")
    await c.continuity.execute(ticket["id"])
    current = await c.continuity.get(RUN, ticket["id"])
    assert current["status"] == "failed" and current["generation"] == ticket["generation"]
    assert len(c.posts) == 3 and not any(
        r.kind == "agent_consume" and r.idempotency_key == entries[-1]["idempotency_key"]
        for r in c.journal.records.rows
    )


@pytest.mark.parametrize("continuity_case", [True], indirect=True)
async def test_admission_fault_rolls_back_witness_and_same_ticket_update(continuity_case, monkeypatch):
    c = continuity_case
    ticket, entries = await pending_after_write(c, monkeypatch)
    enable_checkpoint(c)
    service = c.continuity.checkpoints
    body = continuation_input(await service.candidate(RUN, ticket["id"]))
    events = copy.deepcopy(await c.requests.events(ticket["id"]))
    event = c.requests._event

    async def failed(row, kind, evidence):
        if kind == "checkpoint_continuation_admitted":
            raise ValueError("Controlled transaction rollback")
        return await event(row, kind, evidence)

    monkeypatch.setattr(c.requests, "_event", failed)
    with pytest.raises(ValueError):
        await service.request(RUN, ticket["id"], "f" * 32, body)
    assert await c.continuity.get(RUN, ticket["id"]) == ticket
    assert [r.data for r in c.journal.records.rows] == entries and await c.requests.events(ticket["id"]) == events
    assert len(c.posts) == 3


@pytest.mark.parametrize("continuity_case", [True], indirect=True)
async def test_fault_after_durable_consume_cannot_reexpose_or_requeue_response(continuity_case, monkeypatch):
    from firefly_forge.checkpoint_continuations import CheckpointContinuationExecution

    c = continuity_case
    ticket, entries = await pending_after_write(c, monkeypatch)
    enable_checkpoint(c)
    service = c.continuity.checkpoints
    body = continuation_input(await service.candidate(RUN, ticket["id"]))
    await service.request(RUN, ticket["id"], "f" * 32, body)
    consume = CheckpointContinuationExecution.consume

    async def lost(self, *args):
        await consume(self, *args)
        raise ValueError("Controlled loss after durable consume")

    monkeypatch.setattr(CheckpointContinuationExecution, "consume", lost)
    await c.continuity.execute(ticket["id"])
    current = await c.continuity.get(RUN, ticket["id"])
    assert current["status"] == "needs_reconciliation" and current["generation"] != ticket["generation"]
    assert len(c.posts) == 3
    assert (
        len(
            [
                r
                for r in c.journal.records.rows
                if r.kind == "agent_consume" and r.idempotency_key == entries[-1]["idempotency_key"]
            ]
        )
        == 1
    )
    with pytest.raises(ValueError):
        await service.candidate(RUN, ticket["id"])
    await c.continuity.execute(ticket["id"])
    assert len(c.posts) == 3
    assert (await service.request(RUN, ticket["id"], "f" * 32, body))["status"] == "needs_reconciliation"


@pytest.mark.parametrize("continuity_case", [True], indirect=True)
async def test_later_pending_frontier_can_receive_another_distinct_finite_handoff(continuity_case, monkeypatch):
    from firefly_forge.agent_checkpoints import AgentCheckpoints

    c = continuity_case
    ticket, entries = await pending_after_write(c, monkeypatch)
    enable_checkpoint(c)
    service = c.continuity.checkpoints
    body = continuation_input(await service.candidate(RUN, ticket["id"]))
    await service.request(RUN, ticket["id"], "f" * 32, body)
    original = AgentCheckpoints._consume

    async def stop(self, run, intent, raw, current, reader):
        if b"second-pending-read" in raw:
            raise ValueError("Controlled later ownership expiration")
        return await original(self, run, intent, raw, current, reader)

    def next_pending(request):
        if request.method == "GET":
            return httpx2.Response(200, json={"completionRecovery": CAPABILITY})
        c.posts.append(request.content)
        response = result(tool=True)
        response["choices"][0]["message"]["tool_calls"][0].update(
            id="second-pending-read", function={"name": "read_file", "arguments": '{"path":"existing.py"}'}
        )
        return httpx2.Response(200, json=response)

    c.provider.model().client._client._transport = httpx2.MockTransport(next_pending)
    with monkeypatch.context() as cut:
        cut.setattr(AgentCheckpoints, "_consume", stop)
        await c.continuity.execute(ticket["id"])
    after = await c.continuity.get(RUN, ticket["id"])
    assert after["status"] == "needs_reconciliation" and len(c.posts) == 4
    next_candidate = await service.candidate(RUN, ticket["id"])
    assert next_candidate["admission"] == "READY"
    second = await service.request(RUN, ticket["id"], "e" * 32, continuation_input(next_candidate))
    assert second["status"] == "queued" and second["action_id"] == "e" * 32
    assert len([r for r in c.journal.records.rows if r.kind == "checkpoint_continuation"]) == 2
    assert len(c.requests.records.rows) == 1 and (c.workspace / "existing.py").read_text() == "VALUE = 2\n"
    from firefly_forge import checkpoint_continuations as lifecycle
    from firefly_forge.known_worker_completion_proof import _unsettled

    counts = {}
    validate = lifecycle.validate_witness

    def measured(entry, rows):
        counts[entry["digest"]] = counts.get(entry["digest"], 0) + 1
        return validate(entry, rows)

    monkeypatch.setattr(lifecycle, "validate_witness", measured)
    rows = copy.deepcopy([r.data for r in c.journal.records.rows])
    _unsettled(rows, RUN, pending=True)
    assert len(counts) == 2 and set(counts.values()) == {1}
    _unsettled(rows, RUN, pending=True)
    assert set(counts.values()) == {2}


@pytest.mark.parametrize("continuity_case", [True], indirect=True)
async def test_frontier_rejects_prior_model_identity_drift(continuity_case, monkeypatch):
    import hashlib

    from firefly_forge.agentic_checkpoint_boundary import BoundaryCheckpoint, _checkpoint_payload, _encoded
    from firefly_forge.checkpoint_continuation_proof import pending_checkpoint
    from firefly_forge.journal import digest

    c = continuity_case
    _, entries = await pending_after_write(c, monkeypatch)
    run, task, _ = await c.continuity.recoveries._native(RUN)
    rows = copy.deepcopy(entries)
    p = rows[-2]["payload"]
    native = BoundaryCheckpoint.decode(p["snapshot"].encode(), p["checkpoint_digest"])
    data = native.record.model_dump(mode="json")
    response = next(m for m in reversed(data["history"]) if m["kind"] == "response")
    response["model_name"] = "changed-model"
    data["request_history"] = copy.deepcopy(data["history"])
    body = _checkpoint_payload(_encoded(data))
    p["snapshot"] = body.decode()
    p["checkpoint_digest"] = hashlib.sha256(body).hexdigest()
    for n, e in enumerate(rows, 1):
        e["sequence"] = n
        e["previous_hash"] = rows[n - 2]["digest"] if n > 1 else "0" * 64
        e["digest"] = digest({k: v for k, v in e.items() if k != "digest"})
    with pytest.raises(ValueError):
        pending_checkpoint(run, task, rows)


@pytest.mark.parametrize("continuity_case", [True], indirect=True)
async def test_spent_readonly_action_cannot_name_a_new_checkpoint_handoff(continuity_case, monkeypatch):
    from test_readonly_observation_recovery_host import failed_observation

    from firefly_forge.agent_checkpoints import AgentCheckpoints
    from firefly_forge.invalid_read_lease import ContinuityLeasePolicyV1, write_policy
    from firefly_forge.readonly_observation_models import ReadonlyObservationRecoveryInputV1

    c = continuity_case
    ticket, _, _ = await failed_observation(c, monkeypatch)
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
    candidate = await c.continuity.observations.candidate(RUN, ticket["id"])
    body = ReadonlyObservationRecoveryInputV1(
        version=1,
        candidate_digest=candidate["digest"],
        valid_for_seconds=180,
        acknowledge_unavailable_observation=True,
        acknowledge_no_original_callback_replay=True,
        acknowledge_same_request_attempt_and_budgets=True,
        acknowledge_finite_ownership_lease=True,
    )
    await c.continuity.observations.request(RUN, ticket["id"], "e" * 32, body)
    original = AgentCheckpoints._consume

    async def stop(self, run, intent, raw, current, reader):
        if b"pending-read" in raw:
            raise ValueError("Controlled stop before consumption")
        return await original(self, run, intent, raw, current, reader)

    def transport(request):
        if request.method == "GET":
            return httpx2.Response(200, json={"completionRecovery": CAPABILITY})
        c.posts.append(request.content)
        response = result(tool=True)
        call = response["choices"][0]["message"]["tool_calls"][0]
        if len(c.posts) == 4:
            call.update(
                id="known-write",
                function={"name": "write_file", "arguments": '{"path":"existing.py","content":"VALUE = 2\\n"}'},
            )
        else:
            call.update(id="pending-read", function={"name": "read_file", "arguments": '{"path":"existing.py"}'})
        return httpx2.Response(200, json=response)

    c.provider.model().client._client._transport = httpx2.MockTransport(transport)
    with monkeypatch.context() as cut:
        cut.setattr(AgentCheckpoints, "_consume", stop)
        await c.continuity.execute(ticket["id"])
    ticket = await c.continuity.get(RUN, ticket["id"])
    assert ticket["status"] == "needs_reconciliation" and len(c.posts) == 5
    enable_checkpoint(c)
    service = c.continuity.checkpoints
    new_body = continuation_input(await service.candidate(RUN, ticket["id"]))
    with pytest.raises(ValueError):
        await service.request(RUN, ticket["id"], "e" * 32, new_body)
    saved = await service.request(RUN, ticket["id"], "f" * 32, new_body)
    assert saved["status"] == "queued" and len(c.posts) == 5


@pytest.mark.parametrize("continuity_case", [True], indirect=True)
async def test_normal_report_reaches_native_source_receipt_on_same_ticket(continuity_case, monkeypatch):
    from test_agentic_checkpoint_activation_host import controlled_source_snapshot

    c = continuity_case
    ticket, entries = await pending_after_write(c, monkeypatch)
    enable_checkpoint(c)
    service = c.continuity.checkpoints
    await service.request(RUN, ticket["id"], "f" * 32, continuation_input(await service.candidate(RUN, ticket["id"])))

    async def snapshot(*args, **kwargs):
        return ["existing.py"], controlled_source_snapshot("Controlled retained source patch"), "c" * 40

    monkeypatch.setattr(c.engine, "_snapshot", snapshot)
    turns = []

    def transport(request):
        if request.method == "GET":
            return httpx2.Response(200, json={"completionRecovery": CAPABILITY})
        c.posts.append(request.content)
        turns.append(request.content)
        return httpx2.Response(
            200,
            json=result(
                json.dumps(
                    {"summary": "Normal retained implementation report", "worker_feedback": None, "qa_report": None}
                )
            ),
        )

    c.provider.model().client._client._transport = httpx2.MockTransport(transport)
    await c.continuity.execute(ticket["id"])
    receipts = [r.data for r in c.journal.records.rows if r.kind == "terminal_completion_receipt"]
    assert len(receipts) == 1, (
        c.store.run.data.get("error"),
        (await c.continuity.get(RUN, ticket["id"])).get("result"),
    )
    assert receipts[0]["payload"]["producer"] == "native_implementation_receipt"
    assert len(turns) == 1 and (c.workspace / "existing.py").read_text() == "VALUE = 2\n"
    task = next(r for r in c.journal.tasks.rows if r.task_id == "implement")
    assert task.status == "done" and task.attempt == 2
    assert not c.store.run.data["bridge_completion_hold"]
