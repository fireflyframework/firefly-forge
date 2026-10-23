# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Authenticate bounded historical question/answer payloads, never new authority."""

from __future__ import annotations

import copy
import json
import re
from typing import Any

from firefly_forge.agent_roles import WorkerResponse, worker_feedback_model
from firefly_forge.journal import ZERO_HASH, digest

Data = dict[str, Any]
MESSAGE_KINDS = frozenset({"worker_feedback", "worker_response"})
MAX_MESSAGES, MAX_BYTES, MAX_JOURNAL_ROWS = 64, 262144, 10000


def _actor(response: Data) -> Data:
    actor = response.get("responding_actor", {})
    if response.get("source") == "factory_verification":
        expected = {
            "version",
            "authentication_method",
            "handoff_id",
            "source_digest",
            "commands_digest",
            "policy_digest",
            "provider_digest",
        }
        if (
            set(actor) != expected
            or type(actor.get("version")) is not int
            or actor["version"] != 4
            or actor.get("authentication_method") != "factory_verification"
            or actor.get("handoff_id") != response.get("verification_handoff_id")
            or actor.get("handoff_id") != response.get("id")
            or any(
                not isinstance(actor.get(k), str) or not re.fullmatch(r"[a-f0-9]{64}", actor[k])
                for k in ("source_digest", "commands_digest", "policy_digest", "provider_digest")
            )
        ):
            raise ValueError("Historical verification answer has inconsistent native provenance")
    elif type(actor.get("version")) is int and actor["version"] == 1:
        if (
            set(actor) != {"version", "authentication_method", "user_id"}
            or actor.get("authentication_method") not in {"local_bootstrap", "oidc_session", "oidc_bearer"}
            or not isinstance(actor.get("user_id"), str)
            or not re.fullmatch(r"[a-f0-9]{32}", actor["user_id"])
        ):
            raise ValueError("Historical answer has no exact authenticated native actor")
    else:
        from firefly_forge.scm_authority_models import SCMRespondingActorV2
        from firefly_forge.scm_poll_models import SCMRespondingActorV3

        if type(actor.get("version")) is not int or actor["version"] not in {2, 3}:
            raise ValueError("Historical answer has no authenticated responding actor")
        (SCMRespondingActorV2 if actor["version"] == 2 else SCMRespondingActorV3).model_validate(actor)
    return {"authentication_method": actor["authentication_method"], "provenance_digest": digest(actor)}


def implementation_context(run: Data, snapshot: Data) -> Data:
    headers, entries = snapshot["headers"], snapshot["entries"]
    if (
        type(snapshot["journal_rows"]) is not int
        or type(snapshot["message_rows"]) is not int
        or len(headers) != snapshot["journal_rows"]
        or len(headers) > MAX_JOURNAL_ROWS
        or len(entries) != snapshot["message_rows"]
        or len(entries) > MAX_MESSAGES
        or len(json.dumps(entries, ensure_ascii=False).encode()) > MAX_BYTES
    ):
        raise ValueError("Retained worker context exceeds its bounded native projection")
    previous, identities = ZERO_HASH, set()
    for index, header in enumerate(headers, 1):
        if not isinstance(header, (list, tuple)) or len(header) != 4:
            raise ValueError("Retained worker context has malformed journal headers")
        sequence, parent, value, kind = header
        if (
            type(sequence) is not int
            or sequence != index
            or parent != previous
            or not isinstance(value, str)
            or not re.fullmatch(r"[a-f0-9]{64}", value)
        ):
            raise ValueError("Retained worker context has inconsistent journal header links")
        previous = value
        identities.add((sequence, parent, value, kind))
    if len(entries) != sum(h[3] in MESSAGE_KINDS for h in headers):
        raise ValueError("Retained worker projection omitted a native message")
    questions: Data = {}
    answers: Data = {}
    pairs = []
    last_sequence = 0
    for entry in entries:
        if (
            type(entry.get("sequence")) is not int
            or entry.get("run_id") != run["id"]
            or entry.get("kind") not in MESSAGE_KINDS
            or entry.get("sequence", 0) <= last_sequence
            or (entry.get("sequence"), entry.get("previous_hash"), entry.get("digest"), entry.get("kind"))
            not in identities
            or digest({k: v for k, v in entry.items() if k != "digest"}) != entry.get("digest")
        ):
            raise ValueError("Retained worker payload differs from its exact native journal header")
        last_sequence = entry["sequence"]
        payload = entry["payload"]
        if entry["kind"] == "worker_feedback":
            feedback = worker_feedback_model(payload).model_validate(payload)
            if feedback.task_id != entry["task_id"]:
                raise ValueError("Historical feedback identifies a different native task")
            if feedback.request:
                if feedback.request.id in questions:
                    raise ValueError("Retained worker question identity is ambiguous")
                questions[feedback.request.id] = entry
            continue
        allowed = {"id", "actor", "answer", "request_id", "responding_actor"}
        if payload.get("source") == "factory_verification":
            allowed |= {"source", "verification_handoff_id"}
        if set(payload) != allowed:
            raise ValueError("Historical answer has unexpected fields")
        WorkerResponse.model_validate({k: payload[k] for k in ("id", "actor", "answer")})
        actor = _actor(payload)
        request_id = payload["request_id"]
        question = questions.get(request_id)
        if question is None or request_id in answers or question["task_id"] != entry["task_id"]:
            raise ValueError("Retained answer lacks its unique earlier native question")
        answers[request_id] = entry
        pairs.append(
            {
                "request_id": request_id,
                "task_id": entry["task_id"],
                "attempt": question["payload"]["attempt"],
                "question": copy.deepcopy(question["payload"]["request"]),
                "answer": payload["answer"],
                "answer_source": payload.get("source", "authenticated_operator"),
                "actor": actor,
                "feedback": {k: question[k] for k in ("sequence", "digest", "created_at")},
                "response": {k: entry[k] for k in ("sequence", "digest", "created_at")},
            }
        )
    latest = run.get("worker_response")
    current = (
        answers.get(latest["request_id"])
        if isinstance(latest, dict) and isinstance(latest.get("request_id"), str)
        else None
    )
    if latest and (current is None or current["payload"] != latest):
        raise ValueError("Current worker response differs from its immutable historical answer")
    return {
        "version": 1,
        "kind": "historical_worker_question_answers",
        "run_id": run["id"],
        "history_head": previous,
        "current_response_digest": current["digest"] if current else None,
        "historical_only": True,
        "grants_authority": False,
        "pairs": pairs,
    }
