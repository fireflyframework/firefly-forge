# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Same-ticket consumed final results never dispatch another worker inference."""

import asyncio
import copy
import json

import httpx2
import pytest
from test_agentic_checkpoint_activation_host import result
from test_bridge_completion_callbacks import CAPABILITY
from test_bridge_inflight_cancel import RUN
from test_invalid_read_native_protocol_host import admit

pytest_plugins = ["test_invalid_read_native_protocol_host"]


async def consumed_blocker(c, monkeypatch, *, feedback=True):
    # Install the existing transaction-correct native request fixture without creating a handoff.
    from types import MethodType

    async def budget(self, key):
        rows = [row for row in self.rows if row.request_id == key]
        return len(rows), sum(len(json.dumps(row.data).encode()) for row in rows)

    c.requests.journal.custody_budget = MethodType(budget, c.requests.journal)
    begin, rollback = c.manager.begin, c.manager.rollback

    async def begin_all(definition):
        unit = await begin(definition)
        unit.attributes["final_native_requests"] = copy.deepcopy(
            (c.requests.records.rows, c.requests.journal.rows, c.store.run.status)
        )
        return unit

    async def rollback_all(unit):
        await rollback(unit)
        c.requests.records.rows, c.requests.journal.rows, c.store.run.status = unit.attributes["final_native_requests"]

    monkeypatch.setattr(c.manager, "begin", begin_all)
    monkeypatch.setattr(c.manager, "rollback", rollback_all)
    ticket, _ = await admit(c)

    def transport(request):
        if request.method == "GET":
            return httpx2.Response(200, json={"completionRecovery": CAPABILITY})
        c.posts.append(request.content)
        if len(c.posts) == 2:
            response = result(tool=True)
            response["choices"][0]["message"]["tool_calls"][0].update(
                id="known-written-result",
                function={"name": "write_file", "arguments": '{"path":"existing.py","content":"VALUE = 2\\n"}'},
            )
        else:
            response = result(
                json.dumps(
                    dict(
                        summary="Retained source needs its approved checks",
                        qa_report=None,
                        worker_feedback=dict(
                            id="known-final-verification",
                            role_id="implementer",
                            task_id="implement",
                            attempt=2,
                            disposition="blocked",
                            summary="Run the existing verification plan",
                            request=dict(
                                id="known-final-verification",
                                kind="verification",
                                question="Can the already approved checks run?",
                                context="No coding, tool grant or new command is requested",
                            ),
                        ),
                    )
                )
            )
        return httpx2.Response(200, json=response)

    if not feedback:
        original_transport = transport

        def transport(request):
            response = original_transport(request)
            if request.method == "POST" and len(c.posts) == 3:
                body = response.json()
                report = json.loads(body["choices"][0]["message"]["content"])
                report["worker_feedback"] = None
                body["choices"][0]["message"]["content"] = json.dumps(report)
                return httpx2.Response(200, json=body)
            return response

    async def stop(*args, **kwargs):
        raise ValueError("Controlled authority loss before native final acceptance")

    c.provider.model().client._client._transport = httpx2.MockTransport(transport)
    with monkeypatch.context() as held:
        held.setattr(c.engine, "_worker_feedback" if feedback else "_snapshot", stop)
        await c.continuity.execute(ticket["id"])
    current = await c.store.get_run(RUN)
    ticket = await c.continuity.get(RUN, ticket["id"])
    assert current["status"] == "failed" and current["implementation_rounds"] == 2
    assert current["implementation_pending"] is False and ticket["status"] == "needs_reconciliation"
    assert len(c.posts) == 3 and (c.workspace / "existing.py").read_text() == "VALUE = 2\n"
    assert c.journal.records.rows[-1].kind == "agent_consume"
    return ticket


async def reviewed_action(c, ticket):
    from firefly_forge.final_outcome_models import FinalOutcomeContinuationInputV1
    from firefly_forge.invalid_read_lease import ContinuityLeasePolicyV1, write_policy

    write_policy(
        c.store.configuration,
        ContinuityLeasePolicyV1(version=1, enabled=True, allow_final_outcome_continuation=True, max_window_seconds=180),
    )
    candidate = await c.continuity.final_outcomes.candidate(RUN, ticket["id"])
    return FinalOutcomeContinuationInputV1(
        version=1,
        candidate_digest=candidate["digest"],
        valid_for_seconds=180,
        acknowledge_already_consumed_final_output=True,
        acknowledge_no_completed_effect_replay=True,
        acknowledge_same_request_attempt_and_budgets=True,
        acknowledge_finite_ownership_lease=True,
    )


@pytest.mark.parametrize("continuity_case", [True], indirect=True)
async def test_consumed_final_candidate_is_read_only_and_disabled_by_default(continuity_case, monkeypatch):
    c = continuity_case
    ticket = await consumed_blocker(c, monkeypatch)
    before = copy.deepcopy([row.data for row in c.journal.records.rows])
    assert hasattr(c.continuity, "final_outcomes"), "Missing same-ticket consumed-final continuation"
    candidate = await c.continuity.final_outcomes.candidate(RUN, ticket["id"])
    assert candidate["admission"] == "HOLD" and "final_outcome_continuation_disabled" in candidate["holds"]
    assert candidate["consume_record_digest"] == before[-1]["digest"]
    assert candidate["execution_authorized"] is False
    assert [row.data for row in c.journal.records.rows] == before and len(c.posts) == 3


@pytest.mark.parametrize("continuity_case", [True], indirect=True)
async def test_same_ticket_accepts_exact_blocker_without_recharge_or_original_callbacks(continuity_case, monkeypatch):
    c = continuity_case
    ticket = await consumed_blocker(c, monkeypatch)
    assert hasattr(c.continuity, "final_outcomes"), "Missing same-ticket consumed-final continuation"
    from firefly_forge.final_outcome_models import FinalOutcomeContinuationInputV1
    from firefly_forge.invalid_read_lease import ContinuityLeasePolicyV1, write_policy

    write_policy(
        c.store.configuration,
        ContinuityLeasePolicyV1(version=1, enabled=True, allow_final_outcome_continuation=True, max_window_seconds=180),
    )
    service = c.continuity.final_outcomes
    candidate = await service.candidate(RUN, ticket["id"])
    assert candidate["admission"] == "READY", candidate["holds"]
    before_run = await c.store.get_run(RUN)
    prefix = copy.deepcopy([row.data for row in c.journal.records.rows])
    body = FinalOutcomeContinuationInputV1(
        version=1,
        candidate_digest=candidate["digest"],
        valid_for_seconds=180,
        acknowledge_already_consumed_final_output=True,
        acknowledge_no_completed_effect_replay=True,
        acknowledge_same_request_attempt_and_budgets=True,
        acknowledge_finite_ownership_lease=True,
    )
    saved = await service.request(RUN, ticket["id"], "e" * 32, body)
    assert saved["status"] == "queued" and await service.request(RUN, ticket["id"], "e" * 32, body) == saved

    async def forbidden(*args, **kwargs):
        raise AssertionError("Known final continuation cannot dispatch a provider or repeat consumption")

    from firefly_forge.agent_checkpoints import AgentCheckpoints

    monkeypatch.setattr(c.provider, "execute", forbidden)
    monkeypatch.setattr(AgentCheckpoints, "_consume", forbidden)
    failures = []
    execute = service.execute

    async def observed_execute(ticket):
        try:
            return await execute(ticket)
        except Exception as error:
            import traceback

            failures.append("".join(traceback.format_exception(error)))
            raise

    monkeypatch.setattr(service, "execute", observed_execute)
    await c.continuity.execute(ticket["id"])
    current = await c.store.get_run(RUN)
    observed = await service.status(RUN, ticket["id"])
    assert observed["status"] == "completed" and current["status"] == "paused", {
        "worker_failure": failures,
        "engine_error": current.get("error"),
    }
    assert current["worker_feedback"]["disposition"] == "blocked"
    assert current["worker_request"]["kind"] == "verification"
    assert current["implementation_rounds"] == before_run["implementation_rounds"] == 2
    assert current["usage"] == before_run["usage"] and current["attempt"] == before_run["attempt"]
    entries = [row.data for row in c.journal.records.rows]
    assert entries[: len(prefix)] == prefix
    assert sum(e["kind"] == "agent_consume" for e in entries) == sum(e["kind"] == "agent_consume" for e in prefix)
    assert any(e["kind"] == "terminal_completion_receipt" for e in entries)
    assert not current["bridge_completion_hold"] and len(c.posts) == 3
    assert len(c.requests.records.rows) == 1 and (c.workspace / "existing.py").read_text() == "VALUE = 2\n"


@pytest.mark.parametrize("continuity_case", [True], indirect=True)
async def test_final_admission_rolls_back_request_event_and_witness_together(continuity_case, monkeypatch):
    c = continuity_case
    ticket = await consumed_blocker(c, monkeypatch)
    body = await reviewed_action(c, ticket)

    def custody():
        def rows(values):
            return [{k: v for k, v in vars(row).items() if k != "_sa_instance_state"} for row in values]

        return copy.deepcopy(
            (
                c.store.run.data,
                [r.data for r in c.journal.records.rows],
                rows(c.requests.records.rows),
                rows(c.requests.journal.rows),
            )
        )

    before = custody()
    save = c.requests.records.save

    async def fail(row):
        await save(row)
        if row.status == "queued":
            raise RuntimeError("Controlled final action insert failure")

    monkeypatch.setattr(c.requests.records, "save", fail)
    with pytest.raises(RuntimeError, match="insert failure"):
        await c.continuity.final_outcomes.request(RUN, ticket["id"], "e" * 32, body)
    assert custody() == before


@pytest.mark.parametrize("continuity_case", [True], indirect=True)
async def test_concurrent_identical_final_actions_return_one_saved_receipt(continuity_case, monkeypatch):
    c = continuity_case
    ticket = await consumed_blocker(c, monkeypatch)
    body = await reviewed_action(c, ticket)
    service = c.continuity.final_outcomes
    prepare = service._prepare
    gate = asyncio.Event()
    arrived = 0

    async def barrier(*args):
        nonlocal arrived
        result = await prepare(*args)
        arrived += 1
        if arrived == 2:
            gate.set()
        await gate.wait()
        return result

    monkeypatch.setattr(service, "_prepare", barrier)
    results = await asyncio.gather(*(service.request(RUN, ticket["id"], "e" * 32, body) for _ in range(2)))
    assert results[0] == results[1]
    assert sum(r.kind == "final_outcome_continuation" for r in c.journal.records.rows) == 1
    assert len(c.posts) == 3 and len(c.requests.records.rows) == 1


@pytest.mark.parametrize("continuity_case", [True], indirect=True)
async def test_revoked_final_policy_before_claim_cannot_accept_or_reconsume(continuity_case, monkeypatch):
    from firefly_forge.invalid_read_lease import ContinuityLeasePolicyV1, write_policy

    c = continuity_case
    ticket = await consumed_blocker(c, monkeypatch)
    body = await reviewed_action(c, ticket)
    await c.continuity.final_outcomes.request(RUN, ticket["id"], "e" * 32, body)
    prefix = copy.deepcopy([r.data for r in c.journal.records.rows])
    write_policy(c.store.configuration, ContinuityLeasePolicyV1(version=1, enabled=True))
    await c.continuity.execute(ticket["id"])
    assert (await c.requests.get(ticket["id"]))["status"] == "failed"
    assert [r.data for r in c.journal.records.rows] == prefix and len(c.posts) == 3


@pytest.mark.parametrize("continuity_case", [True], indirect=True)
async def test_final_admission_window_does_not_become_a_claimed_work_deadline(continuity_case, monkeypatch):
    from types import SimpleNamespace

    from firefly_forge import final_outcome_continuations as module

    c = continuity_case
    ticket = await consumed_blocker(c, monkeypatch)
    body = await reviewed_action(c, ticket)
    saved = await c.continuity.final_outcomes.request(RUN, ticket["id"], "e" * 32, body)
    guard = c.continuity.final_outcomes.guard

    async def beyond_admission(*args):
        monkeypatch.setattr(module, "time", SimpleNamespace(time=lambda: saved["lease"]["expires_at_ms"] / 1000 + 1))
        await guard(*args)

    monkeypatch.setattr(c.continuity.final_outcomes, "guard", beyond_admission)
    await c.continuity.execute(ticket["id"])
    assert (await c.requests.get(ticket["id"]))["status"] == "completed"
    assert (await c.store.get_run(RUN))["status"] == "paused" and len(c.posts) == 3


@pytest.mark.parametrize("continuity_case", [True], indirect=True)
async def test_normal_final_receipt_precedes_verification_and_releases_restoration(continuity_case, monkeypatch):
    from test_agentic_checkpoint_activation_host import controlled_source_snapshot

    c = continuity_case
    c.store.run.data["commands"] = [{"argv": ["approved-final-check"], "timeout": 2}]
    c.store.run.data["verification"] = []
    ticket = await consumed_blocker(c, monkeypatch, feedback=False)
    body = await reviewed_action(c, ticket)
    await c.continuity.final_outcomes.request(RUN, ticket["id"], "e" * 32, body)
    before = copy.deepcopy(await c.store.get_run(RUN))

    async def snapshot(*args, **kwargs):
        return ["existing.py"], controlled_source_snapshot("Known final source"), "c" * 40

    original = c.engine._begin_task

    async def stop_at_verify(run_id, task_id, *args, **kwargs):
        if task_id == "verify":
            raise ValueError("Controlled failure after final acceptance")
        return await original(run_id, task_id, *args, **kwargs)

    monkeypatch.setattr(c.engine, "_snapshot", snapshot)
    monkeypatch.setattr(c.engine, "_begin_task", stop_at_verify)
    await c.continuity.execute(ticket["id"])
    current = await c.store.get_run(RUN)
    assert not current["bridge_completion_hold"]
    assert current["status"] == "failed", current.get("error")
    assert current["attempt"] == before["attempt"] == current["implementation_rounds"] == 2
    assert current["usage"] == before["usage"] and len(c.posts) == 3
    assert any(r.kind == "terminal_completion_receipt" for r in c.journal.records.rows)


@pytest.mark.parametrize("continuity_case", [True], indirect=True)
async def test_failed_verification_after_known_final_cannot_dispatch_a_new_coding_round(continuity_case, monkeypatch):
    from test_agentic_checkpoint_activation_host import controlled_source_snapshot

    from firefly_forge.process import ProcessResult

    c = continuity_case
    c.store.run.data["commands"] = [{"argv": ["approved-final-check"], "timeout": 2}]
    c.store.run.data["verification"] = []
    ticket = await consumed_blocker(c, monkeypatch, feedback=False)
    body = await reviewed_action(c, ticket)
    await c.continuity.final_outcomes.request(RUN, ticket["id"], "e" * 32, body)
    before = copy.deepcopy(await c.store.get_run(RUN))

    async def snapshot(*args, **kwargs):
        return ["existing.py"], controlled_source_snapshot("Known final source"), "c" * 40

    checks = []

    async def failed(run_id, run, workspace, argv, timeout, task_id, **kwargs):
        checks.append(argv)
        return ProcessResult(argv, 1, "Controlled verification failure")

    async def no_coding(*args, **kwargs):
        raise AssertionError("Final acceptance cannot reserve a new implementation")

    from firefly_forge import repair_proposals

    async def no_repair_context(*args, **kwargs):
        return None

    monkeypatch.setattr(repair_proposals, "verification_context", no_repair_context)
    monkeypatch.setattr(c.engine, "_snapshot", snapshot)
    monkeypatch.setattr(c.engine, "_project_command", failed)
    monkeypatch.setattr(c.provider, "execute", no_coding)
    await c.continuity.execute(ticket["id"])
    current = await c.store.get_run(RUN)
    assert not current["bridge_completion_hold"] and current["status"] == "failed", current.get("error")
    assert current["attempt"] == before["attempt"] == current["implementation_rounds"] == 2
    assert current["repair_budget"] == before["repair_budget"] and current["usage"] == before["usage"]
    assert checks == [["approved-final-check"]], current.get("error")
    assert any(r.kind == "receipt" and r.task_id == "verify" for r in c.journal.records.rows), current.get("error")
    assert len(c.posts) == 3
