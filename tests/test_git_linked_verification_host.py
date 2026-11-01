# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Separate verification custody cannot be bypassed by an explicit WIP checkpoint."""

import copy
from types import SimpleNamespace
from uuid import UUID

import pytest
from linked_verification_fixtures import candidate as candidate_fixture
from test_checkpoint_test_source_host import source_case as source_case
from test_checkpoint_test_source_host import source_service
from test_git_delivery_local import delivery_case as delivery_case
from test_git_delivery_local import git
from test_git_delivery_operational import operational as operational

from firefly_forge.execution_requests import digest as event_digest
from firefly_forge.git_delivery_models import GitDeliveryInputV1
from firefly_forge.journal import digest
from firefly_forge.linked_verification_models import LinkedVerificationCandidateV1, LinkedVerificationInputV1
from firefly_forge.linked_verifications import LinkedVerifications
from firefly_forge.models import RunInput
from firefly_forge.verification_models import verification_source

RUN, OPERATION = "1" * 32, "8" * 32


@pytest.fixture
def linked_delivery(operational):
    c = operational
    run = c.state["run"]
    run.update(
        commands=[{"argv": ["approved-check"], "timeout": 30}],
        attempt=2,
        implementation_pending=False,
        component_id="api",
    )
    actor, identity = {"user_id": "4" * 32}, {"issuer": "fixture", "subject": "operator"}
    candidate = candidate_fixture(1)
    candidate.update(
        run_id=RUN,
        commands_digest=digest({"commands": run["commands"]}),
        run_digest=digest(run),
        actor_digest=digest({"actor": actor, "identity": identity}),
    )
    candidate["digest"] = digest({key: value for key, value in candidate.items() if key != "digest"})
    LinkedVerificationCandidateV1.model_validate(candidate)
    body = LinkedVerificationInputV1(
        version=1,
        request_id=OPERATION,
        candidate_digest=candidate["digest"],
        acknowledge_all_fresh_checks=True,
        acknowledge_no_coding_or_repair=True,
        acknowledge_original_history_and_budget_preserved=True,
    ).model_dump(mode="json")
    events = []

    def append(kind, evidence):
        value = dict(
            request_id=OPERATION,
            sequence=len(events) + 1,
            previous_hash=events[-1]["digest"] if events else "0" * 64,
            kind=kind,
            evidence=evidence,
            created_at="2026-11-01T00:00:00Z",
        )
        value["digest"] = event_digest(value)
        events.append(value)
        return value

    row = SimpleNamespace(id=UUID(OPERATION), run_id=UUID(RUN), kind="linked_verify", status="queued", data={})

    def state(status):
        events.clear()
        row.status = status
        row.data = dict(
            id=OPERATION,
            run_id=RUN,
            kind="linked_verify",
            status=status,
            request=body,
            request_digest=event_digest(body),
            candidate=copy.deepcopy(candidate),
            actor=actor,
            identity=identity,
            parent_run=copy.deepcopy(run),
            commands=copy.deepcopy(run["commands"]),
            commands_digest=candidate["commands_digest"],
            effect_budget=1,
            inputs_digest=candidate["input_scope_digest"],
        )
        append("linked_verification_admitted", {"request_digest": row.data["request_digest"]})
        if status != "queued":
            append("linked_verification_claimed", {"request_digest": row.data["request_digest"]})
            intent = append(
                "linked_verification_command_intent",
                {
                    "ordinal": 1,
                    "command_digest": event_digest(run["commands"][0]),
                    "inputs_digest": row.data["inputs_digest"],
                },
            )
            if status == "failed":
                append(
                    "linked_verification_command_result",
                    {
                        "ordinal": 1,
                        "intent_digest": intent["digest"],
                        "exit_code": 1,
                        "output": "Controlled known failed check",
                    },
                )
                append("linked_verification_held", {"reason": "check_failed"})
            elif status == "needs_reconciliation":
                append("linked_verification_held", {"reason": "unknown"})
        c.rows[row.id] = row

    async def linked_events(request_id, maximum):
        assert request_id == row.id and len(events) <= maximum
        return [SimpleNamespace(data=copy.deepcopy(event)) for event in events]

    c.requests.journal.linked_verification_events = linked_events
    c.service.store.execution_requests = c.requests
    c.service.engine.linked_verifications = LinkedVerifications(
        c.service.store, c.service.journal, c.requests, c.service.engine
    )
    c.linked_row, c.linked_events, c.linked_state = row, events, state
    state("failed")
    return c


@pytest.mark.parametrize("status", ["queued", "executing", "needs_reconciliation"])
@pytest.mark.parametrize("gate", ["candidate", "admission", "worker"])
async def test_unsettled_verification_blocks_each_git_boundary(linked_delivery, status, gate):
    c = linked_delivery
    c.linked_state(status)
    before = copy.deepcopy(c.state)
    with pytest.raises(ValueError, match="verification"):
        if gate == "candidate":
            await c.controller.candidate(RUN)
        elif gate == "admission":
            await c.controller.request(
                RUN,
                GitDeliveryInputV1(
                    version=1,
                    request_id="a" * 32,
                    expected_head=c.base,
                    expected_source_digest=verification_source(c.root),
                ),
            )
        else:
            await c.service._guard(RUN, before["run"], {"user_id": "4" * 32})
    assert c.state == before and git(c.root, "rev-parse", "HEAD") == c.base
    assert len(c.rows) == 1


async def test_queued_verification_after_git_admission_is_refused_before_git_intent(linked_delivery):
    c = linked_delivery
    c.rows.pop(UUID(OPERATION))
    (c.root / "product.py").write_text("value = 2\n")
    body = GitDeliveryInputV1(
        version=1,
        request_id="a" * 32,
        expected_head=c.base,
        expected_source_digest=verification_source(c.root),
    )
    await c.controller.request(RUN, body)
    c.linked_state("queued")
    await c.requests.git_deliveries.execute(body.request_id)
    assert c.rows[UUID(body.request_id)].status == "failed"
    assert not c.state["entries"] and git(c.root, "rev-parse", "HEAD") == c.base


async def test_known_failed_closed_checks_keep_explicit_wip_checkpoint_available(linked_delivery):
    c = linked_delivery
    original = copy.deepcopy(c.state["run"])
    ledger = copy.deepcopy(c.linked_events)
    (c.root / "product.py").write_text("value = 2\n")
    review = await c.controller.candidate(RUN)
    body = GitDeliveryInputV1(
        version=1,
        request_id="a" * 32,
        expected_head=review["expected_head"],
        expected_source_digest=review["expected_source_digest"],
    )
    await c.controller.request(RUN, body)
    await c.requests.git_deliveries.execute(body.request_id)
    receipt = await c.controller.ticket(RUN, body.request_id)
    assert receipt["status"] == "completed", receipt
    assert receipt["result"].get("verification_status", "pending") == "pending"
    assert c.state["run"]["status"] == "paused" and c.state["run"]["attempt"] == original["attempt"]
    assert c.linked_events == ledger and c.linked_row.status == "failed"
    assert git(c.root, "rev-parse", "HEAD^") == c.base


@pytest.mark.parametrize("change", ["missing_result", "foreign_identity", "changed_candidate"])
async def test_a_failed_enum_cannot_replace_exact_closed_native_custody(linked_delivery, change):
    c = linked_delivery
    if change == "missing_result":
        c.linked_events.pop(-2)
        previous = "0" * 64
        for index, event in enumerate(c.linked_events, 1):
            event.update(sequence=index, previous_hash=previous)
            event["digest"] = previous = event_digest({key: value for key, value in event.items() if key != "digest"})
    elif change == "foreign_identity":
        c.linked_row.data["run_id"] = "9" * 32
    else:
        c.linked_row.data["candidate"]["source_digest"] = "0" * 64
    with pytest.raises(ValueError, match="verification|consent|receipt"):
        await c.controller.candidate(RUN)


async def test_known_failed_checkpoint_needs_confirmed_remote_before_fresh_test_or_maintain(
    linked_delivery, source_case
):
    c, source = linked_delivery, source_case
    (c.root / "product.py").write_text("value = 2\n")
    review = await c.controller.candidate(RUN)
    body = GitDeliveryInputV1(
        version=1,
        request_id="a" * 32,
        expected_head=review["expected_head"],
        expected_source_digest=review["expected_source_digest"],
    )
    await c.controller.request(RUN, body)
    await c.requests.git_deliveries.execute(body.request_id)
    ticket = await c.controller.ticket(RUN, body.request_id)
    assert ticket["status"] == "completed", ticket
    checkpoint = ticket["result"]
    source.run.clear()
    source.run.update(copy.deepcopy(c.state["run"]))
    source.checkpoint.clear()
    source.checkpoint.update(checkpoint)
    source.entries[:] = copy.deepcopy(c.state["entries"])
    service = await source_service(source)
    observed = []

    async def exact_remote(repository, parent, receipt):
        observed.append(receipt["revision"])
        return receipt["revision"]

    service._observe_revision = exact_remote
    reference = dict(
        version=1,
        run_id=RUN,
        request_id=body.request_id,
        expected_checkpoint_digest=checkpoint["checkpoint_digest"],
        expected_revision=checkpoint["revision"],
    )
    request = RunInput(
        repository_id=source.run["repository_id"],
        project_id=source.run["project_id"],
        component_id="api",
        brief="Review the explicitly published WIP source",
        operation="test",
        publish=False,
        source_checkpoint=reference,
    )
    before = copy.deepcopy((c.state["run"], c.linked_row.data, c.linked_events))
    with pytest.raises(ValueError, match="confirmed remote"):
        await service.prepare(request)
    assert not observed
    remote = await c.service._append(
        RUN,
        "git_delivery_remote_intent",
        "remote:" + body.request_id,
        dict(request_id=body.request_id, revision=checkpoint["revision"], body_sha256="7" * 64),
    )
    source.entries[:] = copy.deepcopy(c.state["entries"])
    with pytest.raises(ValueError, match="unknown|remote"):
        await service.prepare(request)
    assert not observed
    await c.service._append(
        RUN,
        "git_delivery_remote_receipt",
        "remote:" + body.request_id,
        dict(
            request_id=body.request_id,
            revision=checkpoint["revision"],
            remote_status="draft",
            intent_digest=remote["digest"],
            reference=dict(body_sha256="7" * 64),
        ),
    )
    source.entries[:] = copy.deepcopy(c.state["entries"])
    for operation in ("test", "maintain"):
        value = await service.prepare(RunInput.model_validate({**request.model_dump(), "operation": operation}))
        assert value["reference"] == reference and value["source_tree"] == checkpoint["source_tree"]
        assert value.get("operation", "test") == operation
        assert not {"commands", "execution_actor", "sandbox_snapshot", "usage", "repair_budget"} & value.keys()
    assert (c.state["run"], c.linked_row.data, c.linked_events) == before
    assert len(observed) == 2
