# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Long replies retain complete native facts and an admitted full-reference link."""

import pytest
from communication_model_fixture import reviewed_model
from pydantic_ai.messages import ModelResponse, ToolCallPart
from test_communication_writer import configured_store
from test_functional_assistant_feedback_host import CONVERSATION, TURN, completed
from test_scm_delegated_feedback import RECEIPT
from test_scm_delegated_feedback import delegated_feedback as delegated_feedback

from firefly_forge.product_provenance import content_digest

REPLY = "A" * 11000
REFERENCE = {
    "published": True,
    "url": "https://github.com/team/product/tree/forge/references/" + "d" * 64,
    "digest": "d" * 64,
}


async def prepare(fixture, tmp_path, configured):
    service, row, _, sent, _ = fixture
    result = dict(
        conversation_id=CONVERSATION,
        turn_id=TURN,
        status="completed",
        reply=REPLY,
        questions=["Which owner approves the proposal?"],
        proposal_reviewed=False,
        proposal_status="unreviewed",
        remote_reference=dict(REFERENCE),
    )
    completed(row, "turn", result)
    service.scm._host = lambda _: "https://github.com"

    async def authorize(data, **kwargs):
        assert data["id"] == RECEIPT

    service._feedback_authority = authorize
    if configured:
        service.store.task_router = (await configured_store(tmp_path)).task_router
    return service, row, result, sent


def draft(messages, info):
    assert REPLY in str(messages), "The writer must receive the complete admitted reply"
    assert "reply_reference_digest" in str(messages)
    return ModelResponse(
        parts=[
            ToolCallPart(
                info.output_tools[0].name,
                {
                    "title": {"text": "Unreviewed proposal", "fact_ids": ["assistant"]},
                    "paragraphs": [
                        {"text": "The confirmed reply is retained for owner review.", "fact_ids": ["assistant"]}
                    ],
                },
            )
        ]
    )


@pytest.mark.parametrize("configured", [False, True])
async def test_long_reply_keeps_full_facts_and_explicit_excerpt_with_validated_reference(
    delegated_feedback, tmp_path, configured
):
    service, row, result, sent = await prepare(delegated_feedback, tmp_path, configured)
    service.communication_writer.model = reviewed_model(draft)
    await service.feedback(RECEIPT)
    assert len(sent) == 1, row.data
    body = sent[0]["body"]
    assert len(body) <= 6000 and "Reply excerpt" in body
    assert "unreviewed proposal" in body[:300]
    assert "Reference files:" in body and REFERENCE["url"] in body
    assert result["reply"] == REPLY
    facts = row.data["feedback_intent"]["communication"]["facts"]["assistant"]
    assert facts["reply"] == REPLY and facts["questions"] == result["questions"]
    assert facts["reply_reference_digest"] == content_digest(REFERENCE)
    assert row.data["feedback_status"] == "sent"


@pytest.mark.parametrize(
    "invalid", ["foreign_host", "foreign_repository", "unpublished", "bad_digest", "wrong_branch_digest"]
)
async def test_long_reply_cannot_substitute_an_unadmitted_reference(delegated_feedback, tmp_path, invalid):
    service, row, result, sent = await prepare(delegated_feedback, tmp_path, True)
    reference = result["remote_reference"]
    if invalid == "foreign_host":
        reference["url"] = reference["url"].replace("github.com", "foreign.example")
    elif invalid == "foreign_repository":
        reference["url"] = reference["url"].replace("team/product", "team/other")
    elif invalid == "unpublished":
        reference["published"] = False
    elif invalid == "bad_digest":
        reference["digest"] = "not-a-retained-digest"
    else:
        reference["digest"] = "e" * 64
    calls = []

    def unexpected(*args):
        calls.append(1)
        raise AssertionError("An unadmitted reference cannot authorize an oversized model card")

    service.communication_writer.model = reviewed_model(unexpected)
    await service.feedback(RECEIPT)
    assert not sent
    assert not calls
    assert not (row.data.get("feedback_intent") or {}).get("body")
    assert row.data["feedback_status"] in {"held", "needs_reconciliation"}
    assert result["reply"] == REPLY


async def test_selected_ai_failure_cannot_fall_back_to_referenced_recorded_copy(delegated_feedback, tmp_path):
    service, row, _, sent = await prepare(delegated_feedback, tmp_path, True)
    calls = []

    def unavailable(messages, info):
        calls.append(1)
        raise RuntimeError("Controlled selected-model failure")

    service.communication_writer.model = reviewed_model(unavailable)
    await service.feedback(RECEIPT)
    assert len(calls) == 1 and not sent
    assert row.data["feedback_status"] == "held"
    assert row.data["feedback_intent"]["body"] == ""


@pytest.mark.parametrize("when", ["draft", "before_post"])
async def test_reference_change_after_await_cannot_publish_the_previous_excerpt(delegated_feedback, tmp_path, when):
    service, row, result, sent = await prepare(delegated_feedback, tmp_path, True)

    def change_reference():
        result["remote_reference"] = {
            **REFERENCE,
            "url": REFERENCE["url"].replace("d" * 64, "e" * 64),
            "digest": "e" * 64,
        }

    def generate(messages, info):
        response = draft(messages, info)
        if when == "draft":
            change_reference()
        return response

    if when == "before_post":
        original = service._remote_write

        async def remote(*args, **kwargs):
            change_reference()
            return await original(*args, **kwargs)

        service._remote_write = remote
    service.communication_writer.model = reviewed_model(generate)
    await service.feedback(RECEIPT)
    assert not sent
    assert result["remote_reference"]["digest"] == "e" * 64
    assert row.data["feedback_status"] == ("held" if when == "draft" else "needs_reconciliation")
    if when == "draft":
        assert row.data["feedback_intent"]["body"] == ""
