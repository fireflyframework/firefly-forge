# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Question identities come from typed native producers and confirmed published receipts."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from firefly_forge.communication import assistant_communication, communication_facts
from firefly_forge.journal import digest
from firefly_forge.product_assistant_runtime import AssistantReply, FunctionalAssistantReply
from firefly_forge.scm_natural_models import NaturalDecision

Data = dict[str, Any]


def confirmed_publication(receipt: Data) -> Data:
    if (
        receipt.get("status") != "processed"
        or receipt.get("action_status") != "completed"
        or receipt.get("feedback_status") != "sent"
    ):
        raise ValueError("Only a confirmed published reply can address a pending question")
    remote = receipt.get("feedback_remote") or {}
    comment = remote.get("id")
    if type(comment) not in {int, str} or not re.fullmatch(r"[1-9][0-9]{0,39}", str(comment)):
        raise ValueError("The confirmed publication has no exact remote comment identity")
    intent = receipt.get("feedback_intent") or {}
    body = intent.get("body")
    if (
        not isinstance(body, str)
        or not body
        or len(body) > 6000
        or hashlib.sha256(body.encode()).hexdigest() != intent.get("body_sha256")
    ):
        raise ValueError("The exact published question text no longer matches its retained intent")
    return {"receipt_id": receipt["id"], "comment_id": str(comment), "body_sha256": intent["body_sha256"]}


def product_questions(
    receipt: Data,
    session: Data,
    turn: Data,
    events: list[Data],
    *,
    repository: Data | None = None,
    host: str = "",
) -> Data | None:
    publication = confirmed_publication(receipt)
    if (
        turn.get("status") != "completed"
        or turn.get("session_id") != session["id"]
        or session["repository_id"] != receipt["repository_id"]
        or not session.get("project_id")
        or len(events) > 300
        or len(json.dumps(events).encode()) > 4 * 1024 * 1024
    ):
        raise ValueError("The completed native product-question producer is unavailable or outside this repository")
    previous, completions = "0" * 64, []
    for index, event in enumerate(events, 1):
        if (
            type(event.get("sequence")) is not int
            or event["sequence"] != index
            or event.get("previous") != previous
            or digest({key: value for key, value in event.items() if key != "digest"}) != event.get("digest")
        ):
            raise ValueError("The product question's immutable producer journal changed")
        previous = event["digest"]
        if event["kind"] == "turn_completed" and event["payload"].get("turn_id") == turn["id"]:
            completions.append(event)
    if (
        previous != session.get("journal_head")
        or len(events) != session.get("journal_sequence")
        or len(completions) != 1
    ):
        raise ValueError("The exact completed product-question producer journal is incomplete")
    reply_type = FunctionalAssistantReply if "intent" in (turn.get("reply") or {}) else AssistantReply
    reply = reply_type.model_validate(turn["reply"])
    if completions[0]["payload"].get("reply") != turn["reply"]:
        raise ValueError("The product questions differ from their immutable completed reply")
    assistant = assistant_communication(receipt, repository=repository, host=host)
    if assistant is None or assistant.conversation_id != session["id"]:
        raise ValueError("The published reply does not identify this exact product conversation")
    published = assistant.retained_reply if assistant.operation == "status" else assistant
    if (
        published is None
        or published.turn_id != turn["id"]
        or published.questions != reply.questions
        or published.reply != reply.message
    ):
        raise ValueError("The publication does not bind this exact completed typed question list")
    expected = communication_facts(None, assistant=assistant)
    if receipt["feedback_intent"].get("communication", {}).get("facts") != expected:
        raise ValueError("The confirmed question publication no longer matches its typed producer facts")
    if not reply.questions:
        return None
    reference = {
        "origin": "product_assistant_turn",
        "conversation_id": session["id"],
        "turn_id": turn["id"],
        "project_id": session["project_id"],
        "questions": reply.questions,
        "producer_digest": completions[0]["digest"],
        "publication": publication,
    }
    return {
        **reference,
        "question_id": "product-" + turn["id"],
        "question_digest": digest(reference),
        "question": "\n".join(f"{index}. {question}" for index, question in enumerate(reply.questions, 1)),
        "question_status": "pending",
        "run_status": None,
        "run_id": None,
        "consumed": False,
        "proposal": reply.proposal.model_dump(mode="json") if reply.proposal else None,
        "reply": reply.message,
    }


def natural_producer(receipt: Data) -> Data:
    turn = receipt["natural_turn"]
    decision = NaturalDecision.model_validate(turn["decision"])
    scope = turn["context"]["scope"]
    if (
        turn.get("classifier_outcome") != "confirmed_valid"
        or decision.message != turn.get("reply")
        or turn.get("source_digest") != receipt["digest"]
        or any(scope.get(key) != receipt.get(key) for key in ("repository_id", "actor", "issue"))
        or not re.fullmatch(r"[a-f0-9]{64}", turn.get("policy_digest", ""))
    ):
        raise ValueError("The typed natural question does not match its completed native producer")
    return {
        "receipt_id": receipt["id"],
        "scope": scope,
        "source_digest": turn["source_digest"],
        "policy_digest": turn["policy_digest"],
        "decision": decision.model_dump(mode="json"),
        "human_message": turn["human_message"],
        "resolution": turn.get("resolution"),
        "model_selection": turn.get("model_selection"),
    }


def natural_questions(receipt: Data) -> Data | None:
    publication = confirmed_publication(receipt)
    producer = natural_producer(receipt)
    if receipt["feedback_intent"].get("natural_turn_digest") != digest(producer):
        raise ValueError("The typed natural questions changed after their confirmed publication")
    decision = producer["decision"]
    resolution = receipt["natural_turn"].get("resolution") or {}
    if not decision["questions"] or resolution.get("state") == "waiting" and resolution.get("candidates"):
        return None
    reference = {
        "origin": "natural_turn",
        "question_id": "natural-" + receipt["id"],
        "producer_digest": digest(producer),
        "questions": decision["questions"],
        "publication": publication,
    }
    return {
        **producer["scope"],
        **reference,
        "question_digest": digest(reference),
        "question": "\n".join(f"{index}. {question}" for index, question in enumerate(decision["questions"], 1)),
        "question_status": "pending",
        "run_status": None,
        "run_id": None,
        "consumed": False,
        "reply": decision["message"],
        "proposal": decision["proposal"],
    }
