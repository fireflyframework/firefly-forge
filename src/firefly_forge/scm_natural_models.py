# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Server-owned task choices for durable natural GitHub conversations."""

from __future__ import annotations

import re
import unicodedata
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

from firefly_forge.journal import digest

Data = dict[str, Any]
SCOPE_FIELDS = ("repository_id", "project_id", "issue", "actor", "binding_digest", "native_user_id")


class GroundedSelection(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    human_span: str = Field(min_length=4, max_length=200)
    question_span: str = Field(min_length=4, max_length=200)


class NaturalDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    intent: Literal["status", "answer", "resume", "clarify", "dialogue", "product"]
    message: str = Field(min_length=1, max_length=12000)
    choice: StrictInt | None = Field(default=None, ge=1, le=200)
    language: Literal["en", "es"] | None = None
    selection_evidence: GroundedSelection | None = None
    questions: list[Annotated[str, Field(min_length=1, max_length=4000)]] = Field(default_factory=list, max_length=10)
    proposal: Data | None = None

    @model_validator(mode="after")
    def unreviewed_dialogue(self) -> NaturalDecision:
        from firefly_forge.scm_natural_render import reply_quote

        if self.intent == "product" and self.questions:
            raise ValueError("A product handoff leaves architectural follow-up questions to the native product service")
        if any(not question.strip() for question in self.questions):
            raise ValueError("Typed questions cannot be empty")
        reply_quote(self.message, self.questions, lambda text: text)
        if self.proposal is not None:
            from firefly_forge.collaboration import ProductDefinition

            proposal = ProductDefinition.model_validate(self.proposal)
            if (
                self.intent not in {"dialogue", "answer"}
                or proposal.ai_origin is not None
                or proposal.functional_reviewed
                or proposal.architecture
                and proposal.architecture.reviewed
                or proposal.design_review
                and proposal.design_review.reviewed
            ):
                raise ValueError("Conversation proposals remain unreviewed and cannot manufacture an AI origin")
            self.proposal = proposal.model_dump(mode="json")
        return self


class NaturalResolution(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    version: Literal[1] = 1
    scope: Data
    state: Literal[
        "status", "answer", "resume", "waiting", "dialogue", "product_answer", "conversation_answer", "product_request"
    ]
    reason: str
    candidates: list[Data] = Field(max_length=200)
    run_id: str | None = None
    question_id: str | None = None
    question_digest: str | None = None
    response_digest: str | None = None
    answer: str | None = None
    conversation_id: str | None = None
    turn_id: str | None = None


def _question(row: Data) -> Data:
    return {key: row.get(key) for key in ("run_id", "question_id", "question_digest", "conversation_id", "turn_id")}


def _human_selection(text: str, rows: list[Data], choice: int, evidence: GroundedSelection | None) -> bool:
    # A typed model number alone cannot resolve ambiguous human consent.
    if evidence is not None:
        return evidence.human_span in text and [
            index for index, row in enumerate(rows, 1) if evidence.question_span in (row.get("question") or "")
        ] == [choice]

    def normalized(value: str) -> str:
        return " ".join(
            "".join(c for c in unicodedata.normalize("NFD", value.casefold()) if not unicodedata.combining(c)).split()
        )

    human = normalized(text)
    ordinals = (
        "first|primera|primero",
        "second|segunda|segundo",
        "third|tercera|tercero",
        "fourth|cuarta|cuarto",
        "fifth|quinta|quinto",
        "sixth|sexta|sexto",
        "seventh|septima|septimo",
        "eighth|octava|octavo",
        "ninth|novena|noveno",
        "tenth|decima|decimo",
    )
    if choice > 10:
        return False
    if re.match(
        rf"^(?:(?:the|la|el|question|pregunta|option|opcion)\s+)?(?:{choice}|{ordinals[choice - 1]})(?=$|[\s.,;:!?])",
        human,
    ):
        return True
    matches = [
        index
        for index, row in enumerate(rows, 1)
        if len(normalized(row.get("question") or "")) >= 12 and normalized(row["question"]) in human
    ]
    return matches == [choice]


def _offered_rows(scope: Data, candidates: list[Data], *, question_only: bool) -> list[Data]:
    if len(candidates) > 200:
        raise ValueError("Natural task context exceeds its bounded lookup")
    rows = sorted(
        [
            row
            for row in candidates
            if all(row.get(key) == scope.get(key) for key in SCOPE_FIELDS)
            and (not question_only or row.get("question_id"))
        ],
        key=lambda row: (row.get("run_id") or "", row.get("question_id") or ""),
    )
    if len({(row.get("run_id"), row.get("question_id")) for row in rows}) != len(rows):
        raise ValueError("Natural task references must identify distinct verified questions or runs")
    return rows


def question_choices(context: Data) -> list[Data]:
    return [
        {"choice": index, **_question(row)}
        for index, row in enumerate(
            _offered_rows(context.get("scope", {}), context.get("candidates", []), question_only=True), 1
        )
    ]


def resolve_task(
    scope: Data,
    candidates: list[Data],
    decision: NaturalDecision,
    *,
    answer: str,
    previous: Data | None = None,
) -> NaturalResolution:
    """A model selects an offered number; only server evidence supplies task identities."""
    rows = _offered_rows(scope, candidates, question_only=decision.intent in {"answer", "resume"})
    result = NaturalResolution(scope=scope, state="waiting", reason="clarification_required", candidates=rows)
    if previous is not None:
        retained = NaturalResolution.model_validate(previous)
        if (
            retained.state == "waiting"
            and retained.candidates
            and (
                retained.scope != scope
                or digest({"questions": [_question(r) for r in retained.candidates]})
                != digest({"questions": [_question(r) for r in rows]})
            )
        ):
            return result.model_copy(update={"reason": "context_changed"})
    if decision.intent == "product":
        return result.model_copy(
            update={"state": "product_request", "reason": "native_product_handoff", "answer": answer}
        )
    if decision.intent == "dialogue":
        return result.model_copy(update={"state": "dialogue", "reason": "discussion_only"})
    if decision.intent == "clarify":
        return result
    choice = decision.choice if decision.choice is not None else (1 if len(rows) == 1 else None)
    if choice is None or choice > len(rows):
        return result
    if decision.selection_evidence is not None and (
        decision.choice is None or not _human_selection(answer, rows, choice, decision.selection_evidence)
    ):
        return result.model_copy(update={"reason": "explicit_human_choice_required"})
    if len(rows) > 1 and (
        (previous is None or retained.state != "waiting")
        and decision.selection_evidence is None
        or not _human_selection(answer, rows, choice, decision.selection_evidence)
    ):
        return result.model_copy(update={"reason": "explicit_human_choice_required"})
    row = rows[choice - 1]
    if row.get("origin") in {"product_assistant_turn", "natural_turn"}:
        if decision.intent == "resume":
            return result.model_copy(update={"reason": "product_answer_has_no_execution_authority"})
        if decision.intent == "answer" and row.get("question_status") == "pending" and row.get("consumed") is False:
            if not answer.strip() or len(answer) > 12000:
                raise ValueError("An answer must retain the bounded original human text")
            return result.model_copy(
                update={
                    "state": "product_answer" if row["origin"] == "product_assistant_turn" else "conversation_answer",
                    "reason": "current_verified_product_question",
                    "question_id": row["question_id"],
                    "question_digest": row["question_digest"],
                    "answer": answer,
                    "conversation_id": row.get("conversation_id"),
                    "turn_id": row.get("turn_id"),
                }
            )
        return result.model_copy(update={"reason": "question_not_pending"})
    if decision.intent == "status":
        return result.model_copy(update={"state": "status", "reason": "current_verified_task", "run_id": row["run_id"]})
    if (
        row.get("run_status") != "paused"
        or row.get("consumed") is not False
        or not row.get("question_id")
        or not row.get("question_digest")
        or row.get("question_status") != ("pending" if decision.intent == "answer" else "answered")
        or decision.intent == "resume"
        and not row.get("response_digest")
    ):
        return result.model_copy(update={"reason": "question_not_pending"})
    if decision.intent == "answer" and (not answer.strip() or len(answer) > 12000):
        raise ValueError("An answer must retain the bounded original human text")
    return result.model_copy(
        update={
            "state": decision.intent,
            "reason": "current_verified_question",
            "run_id": row["run_id"],
            "question_id": row["question_id"],
            "question_digest": row["question_digest"],
            "response_digest": row.get("response_digest"),
            "answer": answer if decision.intent == "answer" else None,
        }
    )
