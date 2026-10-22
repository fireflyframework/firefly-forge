# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Optional, one-shot AI wording over a bounded native communication fact packet."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import Callable
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from firefly_forge.collaboration_feedback import STAGES, TERMINAL, _text, provider_recovery, verification_handoff
from firefly_forge.community_skills import community_catalog
from firefly_forge.product_provenance import content_digest
from firefly_forge.prompts import prompt_text
from firefly_forge.scm_authority_models import StrictContract
from firefly_forge.skills import inject_skills

Data = dict[str, Any]


class CommunicationParagraph(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)
    text: str = Field(min_length=1, max_length=600)
    fact_ids: list[str] = Field(min_length=1, max_length=4)


class CommunicationDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    paragraphs: list[CommunicationParagraph] = Field(min_length=1, max_length=3)


class CommunicationDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    question: str = Field(min_length=1, max_length=600)
    context: str = Field(min_length=1, max_length=600)
    choices: list[str] = Field(max_length=6)


class CommunicationCard(CommunicationDraft):
    title: CommunicationParagraph
    decision: CommunicationDecision | None = None


class CommunicationReview(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)
    verdict: Literal["supported", "unsupported", "uncertain"]
    issues: list[
        Literal[
            "unsupported_claim",
            "contradictory_state",
            "misleading_checks",
            "unrecorded_action",
            "changed_decision",
            "insufficient_context",
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

    @model_validator(mode="after")
    def consistent_selection(self) -> CommunicationConversationV1:
        expected = 1 if self.selection == "run" else 0
        if (self.selection == "ambiguous" and self.linked_runs < 2) or (
            self.selection != "ambiguous" and self.linked_runs != expected
        ):
            raise ValueError("Conversation selection and linked execution count disagree")
        return self


def communication_facts(run: Data | None, *, conversation: CommunicationConversationV1 | None = None) -> Data:
    conversation_facts = {}
    if conversation is not None:
        if (conversation.selection == "run") != (run is not None):
            raise ValueError("Conversation selection requires its actual selected execution or no execution")
        conversation_facts = {
            **conversation.model_dump(exclude={"version"}),
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
    status = run.get("status")
    known = TERMINAL | {"queued", "running", "paused", "needs_reconciliation", "developing", "validating"}
    facts: Data = {"state": {"status": status if isinstance(status, str) and status in known else "unknown"}}
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
    handoff = (
        verification_handoff(run)
        if isinstance(run.get("verification_handoff"), dict) and isinstance(run.get("worker_request"), dict)
        else {}
    )
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
            "review_link": "recorded"
            if isinstance(run.get("pr_url"), str) and run["pr_url"].strip()
            else "not_recorded",
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
        and 1 <= len(checks) <= 20
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
            "provenance": provenance,
        }
    return facts


def _validate_draft(draft: CommunicationDraft, facts: Data) -> str:
    """Validate framing and references; the separate model review evaluates meaning."""
    paragraphs = [draft.title, *draft.paragraphs] if isinstance(draft, CommunicationCard) else draft.paragraphs
    for paragraph in paragraphs:
        if len(set(paragraph.fact_ids)) != len(paragraph.fact_ids) or set(paragraph.fact_ids) - facts.keys():
            raise ValueError("Communication contains an unknown fact reference")
        if re.search(r"[\x00-\x1f\x7f@<>`\[\]{}\\*_#|]|https?://|www\.|\b\w+://|%", paragraph.text):
            raise ValueError("Communication must be plain, bounded prose without destinations or code")
    cited = {key for paragraph in paragraphs for key in paragraph.fact_ids}
    if ({"blocker", "check_batch", "conversation"} & facts.keys()) - cited:
        raise ValueError("Communication omits the recorded blocker or check context")
    return "\n\n".join(paragraph.text for paragraph in paragraphs)


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
    title, narrative = safe(narrative).split("\n\n", 1)
    decision_body = ""
    if draft.decision:
        decision_body = prompt_text(
            "collaboration/communication-decision",
            question=draft.decision.question,
            context=draft.decision.context,
            choices="\n".join("- " + value for value in draft.decision.choices),
        )
    body = prompt_text("collaboration/communication-card", title=title, narrative=narrative, decision=decision_body)
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
    ) -> Data:
        facts = communication_facts(run, conversation=conversation)
        if reject := getattr(self.store, "reject_credentials", None):
            if "decision" in facts:
                assert run is not None
                reject({key: run["worker_request"].get(key) for key in ("question", "context", "choices")})
            reject(facts)
        evidence: Data = {
            "mode": "deterministic",
            "reason": "unconfigured",
            "facts": facts,
            "facts_sha256": content_digest(facts),
        }
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
                "collaboration/communication-request",
                version="1.0.1",
                facts=json.dumps(facts, sort_keys=True),
                fact_ids=json.dumps(sorted(facts)),
            )
            instructions = inject_skills(
                prompt_text("collaboration/communication-system", version="1.0.4" if conversation else "1.0.3"),
                ["humanizer"],
            )
            language_instructions = (
                json.loads(prompt_text("collaboration/communication-language"))[conversation.language]
                if conversation
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
                    "collaboration/communication-review-system", version="1.0.1" if conversation else "1.0.0"
                )
                if language_instructions:
                    review_instructions += "\n" + language_instructions["review"]
                review_prompt = prompt_text(
                    "collaboration/communication-review-request",
                    facts=json.dumps(facts, sort_keys=True),
                    candidate=draft.model_dump_json(),
                    fact_ids=json.dumps(sorted(facts)),
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
                reviewed = await reviewer.run(
                    review_prompt,
                    timeout=remaining,
                    usage_limits=UsageLimits(request_limit=1, tool_calls_limit=0, output_tokens_limit=700),
                )
                try:
                    review = CommunicationReview.model_validate(reviewed.output)
                    references = set(review.fact_ids)
                    cited = {key for paragraph in [draft.title, *draft.paragraphs] for key in paragraph.fact_ids}
                    if (
                        len(references) != len(review.fact_ids)
                        or references - facts.keys()
                        or cited - references
                        or (review.verdict == "supported" and review.issues)
                        or (review.verdict != "supported" and not review.issues)
                    ):
                        raise ValueError("Communication review must bind the whole candidate to known facts")
                    self.store.reject_credentials(review.model_dump())
                except (ValueError, TypeError):
                    evidence["reason"] = "invalid_review"
                    return result
                evidence["review"] = review.model_dump()
                evidence["review_candidate_sha256"] = evidence["candidate_sha256"]
                evidence["review_applies_to"] = "original_candidate"
                if review.verdict != "supported":
                    repair_prompt = prompt_text(
                        "collaboration/communication-repair-request",
                        facts=json.dumps(facts, sort_keys=True),
                        candidate=draft.model_dump_json(),
                        review=review.model_dump_json(),
                        fact_ids=json.dumps(sorted(facts)),
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
