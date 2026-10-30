# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Optional, one-shot AI wording over a bounded native communication fact packet."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import Callable
from datetime import datetime
from typing import Annotated, Any, Literal
from urllib.parse import unquote, urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

from firefly_forge.collaboration_feedback import STAGES, TERMINAL, _link, _text, provider_recovery, verification_handoff
from firefly_forge.community_skills import community_catalog
from firefly_forge.product_provenance import content_digest
from firefly_forge.prompts import prompt_text
from firefly_forge.scm_authority_models import StrictContract
from firefly_forge.scm_conversation_models import SCM_SOURCE_QUESTION_LIMIT
from firefly_forge.skills import inject_skills

Data = dict[str, Any]


class CommunicationParagraph(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)
    text: str = Field(min_length=1, max_length=600)
    fact_ids: list[str] = Field(min_length=1, max_length=4)
    heading: str | None = Field(default=None, min_length=1, max_length=100)
    format: Literal["paragraph", "bullet"] = "paragraph"
    diagnostic_id: str | None = Field(default=None, pattern=r"^check-[1-9][0-9]*-(?:command|output)$")


class CommunicationDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    paragraphs: list[CommunicationParagraph] = Field(min_length=1, max_length=6)


class CommunicationDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    question: str = Field(min_length=1, max_length=600)
    context: str = Field(min_length=1, max_length=600)
    choices: list[str] = Field(max_length=6)


class CommunicationCard(CommunicationDraft):
    title: CommunicationParagraph
    decision: CommunicationDecision | None = None


class CommunicationReaderChecks(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    answers_question: bool = Field(description="Directly answers the current human question.")
    relevant_detail: bool = Field(description="Includes useful context and omits irrelevant recaps or counters.")
    no_redundancy: bool = Field(description="Does not repeat the title, result, check provenance or caveats.")
    plain_language: bool = Field(description="Reads as an ordinary teammate reply rather than an internal audit.")
    useful_excerpts: bool = Field(description="Any selected excerpts help answer the question; none are decorative.")


class CommunicationReview(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)
    verdict: Literal["supported", "unsupported", "uncertain"]
    reader_checks: CommunicationReaderChecks
    issues: list[
        Literal[
            "unsupported_claim",
            "contradictory_state",
            "misleading_checks",
            "unrecorded_action",
            "changed_decision",
            "insufficient_context",
            "unclear_language",
            "poor_structure",
            "uncertain",
        ]
    ] = Field(max_length=8)
    fact_ids: list[str] = Field(min_length=1, max_length=12)
    rationale: str = Field(min_length=1, max_length=600)


class CommunicationConversationV1(StrictContract):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    version: Literal[1] = 1
    selection: Literal["run", "missing", "ambiguous", "clarify"]
    language: Literal["en", "es"]
    linked_runs: int = Field(ge=0, le=200)
    question: str | None = Field(default=None, min_length=1, max_length=SCM_SOURCE_QUESTION_LIMIT)
    question_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def consistent_selection(self) -> CommunicationConversationV1:
        if (self.question is None) != (self.question_sha256 is None) or (
            self.question is not None and hashlib.sha256(self.question.encode()).hexdigest() != self.question_sha256
        ):
            raise ValueError("Conversation question requires its exact source digest")
        expected = 1 if self.selection == "run" else 0
        if (self.selection == "ambiguous" and self.linked_runs < 2) or (
            self.selection != "ambiguous" and self.linked_runs != expected
        ):
            raise ValueError("Conversation selection and linked execution count disagree")
        return self


class CommunicationAudienceV1(StrictContract):
    version: Literal[1] = 1
    source: Literal["verified_maintain_owner"]
    source_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    quotation: str = Field(min_length=1)

    @model_validator(mode="after")
    def exact_source(self) -> CommunicationAudienceV1:
        if hashlib.sha256(self.quotation.encode()).hexdigest() != self.source_sha256:
            raise ValueError("Communication audience requires its exact verified owner quotation")
        return self


class CommunicationPublicationV1(StrictContract):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    version: Literal[1] = 1
    summary: str = Field(min_length=1, max_length=6000)
    source_tree: str = Field(pattern=r"^(?:[a-f0-9]{40}|[a-f0-9]{64})$")
    checkpoint_revision: str = Field(pattern=r"^(?:[a-f0-9]{40}|[a-f0-9]{64})$")


class CommunicationGitDeliveryV1(StrictContract):
    version: Literal[1] = 1
    checkpoint_revision: str = Field(pattern=r"^(?:[a-f0-9]{40}|[a-f0-9]{64})$")
    source_tree: str = Field(pattern=r"^(?:[a-f0-9]{40}|[a-f0-9]{64})$")
    sequence: int = Field(ge=1, le=50)
    changed_file_count: int = Field(ge=1)
    implementation_disposition: Literal["failed"] | None = None
    changed_files: list[str] = Field(default_factory=list)
    changed_files_preview_truncated: bool = True

    @model_validator(mode="after")
    def exact_source_preview(self) -> CommunicationGitDeliveryV1:
        from pathlib import PurePosixPath

        from firefly_forge.workspace_tools import _private_name

        if len(set(self.changed_files)) != len(self.changed_files) or len(self.changed_files) > self.changed_file_count:
            raise ValueError("Source path preview differs from the native checkpoint count")
        if not self.changed_files_preview_truncated and len(self.changed_files) != self.changed_file_count:
            raise ValueError("Complete source preview requires every checkpoint path")
        for name in self.changed_files:
            path = PurePosixPath(name)
            if (
                not name
                or not path.parts
                or any(ord(c) < 32 for c in name)
                or "\\" in name
                or ":" in name
                or path.is_absolute()
                or path.as_posix() != name
                or any(part in {".", ".."} or _private_name(part) for part in path.parts)
            ):
                raise ValueError("Communication source paths must be exact public repository paths")
        return self


class CommunicationAssistantTurnV1(StrictContract):
    turn_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    status: Literal["processing", "completed", "failed", "needs_attention", "needs_reconciliation"]


class CommunicationAssistantRetainedReplyV1(StrictContract):
    basis: Literal["retained_completed_turn"] = "retained_completed_turn"
    turn_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    reply: str = Field(min_length=1, max_length=12000)
    questions: list[Annotated[str, Field(min_length=1, max_length=12000)]] = Field(default_factory=list, max_length=10)


class CommunicationAssistantV1(StrictContract):
    version: Literal[1] = 1
    operation: Literal["start", "status", "turn"]
    status: Literal["conversation_created", "read", "completed"]
    conversation_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")
    turn_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")
    model_invoked: bool | None = None
    reply: str | None = Field(default=None, min_length=1, max_length=12000)
    questions: list[Annotated[str, Field(min_length=1, max_length=12000)]] = Field(default_factory=list, max_length=10)
    proposal_status: Literal["unreviewed"] | None = None
    conversation_count: int | None = Field(default=None, ge=0, le=20)
    turn_count: int | None = Field(default=None, ge=0, le=5)
    turns: list[CommunicationAssistantTurnV1] = Field(default_factory=list, max_length=5)
    retained_reply: CommunicationAssistantRetainedReplyV1 | None = None
    reply_reference_digest: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def confirmed_operation(self) -> CommunicationAssistantV1:
        if self.operation == "start":
            valid = bool(self.status == "conversation_created" and self.conversation_id and self.model_invoked is False)
        elif self.operation == "turn":
            valid = bool(self.status == "completed" and self.conversation_id and self.turn_id and self.reply)
        else:
            valid = self.status == "read" and (
                (self.conversation_id is not None and self.turn_count is not None)
                or self.conversation_count is not None
            )
        if not valid or (
            self.operation != "turn" and (self.turn_id or self.reply or self.questions or self.proposal_status)
        ):
            raise ValueError("Assistant communication requires its confirmed operation outcome")
        if self.operation != "start" and self.model_invoked is not None:
            raise ValueError("Only a confirmed model-free start records model invocation facts")
        if self.operation != "status" and (self.conversation_count is not None or self.turn_count is not None):
            raise ValueError("Only assistant status communication can summarize retained conversations")
        if self.operation != "status" and (self.turns or self.retained_reply):
            raise ValueError("Only a status read can quote a retained completed turn")
        if self.reply_reference_digest and (self.operation != "turn" or self.proposal_status != "unreviewed"):
            raise ValueError("Only a confirmed unreviewed proposal can use its retained reply reference")
        if self.operation == "status":
            if self.conversation_id is not None and self.turn_count != len(self.turns):
                raise ValueError("Assistant status must retain every bounded turn state")
            if len({turn.turn_id for turn in self.turns}) != len(self.turns):
                raise ValueError("Assistant status contains duplicate retained turn identities")
            completed = next((turn for turn in reversed(self.turns) if turn.status == "completed"), None)
            if (completed is None) != (self.retained_reply is None) or (
                completed and self.retained_reply and completed.turn_id != self.retained_reply.turn_id
            ):
                raise ValueError("Retained assistant output must belong to the last completed turn")
        return self


def assistant_communication(
    data: Data, *, repository: Data | None = None, host: str = ""
) -> CommunicationAssistantV1 | None:
    command = data.get("command") or {}
    if command.get("action") != "assistant":
        return None
    if data.get("status") != "processed" or data.get("action_status") != "completed":
        raise ValueError("Only a confirmed assistant action can supply communication facts")
    result = data["action_result"]
    operation = command.get("operation")
    values: Data = {"operation": operation, "conversation_id": result.get("conversation_id")}
    if operation == "start":
        values.update(status=result.get("status"), model_invoked=result.get("model_invoked"))
    elif operation == "turn":
        if result.get("proposal_reviewed") is not False:
            raise ValueError("Assistant feedback cannot claim product approval")
        values.update(
            status=result.get("status"),
            turn_id=result.get("turn_id"),
            reply=result.get("reply"),
            questions=result.get("questions", []),
            proposal_status=result.get("proposal_status"),
        )
        reference = result.get("remote_reference")
        if isinstance(reference, dict) and reference.get("published") is True:
            url = _link(reference.get("url"), host, repository or {}, reference=True)
            digest = reference.get("digest")
            if url and isinstance(digest, str) and re.fullmatch(r"[a-f0-9]{64}", digest):
                path = unquote(urlsplit(url).path)
                if path.endswith("/forge-reference") or path.endswith("/forge/references/" + digest):
                    values["reply_reference_digest"] = content_digest({"published": True, "url": url, "digest": digest})
    else:
        values.update(status="read")
        if "conversation_id" in result:
            if not isinstance(result.get("turns"), list):
                raise ValueError("Assistant status requires its retained turn list")
            if not all(isinstance(turn, dict) for turn in result["turns"]):
                raise ValueError("Assistant status requires typed retained turn states")
            values["turn_count"] = len(result["turns"])
            values["turns"] = [
                {"turn_id": turn.get("turn_id"), "status": turn.get("status")} for turn in result["turns"]
            ]
            completed = next((turn for turn in reversed(result["turns"]) if turn.get("status") == "completed"), None)
            if completed:
                if completed.get("proposal_reviewed") is not False:
                    raise ValueError("Retained assistant output cannot claim product approval")
                values["retained_reply"] = {
                    "turn_id": completed.get("turn_id"),
                    "reply": completed.get("reply"),
                    "questions": completed.get("questions", []),
                }
        else:
            if not isinstance(result.get("conversations"), list):
                raise ValueError("Assistant status requires its retained conversation list")
            values["conversation_count"] = len(result["conversations"])
    return CommunicationAssistantV1.model_validate(values)


def _factory_verification(run: Data, handoff: Data) -> Data:
    from firefly_forge.agent_roles import WorkerRequest

    request = run.get("worker_request")
    if (
        not isinstance(request, dict)
        or request.get("kind") != "verification"
        or request.get("status") not in {"pending", "answered"}
        or (not handoff and run.get("status") != "paused")
    ):
        return {}
    try:
        requested = WorkerRequest.model_validate(
            {key: request[key] for key in ("id", "kind", "question", "context", "choices") if key in request}
        )
    except ValueError:
        return {}
    state = handoff.get("status", "not_recorded")
    raw = run["verification_handoff"] if handoff else {}
    total = raw.get("commands_count")
    total = total if type(total) is int and 1 <= total <= 20 else None
    completed, failed = raw.get("completed_commands"), raw.get("failed_commands")
    counts = (
        state in {"held", "completed"}
        and total is not None
        and type(completed) is int
        and type(failed) is int
        and 0 <= failed <= completed <= total
    )
    result: Data = {
        "responsibility": "factory",
        "requires_human_answer": False,
        "execution_grant": False,
        "source": "recorded_worker_request",
        "request_id": requested.id,
        "reason": requested.question,
        "context": requested.context,
        "handoff_status": state,
        "checks": {
            "source": "recorded_handoff" if handoff else "not_recorded",
            "result": "completed" if counts and state == "completed" and completed == total else "not_completed",
            "total": total,
            "recorded": completed if counts else None,
            "failed": failed if counts else None,
        },
        "next_native_action": {
            "not_recorded": "review_factory_verification_candidate",
            "pending": "review_pending_factory_verification",
            "running": "await_or_reconcile_recorded_verification",
            "held": "review_reconciliation_or_renewed_authority",
            "completed": "review_recorded_verification_results",
        }[state],
    }
    source_digest = raw.get("source_digest")
    if isinstance(source_digest, str) and re.fullmatch(r"[a-f0-9]{64}", source_digest):
        result["retained_source"] = {"source": "recorded_handoff", "digest": source_digest}
    error = raw.get("error")
    if state == "held" and error == (
        "Verification needs explicit reconciliation or renewed authority before continuation."
    ):
        result["hold"] = {"source": "recorded_handoff_error", "reason": error}
    return result


def _reused_verification(check: Data) -> bool | None:
    if "execution" not in check:
        return False
    if (
        check["execution"] != "reused"
        or check.get("exit_code") != 0
        or not isinstance(check.get("argv"), list)
        or not check["argv"]
        or not all(isinstance(value, str) for value in check["argv"])
        or not isinstance(check.get("output"), str)
        or not isinstance(check.get("created_at"), str)
        or any(
            not isinstance(check.get(key), str) or re.fullmatch(pattern, check[key]) is None
            for key, pattern in (
                ("source_execution_id", r"[a-f0-9]{32}"),
                ("source_record_digest", r"[a-f0-9]{64}"),
                ("verification_reuse_intent_digest", r"[a-f0-9]{64}"),
            )
        )
    ):
        return None
    try:
        if datetime.fromisoformat(check["created_at"]).tzinfo is None:
            return None
    except ValueError:
        return None
    return True


def communication_facts(
    run: Data | None,
    *,
    conversation: CommunicationConversationV1 | None = None,
    publication: CommunicationPublicationV1 | None = None,
    assistant: CommunicationAssistantV1 | None = None,
    git_delivery: CommunicationGitDeliveryV1 | None = None,
    audience: CommunicationAudienceV1 | None = None,
) -> Data:
    if assistant is not None:
        if run or conversation or publication or git_delivery or audience:
            raise ValueError("Assistant feedback is separate from execution and publication facts")
        assistant_facts = assistant.model_dump(exclude={"version"}, exclude_none=True)
        if assistant.turns:
            assistant_facts["latest_turn"] = assistant.turns[-1].model_dump()
        return {"assistant": assistant_facts}
    conversation_facts = {}
    if conversation is not None:
        if (conversation.selection == "run") != (run is not None):
            raise ValueError("Conversation selection requires its actual selected execution or no execution")
        conversation_facts = {
            **conversation.model_dump(exclude={"version", "question"}, exclude_none=True),
            "next_step": {
                "run": "explain_selected_execution",
                "missing": "ask_for_ticket_execution_link",
                "ambiguous": "ask_which_linked_execution",
                "clarify": "ask_whether_ticket_status_is_requested",
            }[conversation.selection],
        }
        if run is None:
            return {"conversation": conversation_facts}
    if run is None:
        raise ValueError("Communication requires an actual execution or native conversation context")
    if publication and (conversation or run.get("status") != "succeeded"):
        raise ValueError("Publication communication requires a successful execution without conversation context")
    if git_delivery is not None and (publication or conversation):
        raise ValueError("Intermediate Git delivery is separate from final publication and conversation facts")
    status = run.get("status")
    known = TERMINAL | {"queued", "running", "paused", "needs_reconciliation", "developing", "validating"}
    facts: Data = {"state": {"status": status if isinstance(status, str) and status in known else "unknown"}}
    if (publication or git_delivery) and audience:
        raise ValueError("Owner language context belongs to execution notifications")
    if publication is None:
        component = run.get("component_snapshot") or {}
        if (
            isinstance(component, dict)
            and isinstance(component.get("name"), str)
            and 1 <= len(component["name"]) <= 120
            and isinstance(component.get("responsibility"), str)
            and 1 <= len(component["responsibility"]) <= 2000
        ):
            facts["work"] = {
                "component": component["name"],
                "responsibility": component["responsibility"],
                "basis": "frozen_component",
            }
        operation = run.get("operation")
        if operation in {"develop", "test", "maintain"}:
            facts["execution_scope"] = {"operation": operation}
            revision = (run.get("source_checkpoint_snapshot") or {}).get("revision")
            if isinstance(revision, str) and re.fullmatch(r"[a-f0-9]{40}|[a-f0-9]{64}", revision):
                facts["execution_scope"]["source_revision"] = revision
        product = run.get("product_definition") or {}
        definition = product.get("definition") if isinstance(product, dict) else None
        if (
            isinstance(definition, dict)
            and isinstance(definition.get("customer"), str)
            and 1 <= len(definition["customer"]) <= 4000
            and isinstance(definition.get("problem"), str)
            and 1 <= len(definition["problem"]) <= 8000
        ):
            facts["product"] = {
                "customer": definition["customer"],
                "problem": definition["problem"],
                "basis": "frozen_requested_product_definition",
            }
        brief = run.get("brief")
        if isinstance(brief, str) and brief.strip() and ("work" in facts or "product" in facts or audience):
            facts["requested_work"] = {
                "basis": "requested_not_verified",
                "source": "frozen_run_brief",
                "source_sha256": hashlib.sha256(brief.encode()).hexdigest(),
            }
        if audience:
            facts["audience"] = {
                "source": audience.source,
                "source_sha256": audience.source_sha256,
                "language_basis": "verified_owner_quotation",
            }
    if publication:
        facts["publication"] = {
            **publication.model_dump(exclude={"version"}),
            "summary_basis": "worker_report",
            "purpose": "proposed_pull_request",
        }
    if git_delivery is not None:
        facts["git_delivery"] = {
            **git_delivery.model_dump(exclude={"version"}, exclude_none=True),
            "verification": "unverified" if git_delivery.implementation_disposition == "failed" else "pending",
            "purpose": "unverified_failed_checkpoint"
            if git_delivery.implementation_disposition == "failed"
            else "intermediate_draft_pull_request",
            "intent": {"target": "draft_pull_request", "status": "requested_not_confirmed"},
            "source_basis": "native_checkpoint_receipt",
        }
    checkpoint = run.get("git_delivery_summary")
    if git_delivery is None and (
        isinstance(checkpoint, dict)
        and type(checkpoint.get("sequence")) is int
        and 1 <= checkpoint["sequence"] <= 50
        and all(
            isinstance(checkpoint.get(key), str) and re.fullmatch(r"(?:[a-f0-9]{40}|[a-f0-9]{64})", checkpoint[key])
            for key in ("revision", "source_tree")
        )
        and checkpoint.get("remote_status") in {"not_requested", "publishing", "draft", "unknown", "ready"}
    ):
        facts["git_checkpoint"] = {
            key: checkpoint[key] for key in ("sequence", "revision", "source_tree", "remote_status")
        }
    if conversation_facts:
        facts["conversation"] = conversation_facts
    status = facts["state"]["status"]
    if status in {"failed", "needs_reconciliation"} and run.get("error") == (
        "Generated changes contain private credentials or worker metadata; inspect the workspace"
    ):
        facts["blocker"] = {
            "kind": "source_admission_guard",
            "basis": "recorded_error",
            "credential_exposure": "not_established",
            "next_step": "inspect_workspace_and_recorded_checks",
        }
    elif status in {"failed", "needs_reconciliation"} and run.get("error") in {
        "Checkpoint source changed during final admission",
        "Checkpoint source selection changed during final admission",
        "Checkpoint source directory identity changed during final admission",
        "Checkpoint Git metadata changed during final admission",
    }:
        facts["blocker"] = {
            "kind": "checkpoint_source_admission_guard",
            "basis": "recorded_error",
            "cause": "not_established",
            "next_step": "inspect_source_and_checkpoint_evidence",
        }
    elif status in {"failed", "needs_reconciliation"} and run.get("error") in {
        "native_agentic_checkpoint_wal_required",
        "bridge_completion_wal_required",
    }:
        facts["blocker"] = {
            "kind": "native_durability_mode_guard",
            "basis": "recorded_error",
            "requirement": "agentic_checkpoint_wal"
            if run["error"] == "native_agentic_checkpoint_wal_required"
            else "bridge_completion_wal",
            "cause": "not_established",
            "next_step": "review_native_configuration_and_scoped_adoption",
        }
    elif status in {"failed", "needs_reconciliation"} and run.get("error") == (
        "Original selection, configuration or reviewed policy epoch changed"
    ):
        facts["blocker"] = {
            "kind": "execution_selection_guard",
            "basis": "recorded_error",
            "cause": "not_established",
            "next_step": "review_execution_configuration_before_continuation",
            "recorded_condition": "execution_selection_configuration_or_reviewed_policy_no_longer_matches",
            "review_scope": ["execution_selection", "execution_configuration", "reviewed_policy"],
        }
    handoff = (
        verification_handoff(run)
        if isinstance(run.get("verification_handoff"), dict) and isinstance(run.get("worker_request"), dict)
        else {}
    )
    if factory_verification := _factory_verification(run, handoff):
        facts["factory_verification"] = factory_verification
    if (
        handoff.get("status") == "completed"
        and 1 <= handoff["commands_count"] <= 20
        and 0 <= handoff["failed_commands"] <= handoff["completed_commands"] <= handoff["commands_count"]
    ):
        facts["check_batch"] = {
            "source": "governed_handoff",
            "provenance": "retained",
            "total": handoff["commands_count"],
            "recorded": handoff["completed_commands"],
            "failed": handoff["failed_commands"],
        }
    if isinstance(run.get("id"), str) and re.fullmatch(r"[a-f0-9]{32}", run["id"]) and status in TERMINAL:
        facts["delivery"] = {
            "factory_result": "accepted" if status == "succeeded" else "not_accepted",
            "scope": "execution_unit",
            "operation": run.get("operation") if run.get("operation") in {"develop", "test", "maintain"} else "unknown",
            "product_completion": "not_established",
            **(
                {
                    "review_link": "recorded"
                    if isinstance(run.get("pr_url"), str) and run["pr_url"].strip()
                    else "not_recorded"
                }
                if git_delivery is None
                else {}
            ),
        }
    stage = run.get("active_task")
    if isinstance(stage, str) and stage in STAGES and status in {"running", "developing", "validating"}:
        facts["stage"] = {"name": stage}
    recovery = provider_recovery(run)
    if recovery:
        facts["recovery"] = {"attempt": recovery["attempt"]}
    request = run.get("worker_request")
    if (
        isinstance(request, dict)
        and request.get("kind") != "verification"
        and request.get("status") == "pending"
        and status == "paused"
        and not verification_handoff(run)
    ):
        from firefly_forge.agent_roles import WorkerRequest

        try:
            decision = WorkerRequest.model_validate(
                {key: request[key] for key in ("id", "kind", "question", "context", "choices") if key in request}
            )
        except ValueError:
            decision = None
        if decision:
            facts["decision"] = {
                "kind": decision.kind,
                "choices_count": len(decision.choices),
                "context_available": True,
                "requires_human_answer": True,
                "question": _text(decision.question, lambda value: value, 30000),
                "context": _text(decision.context, lambda value: value, 30000),
                "choices": [_text(value, lambda text: text, 10000) for value in decision.choices],
            }
    checks = run.get("final_verification")
    if (
        isinstance(checks, list)
        and checks
        and all(isinstance(check, dict) and type(check.get("exit_code")) is int for check in checks)
    ):
        attempt = recovery["attempt"] if recovery else run.get("attempt")
        attempts = [check.get("attempt") for check in checks]
        provenance = (
            "unknown"
            if type(attempt) is not int
            or not 1 <= attempt <= 100
            or not all(type(value) is int and 1 <= value <= 100 for value in attempts)
            else "current"
            if set(attempts) == {attempt}
            else "retained"
        )
        facts["verification"] = {
            "recorded": len(checks),
            "passed": sum(check["exit_code"] == 0 for check in checks),
            "failed": sum(check["exit_code"] != 0 for check in checks),
            "provenance": provenance,
        }
        if any("execution" in check for check in checks):
            reused = [_reused_verification(check) for check in checks]
            facts["verification"]["provenance"] = "unknown" if None in reused else "retained"
            if None not in reused:
                facts["verification"].update(
                    reused_results=sum(value is True for value in reused),
                    new_executions=sum(
                        value is False and check.get("attempt") == attempt
                        for check, value in zip(checks, reused, strict=True)
                    ),
                )
    current_attempt = recovery["attempt"] if recovery else run.get("attempt")
    if "work" in facts and type(current_attempt) is int and 1 <= current_attempt <= 100:
        recorded = checks if isinstance(checks, list) else []
        known_attempts = (checks is None or isinstance(checks, list)) and all(
            isinstance(check, dict)
            and type(check.get("attempt")) is int
            and 1 <= check["attempt"] <= 100
            and type(check.get("exit_code")) is int
            and _reused_verification(check) is not None
            for check in recorded
        )
        current = (
            sum(check["attempt"] == current_attempt and _reused_verification(check) is False for check in recorded)
            if known_attempts
            else None
        )
        facts["check_status"] = {
            "current_attempt": current_attempt,
            "recorded": current,
            "provenance": "unknown" if current is None else "current" if current else "not_recorded",
        }
    from firefly_forge.communication_diagnostics import check_diagnostics

    if diagnostics := check_diagnostics(run):
        facts["check_diagnostics"] = diagnostics
    return facts


def _validate_draft(draft: CommunicationDraft, facts: Data) -> str:
    """Validate framing and references; the separate model review evaluates meaning."""
    paragraphs = [draft.title, *draft.paragraphs] if isinstance(draft, CommunicationCard) else draft.paragraphs
    if isinstance(draft, CommunicationCard) and (
        draft.title.heading or draft.title.format != "paragraph" or draft.title.diagnostic_id
    ):
        raise ValueError("Communication title cannot contain a section or excerpt")
    for paragraph in paragraphs:
        if len(set(paragraph.fact_ids)) != len(paragraph.fact_ids) or set(paragraph.fact_ids) - facts.keys():
            raise ValueError("Communication contains an unknown fact reference")
        if paragraph.diagnostic_id and (
            "check_diagnostics" not in paragraph.fact_ids
            or paragraph.diagnostic_id not in facts.get("check_diagnostics", {})
        ):
            raise ValueError("Communication excerpt must identify recorded check evidence")
        prose = paragraph.text + (paragraph.heading or "")
        for path in sorted((facts.get("git_delivery") or {}).get("changed_files", []), key=len, reverse=True):
            if re.fullmatch(r"[\w./ -]+", path):
                prose = prose.replace(path, "SOURCEPATH")
        if re.search(r"[\x00-\x1f\x7f@<>`\[\]{}\\*_#|]|https?://|www\.|\b\w+://|%", prose):
            raise ValueError("Communication must be plain, bounded prose without destinations or code")
    cited = {key for paragraph in paragraphs for key in paragraph.fact_ids}
    required = {
        "blocker",
        "check_batch",
        "factory_verification",
        "conversation",
        "publication",
        "assistant",
        "git_delivery",
        "git_checkpoint",
        "work",
        "requested_work",
        "product",
        "audience",
        "check_status",
    } & facts.keys()
    if facts.get("conversation", {}).get("question_sha256"):
        required -= {"work", "requested_work", "product", "audience"}
    if required - cited:
        raise ValueError("Communication omits the recorded blocker or check context")
    return "\n\n".join(paragraph.text for paragraph in paragraphs)


def _assistant_content(assistant: Data, safe: Callable[[str], str], *, excerpt: bool = False) -> str:
    quotation, references = [], []
    retained = assistant.get("retained_reply")
    if assistant.get("turns"):
        latest = assistant["turns"][-1]
        notices = {
            "failed": "The latest turn failed.",
            "needs_attention": "The latest turn needs attention.",
            "needs_reconciliation": "The latest turn has an unconfirmed outcome.",
            "processing": "The latest turn is still processing.",
            "completed": "The latest recorded turn completed.",
        }
        notice = notices[latest["status"]]
        references.append("Latest turn: `" + latest["turn_id"] + "`")
        if retained:
            notice += (
                " The quote below is an earlier completed reply."
                if latest["turn_id"] != retained["turn_id"]
                else " The quote below is that recorded reply."
            )
            references.append("Quoted reply: `" + retained["turn_id"] + "`")
            assistant = retained
        else:
            notice += " No completed reply is available in this status snapshot."
        quotation.append(notice)
    if excerpt:
        if not assistant.get("reply_reference_digest"):
            raise ValueError("A reply excerpt requires its admitted full reference")
        full = assistant["reply"]
        if assistant["questions"]:
            full += "\n\nQuestions:\n" + "\n".join("- " + question for question in assistant["questions"])
        quotation.append("Reply excerpt. The full reply and questions are retained in the reference files below.")
        quotation.append(
            "  \n".join(
                "> " + _text(line, safe, 12000, preserve_word_slashes=True) for line in (full[:2000] + "…").splitlines()
            )
        )
    elif assistant.get("reply"):
        quotation.append(
            "  \n".join(
                "> " + _text(line, safe, 12000, preserve_word_slashes=True) for line in assistant["reply"].splitlines()
            )
        )
    if not excerpt:
        quotation.extend(
            "> - " + _text(question, safe, 12000, preserve_word_slashes=True) for question in assistant["questions"]
        )
    if assistant.get("proposal_status") == "unreviewed":
        description = json.loads(prompt_text("collaboration/copy", version="1.0.1"))["proposal_description"]
        if excerpt:
            quotation.insert(0, description)
        else:
            quotation.append(description)
    content = "\n\n".join(quotation)
    if references:
        return prompt_text(
            "collaboration/conversation-record",
            message=content,
            summary="Turn references",
            evidence=prompt_text("collaboration/facts", facts="\n".join(references)),
        )
    return content


def _render_card(
    draft: CommunicationCard, facts: Data, safe: Callable[[str], str], reject: Callable[[Any], None]
) -> str:
    if len(draft.title.text) > 100:
        raise ValueError("Communication title exceeds its display bound")
    narrative = _validate_draft(draft, facts)
    expected = facts.get("decision")
    if expected:
        quotation = {key: expected[key] for key in ("question", "context", "choices")}
        if draft.decision is None or draft.decision.model_dump() != quotation:
            raise ValueError("Communication must preserve the human question and every choice")
    elif draft.decision is not None:
        raise ValueError("Communication cannot invent a human decision")
    reject(narrative)
    title = safe(draft.title.text)
    from firefly_forge.communication_diagnostics import render_excerpt

    blocks = []
    for paragraph in draft.paragraphs:
        parts = ["#### " + safe(paragraph.heading)] if paragraph.heading else []
        parts.append(("- " if paragraph.format == "bullet" else "") + safe(paragraph.text))
        if paragraph.diagnostic_id:
            excerpt = facts["check_diagnostics"][paragraph.diagnostic_id]
            reject(excerpt["text"])
            parts.append(render_excerpt({**excerpt, "text": safe(excerpt["text"])}))
        blocks.append("\n\n".join(parts))
    narrative = "\n\n".join(blocks)
    decision_body = ""
    if draft.decision:
        decision_body = prompt_text(
            "collaboration/communication-decision",
            question=draft.decision.question,
            context=draft.decision.context,
            choices="\n".join("- " + value for value in draft.decision.choices),
        )
    if facts.get("assistant"):
        decision_body = _assistant_content(facts["assistant"], safe)
    body = prompt_text("collaboration/communication-card", title=title, narrative=narrative, decision=decision_body)
    if checkpoint := facts.get("git_checkpoint"):
        body += "\n\n" + prompt_text(
            "collaboration/facts",
            facts=f"Factory checkpoint: `{checkpoint['revision']}`\nSource tree: `{checkpoint['source_tree']}`",
        )
    # Native reference controls are appended outside model-authored prose.
    if len(body) > 5000 and (facts.get("assistant") or {}).get("reply_reference_digest"):
        body = prompt_text(
            "collaboration/communication-card",
            title=title,
            narrative=narrative,
            decision=_assistant_content(facts["assistant"], safe, excerpt=True),
        )
    if len(body) > 6000:
        raise ValueError("Communication exceeds its public text bound")
    reject(body)
    return body


class CommunicationWriter:
    def __init__(self, store: Any, *, model: Any = None) -> None:
        self.store, self.model = store, model

    def assert_current(self, evidence: Data) -> None:
        if evidence.get("mode") != "ai":
            return
        router, selected = self.store.task_router, evidence["profile"]
        current = router.current()
        if current["digest"] != selected["routing_digest"]:
            raise ValueError("Communication routing changed before delivery")
        if router.profiles._read(selected["profile_id"])["revision"] != selected["profile_revision"]:
            raise ValueError("Communication profile changed before delivery")

    async def write(
        self,
        run: Data | None,
        native_card: str,
        safe: Callable[[str], str],
        *,
        conversation: CommunicationConversationV1 | None = None,
        publication: CommunicationPublicationV1 | None = None,
        assistant: CommunicationAssistantV1 | None = None,
        git_delivery: CommunicationGitDeliveryV1 | None = None,
        audience: CommunicationAudienceV1 | None = None,
    ) -> Data:
        facts = communication_facts(
            run,
            conversation=conversation,
            publication=publication,
            assistant=assistant,
            git_delivery=git_delivery,
            audience=audience,
        )
        context: Data = {}
        if conversation and conversation.question is not None:
            context["conversation_question"] = {
                "quotation": conversation.question,
                "source_sha256": conversation.question_sha256,
                "basis": "current_human_message_not_execution_authority",
            }
        if "requested_work" in facts:
            assert run is not None
            context["requested_work"] = {**facts["requested_work"], "quotation": run["brief"]}
        if audience:
            context["audience"] = {**facts["audience"], "quotation": audience.quotation}
        if reject := getattr(self.store, "reject_credentials", None):
            if "decision" in facts:
                assert run is not None
                reject({key: run["worker_request"].get(key) for key in ("question", "context", "choices")})
            reject(facts)
            reject(context)
        evidence: Data = {
            "mode": "deterministic",
            "reason": "unconfigured",
            "facts": facts,
            "facts_sha256": content_digest(facts),
            "context_sha256": content_digest(context),
        }
        if assistant is not None:
            copy = json.loads(prompt_text("collaboration/copy", version="1.0.1"))
            assistant_references = [
                copy["facts"][key] + ": `" + value + "`"
                for key in ("conversation_id", "turn_id")
                if (value := facts["assistant"].get(key))
            ]
            narrative = prompt_text("collaboration/action", summary=copy["actions"]["other"], status=assistant.status)

            def assistant_card(*, excerpt: bool = False) -> str:
                return prompt_text(
                    "collaboration/communication-card",
                    title=copy["titles"]["recorded"],
                    narrative=narrative,
                    decision="\n\n".join(
                        [
                            _assistant_content(facts["assistant"], safe, excerpt=excerpt),
                            prompt_text("collaboration/facts", facts="\n".join(assistant_references)),
                        ]
                    ),
                )

            native_card = assistant_card()
            if len(native_card) > 5000 and assistant.reply_reference_digest:
                native_card = assistant_card(excerpt=True)
            self.store.reject_credentials(native_card)
            if len(native_card) > 6000:
                raise ValueError("Assistant communication exceeds its public text bound")
        result = {"body": native_card, "communication": evidence}
        router = getattr(self.store, "task_router", None)
        if router is None:
            return result
        evidence.update(mode="held", reason="unavailable", repair_limit=1, repair_attempted=False)
        result["body"] = ""
        try:
            selection = await router.resolve("communication")
            if selection is None:
                evidence.update(mode="deterministic", reason="unconfigured")
                result["body"] = native_card
                return result
            decision = facts.get("decision")
            if decision and (
                len(decision["question"]) > 600
                or len(decision["context"]) > 600
                or any(len(value) > 160 for value in decision["choices"])
                or len(set(decision["choices"])) != len(decision["choices"])
            ):
                evidence["reason"] = "decision_too_large"
                return result
            if selection["provider"] not in {"agentic", "bridge"}:
                raise ValueError("Communication requires an explicit compatible profile")
            settings = router.profiles.validate_snapshot(selection)
            evidence["profile"] = selection
            timeout = min(120, int(settings.get("FORGE_PROVIDER_TIMEOUT", "60")))
            evidence["timeout_seconds"] = timeout
            prompt = prompt_text(
                "collaboration/publication-request" if publication else "collaboration/communication-request",
                version="1.0.0" if publication else "1.0.3",
                facts=json.dumps(facts, sort_keys=True),
                fact_ids=json.dumps(sorted(facts)),
                **({"context": json.dumps(context, sort_keys=True, ensure_ascii=False)} if not publication else {}),
            )
            instructions = inject_skills(
                prompt_text("collaboration/publication-system", version="1.0.1")
                if publication
                else prompt_text("collaboration/communication-system", version="1.0.8"),
                ["humanizer"],
            )
            language_instructions = (
                json.loads(prompt_text("collaboration/communication-language", version="1.0.1"))[
                    conversation.language if conversation else "owner"
                ]
                if conversation or audience
                else {}
            )
            if language_instructions:
                instructions += "\n" + language_instructions["draft"]
            skill = next(item for item in community_catalog() if item["id"] == "humanizer")
            evidence["skills"] = [
                {
                    "id": skill["id"],
                    "adaptation_sha256": skill["adaptation_sha256"],
                    "revision": skill["provenance"]["revision"],
                }
            ]
            self.store.reject_credentials([prompt, instructions])
            evidence["prompt_sha256"] = hashlib.sha256((instructions + "\n" + prompt).encode()).hexdigest()
            from fireflyframework_agentic.agents import FireflyAgent
            from fireflyframework_agentic.config import FireflyAgenticConfig
            from pydantic_ai import PromptedOutput
            from pydantic_ai.exceptions import UnexpectedModelBehavior
            from pydantic_ai.usage import UsageLimits

            from firefly_forge.providers import APIProvider, BridgeProvider

            model = self.model
            if model is None:
                model = (
                    BridgeProvider(settings).model()
                    if selection["provider"] == "bridge"
                    else APIProvider(settings).model(single_request=True)
                )
            output: Any = PromptedOutput(CommunicationCard) if selection["provider"] == "bridge" else CommunicationCard
            agent: FireflyAgent[None, CommunicationCard] = FireflyAgent(
                name="forge-project-communication",
                model=model,
                instructions=instructions,
                output_type=output,
                retries=0,
                tools=[],
                auto_register=False,
                model_settings={"max_tokens": 1200},
                config=FireflyAgenticConfig(default_temperature=None, rate_limit_max_retries=0),
            )
            async with asyncio.timeout(timeout) as deadline:
                generated = await agent.run(
                    prompt,
                    timeout=timeout,
                    usage_limits=UsageLimits(request_limit=1, tool_calls_limit=0, output_tokens_limit=1400),
                )
                try:
                    draft = CommunicationCard.model_validate(generated.output)
                    body = _render_card(draft, facts, safe, self.store.reject_credentials)
                except (ValueError, TypeError):
                    evidence["reason"] = "invalid_output"
                    return result
                evidence["candidate_sha256"] = content_digest(draft.model_dump())
                review_instructions = prompt_text(
                    "collaboration/publication-review-system"
                    if publication
                    else "collaboration/communication-review-system",
                    version="1.0.1" if publication else "1.0.5",
                )
                if language_instructions:
                    review_instructions += "\n" + language_instructions["review"]
                review_prompt = prompt_text(
                    "collaboration/communication-review-request",
                    version="1.0.1",
                    facts=json.dumps(facts, sort_keys=True),
                    candidate=draft.model_dump_json(),
                    fact_ids=json.dumps(sorted(facts)),
                    context=json.dumps(context, sort_keys=True, ensure_ascii=False),
                )
                self.store.reject_credentials([review_instructions, review_prompt])
                evidence["review_prompt_sha256"] = hashlib.sha256(
                    (review_instructions + "\n" + review_prompt).encode()
                ).hexdigest()
                review_output: Any = (
                    PromptedOutput(CommunicationReview) if selection["provider"] == "bridge" else CommunicationReview
                )
                reviewer: FireflyAgent[None, CommunicationReview] = FireflyAgent(
                    name="forge-project-communication-review",
                    model=model,
                    instructions=review_instructions,
                    output_type=review_output,
                    retries=0,
                    tools=[],
                    auto_register=False,
                    model_settings={"max_tokens": 600},
                    config=FireflyAgenticConfig(default_temperature=None, rate_limit_max_retries=0),
                )
                when = deadline.when()
                assert when is not None
                remaining = max(0.001, when - asyncio.get_running_loop().time())
                try:
                    reviewed = await reviewer.run(
                        review_prompt,
                        timeout=remaining,
                        usage_limits=UsageLimits(request_limit=1, tool_calls_limit=0, output_tokens_limit=700),
                    )
                    review = CommunicationReview.model_validate(reviewed.output)
                    reader_approved = all(review.reader_checks.model_dump().values())
                    references = set(review.fact_ids)
                    cited = {key for paragraph in [draft.title, *draft.paragraphs] for key in paragraph.fact_ids}
                    if (
                        len(references) != len(review.fact_ids)
                        or references - facts.keys()
                        or cited - references
                        or (
                            review.verdict == "supported"
                            and review.issues
                            and (
                                reader_approved
                                or set(review.issues) - {"insufficient_context", "unclear_language", "poor_structure"}
                            )
                        )
                        or (review.verdict != "supported" and not review.issues)
                    ):
                        raise ValueError("Communication review must bind the whole candidate to known facts")
                    self.store.reject_credentials(review.model_dump())
                except (ValueError, TypeError, UnexpectedModelBehavior):
                    evidence["reason"] = "invalid_review"
                    return result
                evidence["review"] = review.model_dump()
                evidence["review_candidate_sha256"] = evidence["candidate_sha256"]
                evidence["review_applies_to"] = "original_candidate"
                if review.verdict != "supported" or not reader_approved:
                    repair_prompt = prompt_text(
                        "collaboration/communication-repair-request",
                        version="1.0.1",
                        facts=json.dumps(facts, sort_keys=True),
                        candidate=draft.model_dump_json(),
                        review=review.model_dump_json(),
                        fact_ids=json.dumps(sorted(facts)),
                        context=json.dumps(context, sort_keys=True, ensure_ascii=False),
                    )
                    self.store.reject_credentials(repair_prompt)
                    evidence["repair_attempted"] = True
                    evidence["repair_prompt_sha256"] = hashlib.sha256(repair_prompt.encode()).hexdigest()
                    corrected = await agent.run(
                        repair_prompt,
                        timeout=max(0.001, when - asyncio.get_running_loop().time()),
                        usage_limits=UsageLimits(request_limit=1, tool_calls_limit=0, output_tokens_limit=1400),
                    )
                    try:
                        draft = CommunicationCard.model_validate(corrected.output)
                        body = _render_card(draft, facts, safe, self.store.reject_credentials)
                    except (ValueError, TypeError):
                        evidence["reason"] = "invalid_output"
                        return result
                    evidence["candidate_sha256"] = content_digest(draft.model_dump())
                evidence.update(mode="ai", reason="generated")
                return {"body": body, "communication": evidence}
        except TimeoutError:
            evidence["reason"] = "timeout"
        except Exception as exc:
            from pydantic_ai.exceptions import ModelHTTPError

            evidence["reason"] = (
                "quota" if isinstance(exc, ModelHTTPError) and exc.status_code == 429 else "unavailable"
            )
        return result
