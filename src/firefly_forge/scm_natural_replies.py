# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Unmentioned replies bind published typed questions without inventing a bot address."""

from __future__ import annotations

import hashlib
import re
from typing import Any, Literal

from pydantic import ConfigDict, Field, model_validator

from firefly_forge.scm_authority_models import Digest, NativeID, StrictContract
from firefly_forge.scm_conversation_models import AddressedStatusV1


class QuestionReference(StrictContract):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True)
    origin: Literal["product_assistant_turn", "worker_journal", "natural_turn"]
    project_id: NativeID
    repository_id: NativeID
    native_user_id: NativeID
    actor: str = Field(pattern=r"^[1-9][0-9]{0,39}$")
    issue: dict[str, Any]
    binding_digest: Digest
    source_receipt_id: NativeID
    question_id: str = Field(min_length=1, max_length=100)
    question_digest: Digest
    conversation_id: NativeID | None = None
    turn_id: NativeID | None = None
    run_id: NativeID | None = None
    publication_comment_id: str = Field(pattern=r"^[1-9][0-9]{0,39}$")
    publication_body_sha256: Digest
    questions: list[str] = Field(min_length=1, max_length=10)
    pending: Literal[True] = True

    @model_validator(mode="after")
    def exact_question(self) -> QuestionReference:
        if (
            set(self.issue) != {"kind", "number"}
            or self.issue["kind"] not in {"issue", "pull_request"}
            or type(self.issue["number"]) is not int
            or self.issue["number"] < 1
            or any(not q.strip() or len(q) > 4000 for q in self.questions)
        ):
            raise ValueError("A retained question requires a bounded exact issue and typed question text")
        if self.origin == "product_assistant_turn":
            if (
                not self.conversation_id
                or not self.turn_id
                or self.run_id
                or self.question_id != "product-" + self.turn_id
            ):
                raise ValueError("A product question must identify its completed native turn")
        elif self.origin == "natural_turn":
            if (
                self.run_id
                or self.conversation_id
                or self.turn_id
                or self.question_id != "natural-" + self.source_receipt_id
            ):
                raise ValueError("A natural question must identify its completed native message receipt")
        elif not self.run_id or self.conversation_id or self.turn_id:
            raise ValueError("A worker question must identify its original native run")
        return self


class RetainedQuestionReplyV1(StrictContract):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True)
    version: Literal[1] = 1
    action: Literal["status"] = "status"
    adapter: Literal["retained_question_reply_v1"] = "retained_question_reply_v1"
    source_kind: Literal["retained_question_reply"] = "retained_question_reply"
    intent: Literal["clarify"] = "clarify"
    source_body_sha256: Digest
    message: str = Field(min_length=1, max_length=12000)
    question_refs: list[QuestionReference] = Field(min_length=1, max_length=200)


def human_message(source: str, message: AddressedStatusV1 | RetainedQuestionReplyV1) -> str:
    if hashlib.sha256(source.encode()).hexdigest() != message.source_body_sha256:
        raise ValueError("The exact natural human source changed")
    if isinstance(message, RetainedQuestionReplyV1):
        if message.message != source:
            raise ValueError("A retained reply cannot substitute human text")
        return source
    direct = re.fullmatch(
        rf"(?:@{re.escape(message.address)}\s*[,.:]?\s*|{re.escape(message.address)},\s*)(.+)",
        source.strip(),
        re.IGNORECASE | re.DOTALL,
    )
    if direct is None or not direct[1].strip() or len(direct[1]) > 12000:
        raise ValueError("Natural intake requires the exact bounded addressed human source")
    return direct[1]


def retained_reply(
    body: str,
    rows: list[dict[str, Any]],
    *,
    project: str,
    repository: str,
    actor: str,
    issue: dict[str, Any],
    binding_digest: str,
    native_user_id: str,
) -> RetainedQuestionReplyV1 | None:
    if len(rows) > 200 or not body.strip() or len(body) > 12000:
        raise ValueError("The retained question reply exceeds its bounded native context")
    scope = dict(
        project_id=project,
        repository_id=repository,
        native_user_id=native_user_id,
        actor=actor,
        issue=issue,
        binding_digest=binding_digest,
    )
    references = [
        QuestionReference.model_validate(row)
        for row in rows
        if row.get("pending") is True and all(row.get(key) == value for key, value in scope.items())
    ]
    references.sort(key=lambda row: (row.origin, row.source_receipt_id, row.question_id))
    identities = {(row.origin, row.question_id, row.question_digest) for row in references}
    if len(identities) != len(references):
        raise ValueError("The retained question context has duplicate identities")
    return (
        RetainedQuestionReplyV1(
            source_body_sha256=hashlib.sha256(body.encode()).hexdigest(), message=body, question_refs=references
        )
        if references
        else None
    )
