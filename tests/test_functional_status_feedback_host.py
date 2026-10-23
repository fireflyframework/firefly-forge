# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Status separates the last attempt from a previously completed assistant reply."""

import pytest
from test_functional_assistant_feedback_host import CONVERSATION, TURN, completed
from test_scm_delegated_feedback import delegated_feedback as delegated_feedback

from firefly_forge.channel_products import ChannelProducts
from firefly_forge.communication import CommunicationCard, _render_card, assistant_communication, communication_facts

FAILED = "c" * 32


def status_receipt(latest="failed"):
    from types import SimpleNamespace

    summary = ChannelProducts._conversation_summary(
        {
            "id": CONVERSATION,
            "project_id": "d" * 32,
            "turns": [
                {
                    "id": TURN,
                    "status": "completed",
                    "reply": {"message": "Earlier public response.", "questions": ["Which owner?"]},
                },
                {"id": FAILED, "status": latest, "reply": None, "detail": ""},
            ],
        }
    )
    row = SimpleNamespace()
    completed(row, "status", summary)
    return row


@pytest.mark.parametrize("latest", ["failed", "needs_attention", "needs_reconciliation", "processing"])
def test_actual_status_summary_preserves_prior_reply_and_last_attempt_separately(latest):
    row = status_receipt(latest)
    facts = communication_facts(None, assistant=assistant_communication(row.data))
    actual = facts["assistant"]
    assert actual["status"] == "read"
    assert actual["turns"] == [{"turn_id": TURN, "status": "completed"}, {"turn_id": FAILED, "status": latest}]
    assert actual["latest_turn"] == {"turn_id": FAILED, "status": latest}
    assert actual["retained_reply"]["basis"] == "retained_completed_turn"
    assert actual["retained_reply"]["turn_id"] == TURN
    assert actual["retained_reply"]["reply"] == "Earlier public response."
    assert actual["retained_reply"]["questions"] == ["Which owner?"]
    assert "detail" not in str(actual)


def test_status_public_render_labels_latest_failed_before_retained_completed_quote():
    row = status_receipt()
    facts = communication_facts(None, assistant=assistant_communication(row.data))
    card = CommunicationCard(
        title={"text": "Conversation status", "fact_ids": ["assistant"]},
        paragraphs=[
            {"text": "The latest recorded turn failed. A prior completed reply is retained.", "fact_ids": ["assistant"]}
        ],
    )
    body = _render_card(card, facts, lambda x: x, lambda x: None)
    assert "The latest turn failed" in body
    assert "earlier completed reply" in body
    assert "retained_completed_turn" not in body
    assert "Original result" not in body
    assert body.index("<details>") > body.index("Earlier public response")
    assert body.index(TURN) > body.index("<details>")
    assert body.index(FAILED) > body.index("<details>")
    assert TURN in body and FAILED in body
    assert body.count("Which owner?") == 1
    assert "unknown" not in body.lower()


@pytest.mark.parametrize("change", ["too_many", "duplicate", "unknown_status", "missing_reply"])
def test_unbounded_or_unconfirmed_retained_status_is_refused(change):
    row = status_receipt()
    turns = row.data["action_result"]["turns"]
    if change == "too_many":
        turns.extend([dict(turns[0], turn_id=f"{n:032x}") for n in range(4)])
    elif change == "duplicate":
        turns[1]["turn_id"] = TURN
    elif change == "unknown_status":
        turns[1]["status"] = "succeeded"
    else:
        turns[0]["reply"] = None
    with pytest.raises(ValueError):
        assistant_communication(row.data)


async def test_status_feedback_posts_previous_reply_with_failed_latest_without_model_replay(
    delegated_feedback, tmp_path
):
    from communication_model_fixture import reviewed_model
    from test_communication_writer import configured_store
    from test_scm_delegated_feedback import RECEIPT

    service, row, _, sent, _ = delegated_feedback
    snapshot = status_receipt().data
    row.status = "processed"
    row.data = snapshot
    service.store.task_router = (await configured_store(tmp_path)).task_router

    async def authorize(data, **kwargs):
        return None

    service._feedback_authority = authorize
    calls = []
    service.communication_writer.model = reviewed_model(
        {
            "title": {"text": "Conversation status", "fact_ids": ["assistant"]},
            "paragraphs": [
                {
                    "text": "The latest recorded turn failed. A prior completed reply remains available.",
                    "fact_ids": ["assistant"],
                }
            ],
        },
        calls=calls,
    )
    await service.feedback(RECEIPT)
    assert len(sent) == 1, row.data
    assert row.data["feedback_status"] == "sent"
    assert "The latest turn failed" in sent[0]["body"]
    assert "earlier completed reply" in sent[0]["body"]
    assert "retained_completed_turn" not in sent[0]["body"]
    assert "Original result" not in sent[0]["body"]
    assert "Earlier public response" in sent[0]["body"]
    assert sent[0]["body"].count("Which owner?") == 1
    assert [phase for phase, _, _ in calls] == ["draft", "review"]
    assert all(info.function_tools == [] for _, _, info in calls)
