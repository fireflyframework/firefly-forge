# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""A claimed request needs a distinct reviewed pre-admission refusal continuation."""

import copy
import hashlib
import json
import traceback
from types import MethodType

import httpx2
import pytest
from test_agentic_checkpoint_activation_host import result
from test_bridge_inflight_cancel import RUN
from test_invalid_read_recovery_host import failed_preclaim, recovery_input

pytest_plugins = ["test_invalid_read_native_protocol_host"]


async def refused_claim(c, *, prior_preclaim=True):
    if prior_preclaim:
        before = await failed_preclaim(c)
        candidate = await c.continuity.recoveries.candidate(RUN, before["id"])
        await c.continuity.recoveries.request(RUN, before["id"], "b" * 32, recovery_input(candidate))
    else:
        from test_invalid_read_native_protocol_host import admit

        # The retained producer stopped before this new reservation, as in the native restart cut.
        task = await c.journal._task(RUN, "implement")
        task.status = "running"

        async def pending():
            return [await c.journal._task(RUN, "implement")]

        c.journal.tasks.find_pending_recovery = pending
        before_startup = copy.deepcopy([row.data for row in c.journal.records.rows])
        assert await c.journal.recover() == 0
        assert [row.data for row in c.journal.records.rows] == before_startup
        before, _ = await admit(c)

        async def budget(self, key):
            rows = [row for row in self.rows if row.request_id == key]
            return len(rows), sum(len(json.dumps(row.data).encode()) for row in rows)

        c.requests.journal.custody_budget = MethodType(budget, c.requests.journal)
    client = c.provider.model().client._client
    ordinary = client._transport.handler

    def refuse(request):
        if request.method == "GET":
            return ordinary(request)
        c.posts.append(request.content)
        return httpx2.Response(400, json={"error": {"type": "invalid_request_error", "message": "Fixture refusal"}})

    client._transport = httpx2.MockTransport(refuse)
    await c.continuity.execute(before["id"])
    failed = await c.continuity.get(RUN, before["id"])
    assert failed["status"] == "needs_reconciliation" and failed["generation"]
    assert len(c.posts) == 2 and len(c.journal.sandboxes.rows) == 11
    return failed, ordinary


def evidence(c, failed):
    from firefly_forge.provider_rejection_models import ReviewedProviderRejectionV1

    entries = [row.data for row in c.journal.records.rows]
    request_id = c.store.run.data["bridge_completion_hold"]["request_id"]
    intent = next(e for e in entries if e["kind"] == "bridge_request_intent" and e["idempotency_key"] == request_id)
    checkpoint = next(e for e in entries if e["kind"] == "agent_checkpoint" and e["idempotency_key"] == request_id)
    descriptor = intent["payload"]["descriptor"]
    return ReviewedProviderRejectionV1(
        version=1,
        provider="bridge",
        classification="request_refused_before_admission",
        completion_request_id=intent["idempotency_key"],
        intent_digest=intent["digest"],
        checkpoint_record_digest=checkpoint["digest"],
        wire_sha256=hashlib.sha256(c.posts[-1]).hexdigest(),
        wire_bytes=len(c.posts[-1]),
        endpoint=descriptor["endpoint"],
        principal_id=descriptor["principal_id"],
        model=descriptor["model"],
        executed_epoch={
            "executable_sha256": "1" * 64,
            "source_revision": "2" * 40,
            "source_tree": "3" * 40,
            "attestation_sha256": "4" * 64,
        },
        validation_epoch={
            "executable_sha256": "1" * 64,
            "source_revision": "2" * 40,
            "source_tree": "3" * 40,
            "validator_sha256": "5" * 64,
            "route_sha256": "6" * 64,
            "report_sha256": "7" * 64,
            "binary_probe_report_sha256": "8" * 64,
        },
        http_status=400,
        admissions=0,
        jobs=0,
        provider_dispatches=0,
        tool_callbacks=0,
    )


@pytest.mark.parametrize("continuity_case", [True], indirect=True)
@pytest.mark.parametrize(
    "prior_preclaim,restart_cut", [(True, None), (False, "before"), (False, "after"), (True, "verification")]
)
async def test_claimed_pre_job_refusal_requires_separate_review_and_resumes_same_request(
    continuity_case, prior_preclaim, restart_cut
):
    from firefly_forge.invalid_read_lease import ContinuityLeasePolicyV1, write_policy
    from firefly_forge.provider_rejection_models import RejectedBoundaryRecoveryInputV1

    c = continuity_case
    if restart_cut == "verification":
        c.store.run.data["worker_request"] = {
            "id": "retained-verification",
            "kind": "verification",
            "status": "answered",
            "response_source": "factory_verification",
            "task_id": "implement",
            "attempt": 2,
        }
        c.store.run.data["verification_handoff"] = {
            "id": "retained-handoff",
            "request_id": "retained-verification",
            "status": "completed",
        }
    failed, ordinary = await refused_claim(c, prior_preclaim=prior_preclaim)
    if restart_cut == "before":

        async def pending():
            return [await c.journal._task(RUN, "implement")]

        c.journal.tasks.find_pending_recovery = pending
        before_startup = copy.deepcopy([row.data for row in c.journal.records.rows])
        assert await c.journal.recover() == 0
        assert await c.journal.recover() == 0
        assert [row.data for row in c.journal.records.rows] == before_startup
    with pytest.raises(ValueError, match="never-claimed"):
        await c.continuity.recoveries.candidate(RUN, failed["id"])
    history = copy.deepcopy([row.data for row in c.journal.records.rows])
    original_commands = copy.deepcopy([row.data for row in c.journal.sandboxes.rows])
    reviewed = evidence(c, failed)
    candidate = await c.continuity.rejections.candidate(RUN, failed["id"], reviewed)
    assert candidate["admission"] == "HOLD" and "rejection_recovery_disabled" in candidate["holds"]
    write_policy(
        c.store.configuration,
        ContinuityLeasePolicyV1(version=1, enabled=True, max_window_seconds=180, allow_pre_admission_recovery=True),
    )
    candidate = await c.continuity.rejections.candidate(RUN, failed["id"], reviewed)
    assert candidate["admission"] == "READY"
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
    receipt = await c.continuity.rejections.request(RUN, failed["id"], "d" * 32, body)
    assert receipt["status"] == "queued" and receipt["request_id"] == failed["id"]
    assert await c.continuity.rejections.request(RUN, failed["id"], "d" * 32, body) == receipt
    assert [row.data for row in c.journal.records.rows][: len(history)] == history
    queued = await c.continuity.get(RUN, failed["id"])
    assert queued["generation"] == failed["generation"] and queued["owner_pid"] == failed["owner_pid"]
    assert queued["admission_seal"] == failed["admission_seal"]
    assert queued["checkpoint_digest"] == failed["checkpoint_digest"]
    if restart_cut == "after":
        before_startup = copy.deepcopy([row.data for row in c.journal.records.rows])
        assert await c.journal.recover() == 0
        assert await c.journal.recover() == 0
        assert [row.data for row in c.journal.records.rows] == before_startup
    if restart_cut == "verification":
        from firefly_forge.journal import digest

        await c.journal.verification_refuse(RUN, digest(c.store.run.data["worker_request"]))
        assert c.journal.records.rows[-1].kind == "verification_handoff_refused"
    turns = []

    def resumed(request):
        if request.method == "GET":
            return ordinary(request)
        c.posts.append(request.content)
        turns.append(json.loads(request.content))
        if len(turns) <= 2:
            response = result(tool=True)
            call = response["choices"][0]["message"]["tool_calls"][0]
            call["id"] = "new-after-refusal-" + str(len(turns))
            call["function"] = {
                "name": "execute_command",
                "arguments": json.dumps({"argv": ["new-controlled-" + str(len(turns))], "timeout": 2}),
            }
        else:
            response = result(
                json.dumps(
                    {
                        "summary": "Controlled same-attempt verification boundary",
                        "worker_feedback": {
                            "id": "after-refusal-review",
                            "role_id": "implementer",
                            "task_id": "implement",
                            "attempt": 2,
                            "disposition": "clarification",
                            "summary": "Retained state preserved",
                            "request": {
                                "id": "after-refusal",
                                "kind": "product_definition",
                                "question": "Proceed?",
                                "context": "Fixture",
                            },
                        },
                        "qa_report": None,
                    }
                )
            )
        return httpx2.Response(200, json=response)

    c.provider.model().client._client._transport = httpx2.MockTransport(resumed)
    execute = c.continuity.rejections.execute
    errors = []

    async def traced(ticket):
        try:
            return await execute(ticket)
        except Exception:
            errors.append(traceback.format_exc())
            raise

    c.continuity.rejections.execute = traced
    await c.continuity.execute(failed["id"])
    complete = await c.continuity.get(RUN, failed["id"])
    assert complete["status"] == "completed", (errors, c.store.run.data.get("error"), len(turns))
    assert len(c.requests.records.rows) == 1 and len(c.journal.sandboxes.rows) == 13
    assert [row.data for row in c.journal.sandboxes.rows][:11] == original_commands
    assert c.store.run.data["attempt"] == 2 and c.store.run.data["repair_budget"] == 2
    assert len(turns) == 3
    assert hashlib.sha256(c.posts[1]).hexdigest() == hashlib.sha256(c.posts[2]).hexdigest()
    from firefly_forge.agentic_checkpoint_boundary import BoundaryCheckpoint
    from firefly_forge.invalid_read_dispatch_guard import assert_generic_dispatch_allowed

    initial = BoundaryCheckpoint.decode(failed["checkpoint"].encode(), failed["checkpoint_digest"])
    assert c.store.run.data["usage"]["requests"] == initial.usage.requests + 3
    last = [row.data["payload"] for row in c.journal.records.rows if row.kind == "agent_checkpoint"][-1]
    final_pending = BoundaryCheckpoint.decode(last["snapshot"].encode(), last["checkpoint_digest"])
    assert final_pending.usage.requests == initial.usage.requests + 2
    assert final_pending.usage.tool_calls == initial.usage.tool_calls + 2
    c.store.execution_requests = c.requests
    await assert_generic_dispatch_allowed(c.store, c.journal, await c.store.get_run(RUN))
    await c.continuity.execute(failed["id"])
    assert len(turns) == 3


@pytest.mark.parametrize("continuity_case", [True], indirect=True)
async def test_refusal_review_cannot_substitute_wire_source_actor_or_later_effects(continuity_case):
    from firefly_forge.invalid_read_lease import ContinuityLeasePolicyV1, write_policy
    from firefly_forge.project_access import AccessDenied
    from firefly_forge.provider_rejection_models import ReviewedProviderRejectionV1

    c = continuity_case
    failed, _ = await refused_claim(c)
    write_policy(
        c.store.configuration,
        ContinuityLeasePolicyV1(
            version=1,
            enabled=True,
            max_window_seconds=180,
            allow_pre_admission_recovery=True,
        ),
    )
    reviewed = evidence(c, failed)
    for field, value in [
        ("intent_digest", "f" * 64),
        ("checkpoint_record_digest", "f" * 64),
        ("wire_sha256", "f" * 64),
        ("wire_bytes", reviewed.wire_bytes + 1),
        ("endpoint", "http://localhost:18181/v1"),
        ("principal_id", "different"),
        ("model", "different"),
    ]:
        changed = ReviewedProviderRejectionV1.model_validate({**reviewed.model_dump(), field: value})
        with pytest.raises(ValueError, match="exact native request"):
            await c.continuity.rejections.candidate(RUN, failed["id"], changed)
    path = c.workspace / "existing.py"
    original = path.read_bytes()
    path.write_bytes(original + b"# changed\n")
    with pytest.raises(ValueError, match="source"):
        await c.continuity.rejections.candidate(RUN, failed["id"], reviewed)
    path.write_bytes(original)
    c.user.enabled = False
    with pytest.raises(AccessDenied):
        await c.continuity.rejections.candidate(RUN, failed["id"], reviewed)
    c.user.enabled = True
    row = c.requests.records.rows[0]
    row.data = {**row.data, "generation": "f" * 32}
    with pytest.raises(ValueError, match="claimed|history"):
        await c.continuity.rejections.candidate(RUN, failed["id"], reviewed)
    row.data = copy.deepcopy(failed)
    await c.journal._append(RUN, "implement", "later-unaccounted", "unknown_effect", {"status": "unknown"})
    with pytest.raises(ValueError, match="Later native activity"):
        await c.continuity.rejections.candidate(RUN, failed["id"], reviewed)
    assert len(c.posts) == 2 and len(c.requests.records.rows) == 1 and len(c.journal.sandboxes.rows) == 11


@pytest.mark.parametrize("continuity_case", [True], indirect=True)
@pytest.mark.parametrize("change", ["policy", "actor", "source", "profile", "cancelled", "expired", "configuration"])
async def test_changed_current_authority_prevents_any_new_provider_or_command(continuity_case, monkeypatch, change):
    from firefly_forge.configuration import ProviderConfiguration
    from firefly_forge.invalid_read_lease import ContinuityLeasePolicyV1, write_policy
    from firefly_forge.provider_rejection_models import RejectedBoundaryRecoveryInputV1

    c = continuity_case
    failed, _ = await refused_claim(c)
    write_policy(
        c.store.configuration,
        ContinuityLeasePolicyV1(
            version=1,
            enabled=True,
            max_window_seconds=180,
            allow_pre_admission_recovery=True,
        ),
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
    receipt = await c.continuity.rejections.request(RUN, failed["id"], "e" * 32, body)
    if change == "policy":
        write_policy(c.store.configuration, ContinuityLeasePolicyV1(version=1, enabled=False))
    elif change == "actor":
        c.user.enabled = False
    elif change == "source":
        (c.workspace / "existing.py").write_text("VALUE = 99\n")
    elif change == "profile":
        monkeypatch.setattr(
            "firefly_forge.invalid_read_continuities.current_profile_seal",
            lambda: {"profile_id": "changed", "digest": "f" * 64},
        )
    elif change == "cancelled":
        c.store.run.data["cancel_request"] = {"status": "requested"}
    elif change == "expired":
        monkeypatch.setattr(
            "firefly_forge.provider_rejection_recoveries.time.time",
            lambda: receipt["lease"]["expires_at_ms"] / 1000 + 1,
        )
    else:
        c.store.configuration.update("bridge", ProviderConfiguration(values={"model": "different"}))
    await c.continuity.execute(failed["id"])
    current = await c.requests.get(failed["id"])
    assert current["status"] == "failed" and current["generation"] == failed["generation"]
    assert len(c.posts) == 2 and len(c.journal.sandboxes.rows) == 11
    events = await c.requests.events(failed["id"])
    assert [e["kind"] for e in events][-2:] == ["request_recovered_after_rejection", "worker_failed"]
