# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Bound human clarification continuations to immutable question, answer and resume records."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from firefly_forge.agent_roles import WorkerFeedback, WorkerResponse, role_digest

Data = dict[str, Any]


class _Actor(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    user_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    authentication_method: Literal["oidc_session", "oidc_bearer", "local_bootstrap", "scm_mapping"]


class _Link(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    version: Literal[1]
    request_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}$")
    feedback_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    response_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    from_attempt: int = Field(ge=1, le=3)
    to_attempt: int = Field(ge=2, le=4)
    resuming_actor: _Actor

    @field_validator("version", mode="before")
    @classmethod
    def exact_version(cls, value: Any) -> int:
        if type(value) is not int or value != 1:
            raise ValueError("Human continuation version must be exact integer one")
        return value

    @model_validator(mode="after")
    def one_successor(self) -> _Link:
        if type(self.version) is not int or self.to_attempt != self.from_attempt + 1:
            raise ValueError("A human clarification permits only its next ordinary implementation attempt")
        return self


def _limit(run: Data) -> int:
    snapshot = run.get("role_snapshot") or {}
    if snapshot and snapshot.get("digest") != role_digest({k: v for k, v in snapshot.items() if k != "digest"}):
        raise ValueError("Human clarification requires the frozen role snapshot")
    limit = snapshot.get("max_requests", 3)
    if type(limit) is not int or not 1 <= limit <= 3:
        raise ValueError("Human clarification requires its finite frozen request bound")
    return limit


def _pair(entries: list[Data], link: _Link) -> tuple[WorkerFeedback, Data]:
    feedback = [e for e in entries if e["kind"] == "worker_feedback" and e["digest"] == link.feedback_digest]
    responses = [e for e in entries if e["kind"] == "worker_response" and e["digest"] == link.response_digest]
    if len(feedback) != 1 or len(responses) != 1:
        raise ValueError("Human clarification lacks an exact immutable question or answer")
    question = WorkerFeedback.model_validate(feedback[0]["payload"])
    response = responses[0]["payload"]
    WorkerResponse.model_validate({k: v for k, v in response.items() if k not in {"request_id", "responding_actor"}})
    actor = response.get("responding_actor", {})
    if type(actor.get("version")) is not int or actor["version"] not in {1, 2, 3}:
        raise ValueError("Human clarification requires an authenticated human answer")
    _Actor.model_validate({k: actor.get(k) for k in ("user_id", "authentication_method")})
    if actor["version"] == 1:
        if (
            set(actor) != {"version", "user_id", "authentication_method"}
            or actor["authentication_method"] == "scm_mapping"
        ):
            raise ValueError("Human clarification native answer identity is inconsistent")
    else:
        from firefly_forge.scm_authority_models import SCMRespondingActorV2
        from firefly_forge.scm_poll_models import SCMRespondingActorV3

        (SCMRespondingActorV2 if actor["version"] == 2 else SCMRespondingActorV3).model_validate(actor)
    if (
        question.request is None
        or question.request.id != link.request_id
        or question.role_id != "implementer"
        or question.task_id != "implement"
        or question.attempt != link.from_attempt
        or response.get("request_id") != link.request_id
        or feedback[0]["task_id"] != "implement"
        or responses[0]["task_id"] != "implement"
        or feedback[0]["sequence"] >= responses[0]["sequence"]
    ):
        raise ValueError("Human clarification records do not identify the same implementation question")
    return question, response


def continuation_allowance(run: Data, entries: list[Data]) -> int:
    links = []
    for entry in entries:
        payload = entry["payload"]
        if entry["kind"] not in {"resume", "handoff"} or "human_clarification" not in payload:
            continue
        link = _Link.model_validate(payload["human_clarification"])
        if payload.get("automatic") is not False:
            raise ValueError("A human clarification requires explicit operator resume")
        _pair(entries, link)
        response = next(e for e in entries if e["digest"] == link.response_digest)
        if response["sequence"] >= entry["sequence"]:
            raise ValueError("Human clarification resume must follow its answer")
        links.append(link)
    if (
        len(links) > _limit(run)
        or len({link.request_id for link in links}) != len(links)
        or len({link.response_digest for link in links}) != len(links)
        or len({link.to_attempt for link in links}) != len(links)
    ):
        raise ValueError("Human clarification continuation is duplicated or exceeds its finite request bound")
    return len(links)


def awaiting_human_resume(run: Data) -> bool:
    request = run.get("worker_request", {})
    return bool(
        run.get("pause", {}).get("kind") == "worker_request"
        and request.get("status") == "answered"
        and request.get("role_id") == "implementer"
        and request.get("task_id") == "implement"
    )


def continuation_link(run: Data, task: Data, entries: list[Data], actor: Data) -> Data | None:
    if not awaiting_human_resume(run):
        return None
    request = run["worker_request"]
    responses = [
        e for e in entries if e["kind"] == "worker_response" and e["payload"].get("request_id") == request["id"]
    ]
    if len(responses) != 1:
        raise ValueError("Human clarification answer is missing or ambiguous")
    if responses[0]["payload"].get("responding_actor", {}).get("authentication_method") == "factory_verification":
        return None
    if (run.get("provider_recovery") or {}).get("status") not in {None, "completed"}:
        raise ValueError("An active provider recovery cannot grant a human clarification continuation")
    if task["status"] != "paused" or task["attempt"] >= 4 or run.get("cancel_request", {}).get("status") == "requested":
        raise ValueError("Human clarification cannot exceed four ordinary attempts or continue cancelled work")
    feedback = [
        e
        for e in entries
        if e["kind"] == "worker_feedback" and (e["payload"].get("request") or {}).get("id") == request["id"]
    ]
    if len(feedback) != 1:
        raise ValueError("Human clarification question is missing or ambiguous")
    link = _Link(
        version=1,
        request_id=request["id"],
        feedback_digest=feedback[0]["digest"],
        response_digest=responses[0]["digest"],
        from_attempt=task["attempt"],
        to_attempt=task["attempt"] + 1,
        resuming_actor=_Actor.model_validate(actor),
    )
    question, _ = _pair(entries, link)
    assert question.request is not None
    expected = {
        **question.request.model_dump(),
        "status": "answered",
        "role_id": "implementer",
        "task_id": "implement",
        "attempt": question.attempt,
    }
    if request != expected or continuation_allowance(run, entries) >= _limit(run):
        raise ValueError("Human clarification request changed or exhausted its continuation bound")
    if any(
        e["kind"] in {"resume", "handoff"}
        and e["payload"].get("human_clarification", {}).get("request_id") == request["id"]
        for e in entries
    ):
        raise ValueError("The human clarification answer has already admitted a continuation")
    return link.model_dump(mode="json")
