# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Real feedback/classification consumers; controlled read projection and model/HTTP boundaries."""

import copy
import hashlib
from types import SimpleNamespace

import pytest
from communication_model_fixture import reviewed_model
from test_communication_accepted_recovery_host import accepted_run
from test_communication_writer import configured_store
from test_scm_delegated_feedback import RECEIPT, RUN, received
from test_scm_delegated_feedback import delegated_feedback as delegated_feedback
from test_scm_native_verification_status_host import status_input

from firefly_forge.communication import CommunicationConversationV1, communication_facts
from firefly_forge.execution_requests import digest as request_digest
from firefly_forge.journal import digest
from firefly_forge.project_access import AccessDenied
from firefly_forge.scm_native_verification_status_models import SCMNativeVerificationStatusV1


def status_for_run(run, *, unknown=False):
    value = status_input(unknown=unknown)
    for key in ("project_id", "repository_id", "component_id", "attempt"):
        value[key] = run[key]
    value["run_id"] = run["id"]
    value["parent_run_digest"] = digest(run)
    receipt = value["operations"][0]["receipt"]
    receipt["run_id"] = receipt["candidate"]["run_id"] = run["id"]
    receipt["candidate"]["digest"] = digest({k: v for k, v in receipt["candidate"].items() if k != "digest"})
    receipt["request"]["candidate_digest"] = receipt["candidate"]["digest"]
    receipt["request_digest"] = request_digest(receipt["request"])
    if unknown:
        value.pop("held_predecessor")
    value["digest"] = digest({k: v for k, v in value.items() if k != "digest"})
    return SCMNativeVerificationStatusV1.model_validate(value)


async def test_classifier_current_facts_include_native_progress_without_a_fake_scm_proposal():
    from test_scm_natural_worker_context_host import fixture

    natural, delivery, scopes, _, runs = await fixture()
    run = accepted_run() | dict(
        id="1" * 32, project_id=scopes[0]["project_id"], repository_id=delivery["repository_id"], component_id="api"
    )
    runs[run["id"]] = run
    view = status_for_run(run)
    reads = []

    async def read(current, scope, reference):
        assert current == delivery and scope == scopes[0]
        reads.append(reference)
        return view if reference["run_id"] == run["id"] else None

    natural.collaboration.native_verification_status = SimpleNamespace(read=read)
    context = await natural._context(delivery, scopes)
    facts = next(c["current_facts"] for c in context["candidates"] if c["run_id"] == run["id"])
    assert facts["native_verification"]["current"]["known_passed"] == 4
    assert facts["native_verification"]["held_predecessor"]["review_only"] is True
    assert facts["consumed_terminal_recovery"]["state"] == "accepted"
    assert "proposal_id" not in str(facts["native_verification"])
    assert reads


@pytest.mark.parametrize(
    "outcome",
    ["sent", "unknown", "progress_during_draft", "progress_before_write", "revoked", "unconfigured", "lost_response"],
)
async def test_native_status_feedback_is_reviewed_fresh_and_never_reposted(delegated_feedback, tmp_path, outcome):
    service, row, run, sent, state = delegated_feedback
    run.update(accepted_run())
    received(row, "status")
    row.data.update(
        status="processed", action_status="completed", feedback_status="pending", run_id=RUN, digest="d" * 64
    )
    row.data["natural_turn"] = dict(
        classifier_outcome="confirmed_valid",
        source_digest=row.data["digest"],
        policy_digest="e" * 64,
        human_message="How is verification progressing?",
        context={"scope": {key: row.data[key] for key in ("repository_id", "actor", "issue")}},
        state="status",
        decision=dict(intent="status", language="en", message="Historical draft is not authority."),
        resolution=dict(state="status", run_id=RUN),
        reply="Historical draft is not authority.",
    )
    read_state = {"view": status_for_run(run, unknown=outcome == "unknown")}

    async def current(data):
        assert data["run_id"] == RUN
        if state["deny"]:
            raise AccessDenied("Current native project read permission changed")
        return read_state["view"]

    service.native_verification_status = SimpleNamespace(for_delivery=current)
    if outcome != "unconfigured":
        service.store.task_router = (await configured_store(tmp_path)).task_router
    facts = communication_facts(
        run,
        conversation=CommunicationConversationV1(
            selection="run",
            language="en",
            linked_runs=1,
            question=row.data["natural_turn"]["human_message"],
            question_sha256=hashlib.sha256(row.data["natural_turn"]["human_message"].encode()).hexdigest(),
        ),
        native_verification=read_state["view"],
    )
    keys = sorted(facts)
    card = dict(
        title=dict(text="Independent verification is held", fact_ids=keys[:4]),
        paragraphs=[
            dict(
                text="Four of seven checks have known passing results. The earlier handoff is retained history.",
                fact_ids=keys[i : i + 4],
            )
            for i in range(4, len(keys), 4)
        ],
    )
    calls = []

    def drafted(messages, info):
        from pydantic_ai.messages import ModelResponse, ToolCallPart

        if outcome == "progress_during_draft":
            read_state["view"] = status_for_run(run, unknown=True)
        if outcome == "revoked":
            state["deny"] = True
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, card)])

    service.communication_writer.model = reviewed_model(drafted, calls=calls)
    original_write = service._remote_write

    async def transport(*args, **kwargs):
        if outcome == "progress_before_write":
            read_state["view"] = status_for_run(run, unknown=True)
        return await original_write(*args, **kwargs)

    service._remote_write = transport
    state["fail_delivery"] = outcome == "lost_response"
    await service.feedback(RECEIPT)
    if outcome in {"unconfigured", "progress_during_draft", "progress_before_write", "revoked"}:
        assert sent == [] and row.data["feedback_status"] in {"held", "needs_reconciliation"}
        return
    assert len(sent) == 1 and [phase for phase, *_ in calls] == ["draft", "review"]
    recorded = row.data["feedback_intent"]["communication"]["facts"]
    assert recorded["native_verification"]["current"]["known_passed"] == 4
    assert recorded["native_verification"]["current"]["unknown"] is (outcome == "unknown")
    assert recorded["factory_verification"]["checks"]["recorded"] == 7
    assert recorded["check_status"]["recorded"] == 0
    if outcome == "lost_response":
        original = copy.deepcopy(row.data["feedback_intent"])
        await service.sync_feedback()
        await service.feedback(RECEIPT)
        assert row.data["feedback_status"] == "needs_reconciliation"
        assert row.data["feedback_intent"] == original and len(sent) == 1 and len(calls) == 2
    else:
        assert row.data["feedback_status"] == "sent"


async def test_explicit_issue_status_uses_same_read_projection_without_natural_proposal(delegated_feedback, tmp_path):
    service, row, run, sent, _ = delegated_feedback
    run.update(accepted_run())
    view = status_for_run(run)

    async def current(data):
        assert data["run_id"] == RUN
        return view

    service.native_verification_status = SimpleNamespace(for_delivery=current)
    service.store.task_router = (await configured_store(tmp_path)).task_router
    keys = sorted(communication_facts(run, native_verification=view))
    service.communication_writer.model = reviewed_model(
        dict(
            title=dict(text="Four new checks passed", fact_ids=keys[:4]),
            paragraphs=[
                dict(
                    text="Independent verification is held with four of seven known passing results.",
                    fact_ids=keys[i : i + 4],
                )
                for i in range(4, len(keys), 4)
            ],
        )
    )
    received(row, "status")
    await service.process(RECEIPT)
    assert row.data["feedback_status"] == "sent" and len(sent) == 1
    assert row.data.get("natural_turn") is None
    assert row.data["feedback_intent"]["communication"]["facts"]["native_verification"]["current"]["known_passed"] == 4


async def test_unconfigured_writer_never_returns_stale_native_card_for_native_qa():
    from firefly_forge.communication import CommunicationWriter

    run = accepted_run() | dict(id="b" * 32, project_id="1" * 32, repository_id="2" * 32, component_id="api")
    result = await CommunicationWriter(SimpleNamespace(reject_credentials=lambda value: None)).write(
        run, "Old native card omits independent QA", lambda value: value, native_verification=status_for_run(run)
    )
    assert result["body"] == "" and result["communication"]["mode"] == "held"
