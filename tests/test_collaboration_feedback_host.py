# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Real feedback controller with host-only persistence and transport boundaries."""

import copy
import json
import re

import pytest
from test_scm_delegated_feedback import (
    RECEIPT,
    received,
)
from test_scm_delegated_feedback import (
    delegated_feedback as delegated_feedback,
)


async def test_status_uses_a_markdown_card_without_serialized_action_data(delegated_feedback):
    service, row, run, sent, _ = delegated_feedback
    received(row, "status")
    await service.process(RECEIPT)
    body = sent[0]["body"]
    assert body.startswith("### forge · Your decision is needed")
    assert "needs an answer" in body and "Which result is expected?" in body
    command = re.search(r"```text\n(/forge respond [^\n]+)\n```", body)
    assert command is not None
    from firefly_forge.collaboration import RepositoryCommand

    parsed = RepositoryCommand.parse(command[1])
    assert parsed.action == "respond" and parsed.run_id == run["id"]
    prose = body.replace(command[0], "")
    assert '"run_id"' not in prose and '"worker_request"' not in body
    assert RECEIPT in body
    assert body.index(RECEIPT) > body.index("<details>")


async def test_native_stages_are_proactive_and_unchanged_ticks_are_quiet(delegated_feedback):
    service, row, run, sent, _ = delegated_feedback
    received(row, "status")
    run.update(status="queued", worker_request={})
    await service.process(RECEIPT)
    original = copy.deepcopy(row.data["action_result"])
    run.update(status="running", active_task="prepare")
    await service.sync_feedback()
    await service.sync_feedback()
    assert len(sent) == 2 and "Preparing the workspace" in sent[-1]["body"]
    run["active_task"] = "implement"
    await service.sync_feedback()
    assert len(sent) == 3 and "Implementing the requested change" in sent[-1]["body"]
    assert row.data["action_result"] == original
    assert len(row.data["feedback_history"]) == 3
    assert len({item["key"] for item in row.data["feedback_history"]}) == 3
    assert all(item["remote"]["id"] == 91 for item in row.data["feedback_history"])


async def test_stage_and_token_noise_do_not_create_activity_posts(delegated_feedback):
    service, row, run, sent, _ = delegated_feedback
    received(row, "status")
    run.update(status="running", active_task="implement", worker_request={})
    await service.process(RECEIPT)
    for index in range(4):
        run["events"] = [{"id": str(index), "stage": "output", "message": "stream token"}]
        run["updated_at"] = str(index)
        await service.sync_feedback()
    assert len(sent) == 1


async def test_lost_stage_delivery_keeps_exact_intent_and_never_reposts(delegated_feedback):
    service, row, run, sent, state = delegated_feedback
    received(row, "status")
    run.update(status="queued", worker_request={})
    await service.process(RECEIPT)
    state["fail_delivery"] = True
    run.update(status="running", active_task="implement")
    await service.sync_feedback()
    assert len(sent) == 2 and row.data["feedback_status"] == "needs_reconciliation"
    retained = copy.deepcopy(row.data["feedback_intent"])
    run["active_task"] = "verify"
    await service.sync_feedback()
    await service.process(RECEIPT)
    assert len(sent) == 2 and row.data["feedback_intent"] == retained
    assert len(retained["body_sha256"]) == 64


async def test_revoked_delegated_actor_prevents_proactive_delivery(delegated_feedback):
    service, row, run, sent, state = delegated_feedback
    received(row, "status")
    run.update(status="queued", worker_request={})
    await service.process(RECEIPT)
    state["deny"] = True
    run.update(status="running", active_task="implement")
    await service.sync_feedback()
    assert len(sent) == 1


async def test_progress_cap_reserves_a_terminal_card(delegated_feedback):
    service, row, run, sent, _ = delegated_feedback
    received(row, "status")
    run.update(status="running", active_task="implement", worker_request={})
    await service.process(RECEIPT)
    row.data["feedback_history"] = [copy.deepcopy(row.data["feedback_history"][0]) for _ in range(30)]
    run["active_task"] = "verify"
    await service.sync_feedback()
    assert len(sent) == 1
    service.scm._host = lambda _: "https://github.com"
    run.update(
        status="succeeded", final_verification=[{"exit_code": 0}], pr_url="https://github.com/team/product/pull/9"
    )
    await service.sync_feedback()
    assert len(sent) == 2 and "1/1" in sent[-1]["body"]
    assert "https://github.com/team/product/pull/9" in sent[-1]["body"]
    assert len(row.data["feedback_history"]) == 31


async def test_untrusted_question_and_choices_cannot_inject_links_or_mentions(delegated_feedback):
    service, row, run, sent, _ = delegated_feedback
    received(row, "status")
    run["worker_request"].update(
        question="@everyone <script> [open](https://evil.test)\n# approval", choices=["**ship**"]
    )
    await service.process(RECEIPT)
    body = sent[0]["body"]
    assert "@everyone" not in body and "<script>" not in body
    assert "[open](https://evil.test)" not in body
    assert "**ship**" not in body
    assert "Choices" in body


async def test_arbitrary_action_payload_is_never_returned_as_json(delegated_feedback):
    service, row, run, sent, _ = delegated_feedback
    received(row, "status")
    await service.process(RECEIPT)
    row.data.update(
        feedback_status="pending",
        action_result={"status": "paused", "private_path": "/secret/model", "tool_json": {"password": "never-display"}},
    )
    await service.feedback(RECEIPT)
    assert "/secret/model" not in json.dumps(sent)
    assert "never-display" not in json.dumps(sent)


async def test_reconciling_a_posted_stage_does_not_repost_the_same_stage(delegated_feedback):
    service, row, run, sent, state = delegated_feedback
    received(row, "status")
    run.update(status="queued", worker_request={})
    await service.process(RECEIPT)
    state["fail_delivery"] = True
    run.update(status="running", active_task="implement")
    await service.sync_feedback()
    state["fail_delivery"] = False
    await service.reconcile(RECEIPT, "posted", "Confirmed the implementation update in the issue")
    await service.sync_feedback()
    assert len(sent) == 2
    assert row.data["feedback_key"] == row.data["feedback_intent"]["key"]
    run["active_task"] = "verify"
    await service.sync_feedback()
    assert len(sent) == 3


async def test_concurrent_ticks_claim_one_stage_write(delegated_feedback):
    import asyncio

    service, row, run, sent, _ = delegated_feedback
    received(row, "status")
    run.update(status="queued", worker_request={})
    await service.process(RECEIPT)
    run.update(status="running", active_task="implement")
    await asyncio.gather(service.sync_feedback(), service.sync_feedback())
    assert len(sent) == 2


async def test_completion_before_publication_keeps_a_final_review_slot(delegated_feedback):
    service, row, run, sent, _ = delegated_feedback
    received(row, "status")
    run.update(status="running", active_task="implement", worker_request={})
    service.scm._host = lambda _: "https://github.com"
    await service.process(RECEIPT)
    row.data["feedback_history"] = [copy.deepcopy(row.data["feedback_history"][0]) for _ in range(30)]
    run["active_task"] = "verify"
    await service.sync_feedback()
    assert len(sent) == 1
    run.update(status="succeeded", final_verification=[{"exit_code": 0}])
    await service.sync_feedback()
    assert len(sent) == 2 and len(row.data["feedback_history"]) == 31
    run["pr_url"] = "https://github.com/team/product/pull/9"
    await service.sync_feedback()
    await service.sync_feedback()
    assert len(sent) == 3 and len(row.data["feedback_history"]) == 32
    assert run["pr_url"] in sent[-1]["body"]


async def test_upgrade_counts_the_existing_sent_comment(delegated_feedback):
    service, row, run, sent, _ = delegated_feedback
    received(row, "status")
    run.update(status="queued", worker_request={})
    await service.process(RECEIPT)
    for key in ("feedback_key", "feedback_history", "feedback_intent"):
        row.data.pop(key, None)
    await service.sync_feedback()
    assert len(sent) == 1
    run.update(status="running", active_task="implement")
    await service.sync_feedback()
    assert len(sent) == 2 and len(row.data["feedback_history"]) == 2
    assert row.data["feedback_history"][0]["legacy"] is True


async def test_only_latest_issue_run_receipt_follows_future_stages(delegated_feedback):
    from types import SimpleNamespace

    service, row, run, sent, _ = delegated_feedback
    received(row, "status")
    run.update(status="queued", worker_request={})
    await service.process(RECEIPT)
    older = SimpleNamespace(data={**copy.deepcopy(row.data), "id": "9" * 32})

    async def peers(data):
        return [row, older]

    async def pending():
        return [older, row]

    service.inbox.feedback_context = peers
    service.inbox.find_pending = pending
    run.update(status="running", active_task="implement")
    await service.sync_feedback()
    assert len(sent) == 2
    assert len(older.data["feedback_history"]) == 1


async def test_unknown_sibling_blocks_even_a_new_explicit_status_post(delegated_feedback):
    from types import SimpleNamespace

    service, row, run, sent, _ = delegated_feedback
    received(row, "status")
    run.update(status="queued", worker_request={})
    await service.process(RECEIPT)
    unknown = SimpleNamespace(
        data={**copy.deepcopy(row.data), "id": "9" * 32, "feedback_status": "needs_reconciliation"}
    )

    async def peers(data):
        return [row, unknown]

    service.inbox.feedback_context = peers
    row.data["feedback_status"] = "pending"
    await service.feedback(RECEIPT)
    assert len(sent) == 1 and row.data["feedback_status"] == "pending"


@pytest.mark.parametrize("status", ["pending", "running", "completed", "held"])
async def test_typed_factory_verification_is_distinct_from_a_manual_decision(delegated_feedback, status):
    service, row, run, sent, _ = delegated_feedback
    received(row, "status")
    run["verification_handoff"] = dict(
        version=1,
        id="handoff-1",
        request_id="question-1",
        status=status,
        commands_count=7,
        completed_commands=7 if status == "completed" else 0,
        failed_commands=0,
    )
    await service.process(RECEIPT)
    body = sent[0]["body"]
    assert "factory" in body.lower() and "checks" in body
    if status in {"pending", "running", "completed"}:
        assert "Decision needed" not in body and "/forge respond" not in body
    else:
        assert "operator needs to review" in body and "Decision needed" in body


async def test_handoff_from_another_question_cannot_hide_a_pending_decision(delegated_feedback):
    service, row, run, sent, _ = delegated_feedback
    received(row, "status")
    run["verification_handoff"] = dict(version=1, id="handoff-1", request_id="different-question", status="running")
    await service.process(RECEIPT)
    assert "Decision needed" in sent[0]["body"]


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.test/team/product/pull/9",
        "https://github.com/other/project/pull/9",
        "https://github.com/team/product/pull/9?token=bad",
        "https://github.com@evil.test/team/product/pull/9",
        "https://github.com/team/product/pull/9)\n@everyone",
    ],
)
async def test_review_links_are_confined_to_the_current_app_repository(delegated_feedback, url):
    service, row, run, sent, _ = delegated_feedback
    received(row, "status")
    service.scm._host = lambda _: "https://github.com"
    run.update(status="succeeded", worker_request={}, pr_url=url)
    await service.process(RECEIPT)
    assert "Open the pull request" not in sent[0]["body"]
    assert url not in sent[0]["body"]


async def test_publication_failure_after_success_is_a_bounded_distinct_outcome(delegated_feedback):
    service, row, run, sent, _ = delegated_feedback
    received(row, "status")
    run.update(status="succeeded", worker_request={}, publish=True)
    await service.process(RECEIPT)
    assert row.data["feedback_publication_pending"] is True
    run["publication_error"] = "private provider exception /secret/private"
    await service.sync_feedback()
    assert len(sent) == 2 and "Publication needs attention" in sent[-1]["body"]
    assert "/secret/private" not in sent[-1]["body"]
    assert row.data["feedback_publication_pending"] is False


async def test_known_credentials_are_refused_before_transport(delegated_feedback):
    service, row, run, sent, _ = delegated_feedback
    received(row, "status")
    run["worker_request"]["question"] = "retained-secret-token"

    def reject(value):
        if "retained-secret-token" in str(value):
            raise ValueError("Credential-bearing public field")

    service.store.reject_credentials = reject
    await service.process(RECEIPT)
    assert sent == [] and row.data["feedback_status"] == "needs_reconciliation"


async def test_late_credential_registration_is_checked_before_markdown_transport(delegated_feedback):
    service, row, run, sent, _ = delegated_feedback
    received(row, "status")
    run["worker_request"]["question"] = "owned-late-credential"
    registered = False
    original = service._feedback_authority

    async def authority(data, *, local=False):
        nonlocal registered
        await original(data, local=local)
        if local:
            registered = True

    def reject(value):
        if registered and "owned-late-credential" in str(value):
            raise ValueError("Late credential must not be escaped past the registry")

    service._feedback_authority = authority
    service.store.reject_credentials = reject
    await service.process(RECEIPT)
    assert sent == [] and row.data["feedback_status"] == "needs_reconciliation"


async def test_factory_owned_handoff_does_not_add_a_manual_recovery_request(delegated_feedback):
    service, row, run, sent, _ = delegated_feedback
    received(row, "status")
    run["pause"] = {"kind": "worker_request"}
    run["verification_handoff"] = dict(
        version=1, id="handoff-1", request_id="question-1", status="running", commands_count=7
    )
    await service.process(RECEIPT)
    assert "needs an authorized answer" not in sent[0]["body"]
    assert "/forge respond" not in sent[0]["body"]


def test_assistant_card_preserves_unreviewed_reference_without_raw_proposal_json():
    from firefly_forge.collaboration_feedback import feedback_card

    data = dict(
        id=RECEIPT,
        command={"action": "assistant"},
        action_result={
            "status": "completed",
            "reply": "A" * 11000,
            "proposal": {"secret_field": "not-public"},
            "proposal_status": "unreviewed",
            "remote_reference": {"published": True, "url": "https://github.com/team/product/tree/forge-reference"},
        },
    )
    body = feedback_card(data, {}, {"url": "https://github.com/team/product.git"}, "https://github.com", lambda x: x)
    assert len(body) <= 6000 and "unreviewed proposal" in body[:300] and "Reference files:" in body
    assert "not-public" not in body and '"reply"' not in body
    assert "A" * 1200 in body


def test_hostile_question_card_is_bounded_without_cutting_the_receipt():
    from firefly_forge.collaboration_feedback import feedback_card

    run = dict(
        id="2" * 32,
        status="paused",
        worker_request=dict(
            id="question-1",
            status="pending",
            question="<@**" * 4000,
            choices=["<@**" * 1000] * 12,
        ),
    )
    body = feedback_card(dict(id=RECEIPT, command={"action": "status"}), run, {}, "", lambda x: x)
    assert len(body) <= 6000 and f"Receipt: `{RECEIPT}`" in body and body.endswith("</details>")
    assert "<@**" not in body


async def test_cancelled_run_does_not_invite_an_answer_to_a_retained_question(delegated_feedback):
    service, row, run, sent, _ = delegated_feedback
    received(row, "status")
    run["status"] = "cancelled"
    await service.process(RECEIPT)
    assert "The run was cancelled" in sent[0]["body"]
    assert "Decision needed" not in sent[0]["body"] and "/forge respond" not in sent[0]["body"]


async def test_answered_manual_question_does_not_ask_for_the_same_answer(delegated_feedback):
    service, row, run, sent, _ = delegated_feedback
    received(row, "respond")
    run["pause"] = {"kind": "worker_request"}
    await service.process(RECEIPT)
    assert "answer is recorded" in sent[0]["body"]
    assert "needs an authorized answer" not in sent[0]["body"]


async def test_native_pre_admission_refusal_explains_hold_without_claiming_commands_ran(delegated_feedback):
    service, row, run, sent, _ = delegated_feedback
    received(row, "status")
    await service.process(RECEIPT)
    run["verification_refusal"] = dict(
        version=1,
        request_id="question-1",
        request_digest="f" * 64,
        status="held",
        reason="admission_refused",
        message="private diagnostic",
    )
    await service.sync_feedback()
    assert len(sent) == 2
    assert "could not start the requested checks" in sent[-1]["body"]
    assert "An operator needs to review the original question" in sent[-1]["body"]
    assert "Decision needed" in sent[-1]["body"] and "private diagnostic" not in sent[-1]["body"]


async def test_displayed_request_identifiers_remain_exact_copyable_native_values(delegated_feedback):
    service, row, run, sent, _ = delegated_feedback
    received(row, "status")
    run["worker_request"]["id"] = "owner-decision_1"
    await service.process(RECEIPT)
    assert "Request: `owner-decision_1`" in sent[0]["body"]
    assert "owner\\-decision" not in sent[0]["body"]


@pytest.mark.parametrize(
    "status,task,description",
    [
        ("developing", "implement", "Implementing the requested change"),
        ("validating", "verify", "Checking the result"),
    ],
)
async def test_actual_engine_states_render_native_phase_activity(delegated_feedback, status, task, description):
    service, row, run, sent, _ = delegated_feedback
    received(row, "status")
    run.update(status=status, active_task=task, worker_request={})
    await service.process(RECEIPT)
    assert description in sent[0]["body"]
    assert "No current execution outcome" not in sent[0]["body"]


@pytest.mark.parametrize("remote", ["git@github.com:team/product.git", "ssh://git@github.com/team/product.git"])
def test_confirmed_review_link_accepts_the_exact_ssh_repository(remote):
    from firefly_forge.collaboration_feedback import feedback_card

    run = dict(id="2" * 32, status="succeeded", pr_url="https://github.com/team/product/pull/9")
    body = feedback_card(
        dict(id=RECEIPT, command={"action": "status"}), run, {"url": remote}, "https://github.com", lambda x: x
    )
    assert run["pr_url"] in body
    run["pr_url"] = "https://github.com/other/project/pull/9"
    assert run["pr_url"] not in feedback_card(
        dict(id=RECEIPT, command={"action": "status"}), run, {"url": remote}, "https://github.com", lambda x: x
    )


async def test_failed_card_preparation_cannot_reuse_a_previous_delivered_intent(delegated_feedback):
    service, row, run, sent, _ = delegated_feedback
    received(row, "status")
    run.update(status="queued", worker_request={})
    await service.process(RECEIPT)
    previous = copy.deepcopy(row.data["feedback_intent"])
    run.update(status="paused", worker_request=dict(id="question-2", status="pending", question="retained-secret"))

    def reject(value):
        if "retained-secret" in str(value):
            raise ValueError("Credential-bearing card")

    service.store.reject_credentials = reject
    await service.sync_feedback()
    assert len(sent) == 1 and row.data["feedback_status"] == "needs_reconciliation"
    assert row.data["feedback_intent"] is None
    await service.reconcile(RECEIPT, "posted", "Checked the provider's actual outcome explicitly")
    assert len(row.data["feedback_history"]) == 1
    assert row.data["feedback_key"] == previous["key"]


async def test_requeued_partial_work_does_not_claim_it_never_started(delegated_feedback):
    service, row, run, sent, _ = delegated_feedback
    received(row, "status")
    run.update(status="queued", worker_request={"id": "question-1", "status": "answered"}, implementation_rounds=1)
    await service.process(RECEIPT)
    assert "queued for execution" in sent[0]["body"]
    assert "not started" not in sent[0]["body"]
