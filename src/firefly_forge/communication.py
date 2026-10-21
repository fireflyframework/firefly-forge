# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Optional, one-shot AI wording over a bounded native communication fact packet."""

from __future__ import annotations

import asyncio
import hashlib
import html
import json
import re
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from firefly_forge.collaboration_feedback import STAGES, TERMINAL, _text, provider_recovery, verification_handoff
from firefly_forge.community_skills import community_catalog
from firefly_forge.product_provenance import content_digest
from firefly_forge.prompts import prompt_text
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


def communication_facts(run: Data) -> Data:
    status = run.get("status")
    known = TERMINAL | {"queued", "running", "paused", "needs_reconciliation", "developing", "validating"}
    facts: Data = {"state": {"status": status if isinstance(status, str) and status in known else "unknown"}}
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
    texts = []
    paragraphs = [draft.title, *draft.paragraphs] if isinstance(draft, CommunicationCard) else draft.paragraphs
    for paragraph in paragraphs:
        if len(set(paragraph.fact_ids)) != len(paragraph.fact_ids) or set(paragraph.fact_ids) - facts.keys():
            raise ValueError("Communication contains an unknown fact reference")
        text = paragraph.text
        if re.search(r"[\x00-\x1f\x7f@<>`\[\]{}\\*_#|]|https?://|www\.|\b\w+://|%", text):
            raise ValueError("Communication must be plain, bounded prose without destinations or code")
        referenced = {key: facts[key] for key in paragraph.fact_ids}
        if facts.get("blocker", {}).get("credential_exposure") == "not_established":
            diagnostic = re.sub(
                r"\b(?:(?:does not|doesn['’]t) (?:establish|confirm|prove) (?:a |any )?credential (?:leak|exposure)|"
                r"no evidence of (?:a |any )?credential (?:leak|exposure)|"
                r"credential (?:leak|exposure) is not (?:established|confirmed|proven))\b",
                "",
                text,
                flags=re.I,
            )
            if re.search(
                r"\b(?:leaks?|leaked|leaking|leakage|exposed|exposure|breach(?:ed)?|compromis(?:e|ed)|stolen)\b",
                diagnostic,
                re.I,
            ):
                raise ValueError("A recorded source guard cannot establish a credential breach")
        for word, statuses in {
            "queued": {"queued"},
            "paused": {"paused"},
            "cancelled": {"cancelled"},
            "interrupted": {"interrupted"},
            "implementation": {"running", "developing"},
            "development": {"running", "developing"},
        }.items():
            if re.search(r"\b" + word + r"\b", text, re.I) and (
                "state" not in referenced or facts["state"]["status"] not in statuses
            ):
                raise ValueError("Communication contradicts the recorded run state")
        allowed_numbers = set(re.findall(r"\b\d+\b", json.dumps(referenced)))
        if set(re.findall(r"\b\d+\b", text)) - allowed_numbers:
            raise ValueError("Communication introduces an unrecorded number")
        if re.search(
            r"\b(?:deploy(?:ed|ment)?|merged|released|published|production.ready|ready for review|"
            r"fixed|implemented|built|"
            r"resolved|added|created|deleted|removed)\b",
            text,
            re.I,
        ):
            raise ValueError("Communication cannot claim an external delivery")
        if re.search(r"\b(?:will|shall|going to)\b|\bI['’]ll\b", text, re.I):
            raise ValueError("Communication cannot promise an unrecorded future action")
        if "decision" in facts:
            normalized_text = " " + " ".join(re.findall(r"\w+", text.casefold())) + " "
            for choice in facts["decision"].get("choices", []):
                plain_choice = re.sub(r"\\([^\w\s])", r"\1", html.unescape(choice)).strip().casefold()
                words = " ".join(re.findall(r"\w+", plain_choice))
                if words and " " + words + " " in normalized_text or not words and plain_choice in text.casefold():
                    raise ValueError("Human choice names belong only in the exact decision quotation")
            if re.search(r"\b(?:recommend|choose|chosen|approved|answer is|I accept|we accept)\b", text, re.I):
                raise ValueError("Communication must not choose or answer on the human's behalf")
        completion_claim = text
        if referenced.get("delivery", {}).get("factory_result") == "not_accepted":
            completion_claim = re.sub(
                r"(^|[.!?]\s+)(?:this|(?:this|the) (?:(?:native|recorded) )?(?:run|delivery)) (?:is|was) "
                r"not (?:accepted as )?a completed delivery(?=[.,;]|$)",
                r"\1",
                completion_claim,
                flags=re.I,
            )
        if re.search(r"\b(?:complete[ds]?|succeeded|finished|ready for review)\b", completion_claim, re.I) and facts[
            "state"
        ]["status"] not in {"succeeded"}:
            raise ValueError("Communication cannot announce unrecorded completion")
        if re.search(r"\b(?:pass(?:ed|ing)?|green)\b", text, re.I):
            verification = referenced.get("verification", {})
            if (
                not verification
                or verification.get("provenance") != "current"
                or verification["passed"] != verification["recorded"]
            ):
                raise ValueError("Communication cannot turn retained or failed checks into current success")
        texts.append(text)
    cited = {key for paragraph in paragraphs for key in paragraph.fact_ids}
    if ({"blocker", "check_batch"} & facts.keys()) - cited:
        raise ValueError("Communication omits the recorded blocker or check context")
    return "\n\n".join(texts)


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

    async def write(self, run: Data, native_card: str, safe: Callable[[str], str]) -> Data:
        facts = communication_facts(run)
        if reject := getattr(self.store, "reject_credentials", None):
            if "decision" in facts:
                reject({key: run["worker_request"].get(key) for key in ("question", "context", "choices")})
            reject(facts)
        evidence: Data = {
            "mode": "deterministic",
            "reason": "unconfigured",
            "facts": facts,
            "facts_sha256": content_digest(facts),
        }
        result = {"body": native_card, "communication": evidence}
        decision = facts.get("decision")
        if decision and (
            len(decision["question"]) > 600
            or len(decision["context"]) > 600
            or any(len(value) > 160 for value in decision["choices"])
            or len(set(decision["choices"])) != len(decision["choices"])
        ):
            evidence["reason"] = "decision_too_large"
            return result
        router = getattr(self.store, "task_router", None)
        if router is None:
            return result
        try:
            selection = await router.resolve("communication")
            if selection is None:
                return result
            if selection["provider"] not in {"agentic", "bridge"}:
                raise ValueError("Communication requires an explicit compatible profile")
            settings = router.profiles.validate_snapshot(selection)
            evidence["profile"] = selection
            prompt = prompt_text(
                "collaboration/communication-request",
                version="1.0.1",
                facts=json.dumps(facts, sort_keys=True),
                fact_ids=json.dumps(sorted(facts)),
            )
            instructions = inject_skills(
                prompt_text("collaboration/communication-system", version="1.0.2"), ["humanizer"]
            )
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
            async with asyncio.timeout(20):
                generated = await agent.run(
                    prompt,
                    timeout=20,
                    usage_limits=UsageLimits(request_limit=1, tool_calls_limit=0, output_tokens_limit=1400),
                )
            try:
                draft = CommunicationCard.model_validate(generated.output)
                if len(draft.title.text) > 100:
                    raise ValueError("Communication title exceeds its display bound")
                narrative = _validate_draft(draft, facts)
                expected_decision = facts.get("decision")
                if expected_decision:
                    expected_quote = {key: expected_decision[key] for key in ("question", "context", "choices")}
                    if draft.decision is None or draft.decision.model_dump() != expected_quote:
                        raise ValueError("Communication must preserve the human question and every choice")
                elif draft.decision is not None:
                    raise ValueError("Communication cannot invent a human decision")
                self.store.reject_credentials(narrative)
                title, narrative = safe(narrative).split("\n\n", 1)
            except (ValueError, TypeError):
                evidence["reason"] = "invalid_output"
                return result
            decision_body = ""
            if draft.decision:
                decision_body = prompt_text(
                    "collaboration/communication-decision",
                    question=draft.decision.question,
                    context=draft.decision.context,
                    choices="\n".join("- " + value for value in draft.decision.choices),
                )
            body = prompt_text(
                "collaboration/communication-card", title=title, narrative=narrative, decision=decision_body
            )
            if len(body) > 6000:
                evidence["reason"] = "invalid_output"
                return result
            self.store.reject_credentials(body)
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
