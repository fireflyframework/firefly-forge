# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Durable conversational intake, independent of permission to execute a task."""

from __future__ import annotations

import copy
import hashlib
import json
import stat
from pathlib import Path
from typing import Any

from pyfly.data.relational.sqlalchemy import LockMode

from firefly_forge.agent_roles import worker_feedback_model
from firefly_forge.collaboration_feedback import TERMINAL
from firefly_forge.communication import assistant_communication, communication_facts
from firefly_forge.journal import digest
from firefly_forge.project_access import AccessDenied
from firefly_forge.scm_conversation_models import AddressedContextV1, AddressedStatusV1
from firefly_forge.scm_conversations import SCMConversations, observation_cut
from firefly_forge.scm_natural_delivery import current_delivery_facts
from firefly_forge.scm_natural_models import NaturalDecision, resolve_task
from firefly_forge.scm_natural_questions import confirmed_publication, natural_questions, product_questions
from firefly_forge.scm_natural_replies import RetainedQuestionReplyV1, human_message, retained_reply
from firefly_forge.scm_natural_runtime import ConfirmedInvalidReply, classify
from firefly_forge.store import identifier

Data = dict[str, Any]


def natural_policy_digest() -> str:
    root = Path(__file__).parent
    hashes = {}
    for name in (
        "scm_natural_models.py",
        "scm_natural_product.py",
        "scm_poll_functional.py",
        "scm_poll_functional_models.py",
        "channel_products.py",
        "product_assistant.py",
        "scm_natural_runtime.py",
        "scm_natural_tasks.py",
        "scm_natural_delivery.py",
        "git_deliveries.py",
        "git_delivery_models.py",
        "scm_natural_effects.py",
        "scm_natural_checkpoints.py",
        "scm_poll_models.py",
        "scm_natural_feedback.py",
        "scm_natural_render.py",
        "scm_natural_links.py",
        "collaboration_feedback.py",
        "scm_natural_questions.py",
        "scm_natural_replies.py",
        "scm_natural_controllers.py",
        "scm_conversations.py",
        "scm_conversation_models.py",
        "scm_maintain_ticket_models.py",
        "scm_maintain_tickets.py",
        "scm_maintain_ticket_controllers.py",
        "checkpoint_test_models.py",
        "run_creation_models.py",
        "run_creation_requests.py",
        "run_creation_persistence.py",
        "scm_polling.py",
        "collaboration.py",
        "collaboration_persistence.py",
        "community_skills.py",
        "skills.py",
        "data/community-skills.json",
        "resources/prompts/scm-natural-v1/manifest.json",
        "resources/prompts/scm-natural-v1/system.yaml",
        "resources/prompts/scm-natural-v1/system-1.0.2.yaml",
        "resources/prompts/scm-natural-v1/system-1.0.3.yaml",
        "resources/prompts/scm-natural-v1/request.yaml",
        "resources/prompts/scm-natural-v1/feedback.yaml",
        "resources/prompts/scm-natural-v1/copy.yaml",
        "resources/prompts/scm-natural-v1/copy-1.0.1.yaml",
        "scm_actor_authority.py",
        "scm_authority_models.py",
        "scm_connections.py",
        "scm_comment_source.py",
    ):
        path = root / name
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1 or metadata.st_size > 262144:
            raise AccessDenied("The package-owned natural conversation policy changed")
        hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return digest(hashes)


def _status_progress_only(previous: Data, current: Data, decision: NaturalDecision) -> bool:
    if decision.intent != "status":
        return False

    def stable(context: Data) -> Data:
        value = copy.deepcopy(context)
        for candidate in value["candidates"]:
            candidate.pop("run_status", None)
            candidate.pop("current_facts", None)
        for key in ("selection", "candidate_ids", "selected_run_id"):
            value["status_context"].pop(key, None)
        return value

    return stable(previous) == stable(current) and [row.get("run_status") for row in previous["candidates"]] != [
        row.get("run_status") for row in current["candidates"]
    ]


def _worker_question(original: Data, run: Data, entries: list[Data]) -> Data:
    request = run.get("worker_request") or {}
    if (
        not request.get("id")
        or run["status"] in TERMINAL
        or (original.get("feedback_intent") or {}).get("request_id") != request["id"]
    ):
        return {}
    publication = confirmed_publication(original)
    question, question_digest, response_digest, consumed = None, None, None, False
    response_answer, response_receipt_id = None, None
    for entry in entries:
        payload = entry.get("payload") or {}
        if entry.get("kind") == "worker_feedback" and (payload.get("request") or {}).get("id") == request["id"]:
            feedback = worker_feedback_model(payload).model_validate(payload)
            assert feedback.request is not None
            expected = {
                **feedback.request.model_dump(),
                "status": request.get("status"),
                "role_id": feedback.role_id,
                "task_id": feedback.task_id,
                "attempt": feedback.attempt,
            }
            if request != expected or question is not None:
                raise AccessDenied("The current question does not have one exact immutable journal record")
            question, question_digest = feedback.request, entry["digest"]
        if entry.get("kind") == "worker_response" and payload.get("request_id") == request["id"]:
            if response_digest is not None:
                raise AccessDenied("The current question has ambiguous immutable answers")
            response_digest = entry["digest"]
            response_answer = payload.get("answer")
            response_receipt_id = (payload.get("responding_actor", {}).get("provenance") or {}).get(
                "verified_receipt_id"
            )
        if (
            entry.get("kind") in {"resume", "handoff"}
            and payload.get("human_clarification", {}).get("request_id") == request["id"]
        ):
            consumed = True
    if question is None:
        return {}
    return dict(
        question=question,
        question_digest=question_digest,
        response_digest=response_digest,
        response_answer=response_answer,
        response_receipt_id=response_receipt_id,
        consumed=consumed,
        publication=publication,
    )


class SCMNaturalTasks:
    def __init__(self, collaboration: Any) -> None:
        self.collaboration, self.store = collaboration, collaboration.store
        self.conversations = SCMConversations(collaboration)

    @staticmethod
    def require_effect_transport(delivery: Data) -> None:
        if (delivery.get("verification") or {}).get("transport") != "github_rest_poll_v1":
            raise AccessDenied("Signed natural conversation read authority does not admit polling effects")

    async def _context(self, delivery: Data, scopes: list[Data]) -> Data:
        # Lookup verifies existing authenticated ticket links; it never searches briefs or global runs.
        references, linked_runs = await self.conversations.references(delivery, scopes, durable=True)
        if len(scopes) != 1:
            raise AccessDenied("Natural tasks require one exact current native project scope")
        scope = {
            "repository_id": delivery["repository_id"],
            "project_id": scopes[0]["project_id"],
            "issue": delivery["issue"],
            "actor": delivery["actor"],
            "binding_digest": scopes[0]["binding_digest"],
            "native_user_id": scopes[0]["native_user_id"],
        }
        async with self.store._unit():
            rows = await self.collaboration.inbox.conversation_context(delivery)
            if len(rows) > 5000 or len(json.dumps([row.data for row in rows]).encode()) > 16 * 1024 * 1024:
                raise AccessDenied("Natural conversation history exceeds its bounded lookup")
            previous = [copy.deepcopy(row.data) for row in rows if row.data["id"] != delivery["id"]]
        by_id = {row["id"]: row for row in previous}
        candidates = []
        journal = self.collaboration.deployments.engine.journal if self.collaboration.deployments else None
        for reference in references:
            checkpoint_source = reference.get("kind") in {"native_checkpoint_test", "native_checkpoint_maintain"}
            original = by_id[reference["parent_receipt_id"] if checkpoint_source else reference["receipt_id"]]
            if not checkpoint_source and original["actor"] != delivery["actor"]:
                continue
            run = await self.store.get_run(reference["run_id"])
            request = (run.get("worker_request") or {}) if not checkpoint_source else {}
            async with self.store._unit():
                entries = (
                    [
                        copy.deepcopy(row.data)
                        for row in await journal.records.find_question_context(identifier(run["id"]), request["id"])
                    ]
                    if journal and request.get("id")
                    else []
                )
            if len(entries) > 2000 or len(json.dumps(entries).encode()) > 4 * 1024 * 1024:
                raise AccessDenied("The linked question journal exceeds its bounded native lookup")
            try:
                projection = _worker_question(original, run, entries) if not checkpoint_source else {}
            except (AccessDenied, ValueError):
                projection = {}
            question = projection.get("question")
            candidates.append(
                {
                    **scope,
                    "run_id": run["id"],
                    "run_status": run["status"],
                    "current_facts": communication_facts(run),
                    "delivery": await current_delivery_facts(self.store, journal, run),
                    "question_id": question.id if question else None,
                    "question_digest": projection.get("question_digest"),
                    "question": question.question if question else None,
                    "choices": question.choices if question else [],
                    "question_status": request.get("status") if question else None,
                    "response_digest": projection.get("response_digest"),
                    "response_answer": projection.get("response_answer"),
                    "response_receipt_id": projection.get("response_receipt_id"),
                    "consumed": projection.get("consumed", False),
                }
            )
            if question is not None:
                candidates[-1].update(origin="worker_journal", publication=projection["publication"])
                if original.get("maintain_ticket") is not None:
                    candidates[-1]["answer_only"] = True
            elif checkpoint_source:
                source = run["source_checkpoint_snapshot"]
                candidates[-1].update(
                    origin=reference["kind"],
                    read_only=True,
                    operation=source.get("operation", "test"),
                    source_parent_run_id=source["reference"]["run_id"],
                    source_revision=source["revision"],
                )
        manager = getattr(self.collaboration, "product_assistant", None)
        if manager is not None:
            products: dict[str, Data] = {}
            repository = await self.store.repository(delivery["repository_id"])
            host = (
                self.collaboration.scm._host(delivery["provider"]) if hasattr(self.collaboration.scm, "_host") else ""
            )
            for original in previous:
                if original.get("status") != "processed" or original.get("action_status") != "completed":
                    continue
                if original.get("actor") != delivery["actor"] or original.get("issue") != delivery["issue"]:
                    continue
                if (original.get("functional_authority") or {}).get("binding_digest") != scope["binding_digest"]:
                    continue
                assistant = assistant_communication(original, repository=repository, host=host)
                if assistant is None:
                    continue
                published = assistant.retained_reply if assistant.operation == "status" else assistant
                if published is None or not published.questions or original.get("feedback_status") != "sent":
                    continue
                if (original.get("feedback_intent") or {}).get("communication", {}).get("facts") != communication_facts(
                    None, assistant=assistant
                ):
                    continue
                if not assistant.conversation_id or not published.turn_id:
                    raise AccessDenied("The product questions have no exact completed native turn identity")
                async with self.store._unit():
                    session_row = await manager.records.find_by_id(identifier(assistant.conversation_id))
                    turn_row = await manager.turns.find_by_id(identifier(published.turn_id))
                    if (
                        session_row is None
                        or turn_row is None
                        or turn_row.status != "completed"
                        or session_row.id.hex != assistant.conversation_id
                        or session_row.repository_id.hex != delivery["repository_id"]
                        or turn_row.session_id != session_row.id
                        or turn_row.id.hex != published.turn_id
                    ):
                        raise AccessDenied("The completed product-question native producer changed")
                    events = [copy.deepcopy(row.data) for row in await manager.events.events(session_row.id)]
                    product = product_questions(
                        original,
                        copy.deepcopy(session_row.data),
                        copy.deepcopy(turn_row.data),
                        events,
                        repository=repository,
                        host=host,
                    )
                if product is None or product["project_id"] != scope["project_id"]:
                    continue
                identity = product["question_id"]
                if identity in products and products[identity]["question_digest"] != product["question_digest"]:
                    raise AccessDenied("The published product question has conflicting native producers")
                products[identity] = {**scope, **product}
            candidates.extend(products.values())
        for original in previous:
            if (
                original.get("natural_turn")
                and original["natural_turn"].get("classifier_outcome") == "confirmed_valid"
                and original.get("feedback_status") == "sent"
                and original.get("actor") == delivery["actor"]
                and original["natural_turn"]["context"]["scope"] == scope
            ):
                natural_question = natural_questions(original)
                if natural_question is not None:
                    candidates.append(natural_question)
        candidates.sort(key=lambda row: (row.get("run_id") or "", row.get("question_id") or ""))
        turns = [row for row in reversed(previous) if row["actor"] == delivery["actor"] and row.get("natural_turn")]
        history = [
            {"human": row["natural_turn"]["human_message"], "reply": row["natural_turn"].get("reply")}
            for row in turns[-20:]
        ]
        previous_resolution = next(
            (row["natural_turn"].get("resolution") for row in reversed(turns) if row["natural_turn"].get("resolution")),
            None,
        )
        for candidate in candidates:
            if candidate.get("origin") in {"product_assistant_turn", "natural_turn"}:
                answers = [
                    row
                    for row in turns
                    if (row["natural_turn"].get("resolution") or {}).get("state")
                    in {"product_answer", "conversation_answer"}
                    and row["natural_turn"]["resolution"].get("question_id") == candidate["question_id"]
                    and row["natural_turn"]["resolution"].get("question_digest") == candidate["question_digest"]
                ]
                if answers:
                    candidate.update(consumed=True, question_status="answered")
        eligible = self.conversations.current_references(references, linked_runs)
        status_context = {
            "version": 1,
            "selection": "missing" if not eligible else "ambiguous" if len(eligible) > 1 else "run",
            "scopes": scopes,
            "references": references,
            "candidate_ids": sorted(reference["run_id"] for reference in eligible),
            "selected_run_id": eligible[0]["run_id"] if len(eligible) == 1 else None,
        }
        return {
            "scope": scope,
            "candidates": candidates,
            "history": history,
            "previous": previous_resolution,
            "status_context": status_context,
        }

    async def reply_source(
        self, delivery: Data, scopes: list[Data], body: str, expected: RetainedQuestionReplyV1 | None = None
    ) -> RetainedQuestionReplyV1 | None:
        context = await self._context(delivery, scopes)
        references = []
        for row in context["candidates"]:
            retained = expected is not None and any(
                ref.question_id == row.get("question_id") and ref.question_digest == row.get("question_digest")
                for ref in expected.question_refs
            )
            if (
                not retained
                and (row.get("question_status") != "pending" or row.get("consumed") is not False)
                or not row.get("publication")
            ):
                continue
            publication = row["publication"]
            references.append(
                {
                    **{
                        key: context["scope"][key]
                        for key in ("project_id", "repository_id", "native_user_id", "actor", "issue", "binding_digest")
                    },
                    "origin": row["origin"],
                    "source_receipt_id": publication["receipt_id"],
                    "question_id": row["question_id"],
                    "question_digest": row["question_digest"],
                    "conversation_id": row.get("conversation_id"),
                    "turn_id": row.get("turn_id"),
                    "run_id": row.get("run_id"),
                    "publication_comment_id": publication["comment_id"],
                    "publication_body_sha256": publication["body_sha256"],
                    "questions": row.get("questions") or [row["question"]],
                    "pending": True,
                }
            )
        scope = context["scope"]
        return retained_reply(
            body,
            references,
            project=scope["project_id"],
            repository=scope["repository_id"],
            actor=scope["actor"],
            issue=scope["issue"],
            binding_digest=scope["binding_digest"],
            native_user_id=scope["native_user_id"],
        )

    async def _model(self, expected: Data | None = None) -> tuple[Data, dict[str, str]] | None:
        router = getattr(self.store, "task_router", None)
        selection: Data | None
        if expected is not None:
            if router is None:
                raise AccessDenied("The admitted communication route is no longer configured")
            self.collaboration.communication_writer.assert_current({"mode": "ai", "profile": expected})
            selection = expected
        else:
            selection = await router.resolve("communication") if router is not None else None
        if router is None or selection is None:
            return None
        if selection["provider"] not in {"bridge", "agentic"}:
            raise AccessDenied("Natural conversation requires a compatible configured communication route")
        settings = dict(router.profiles.validate_snapshot(selection))
        settings["ASSISTANT_PROVIDER"] = selection["provider"]
        return selection, settings

    async def authorize_feedback(self, expected: Data, *, remote: bool) -> None:
        current = await self.collaboration.receipt(expected["id"])
        if observation_cut(current) != observation_cut(expected) or current.get("natural_turn") != expected.get(
            "natural_turn"
        ):
            raise AccessDenied("The retained natural reply or exact human source changed")
        turn = current["natural_turn"]
        if turn.get("model_selection") is not None:
            await self._model(turn["model_selection"])
        if current["status"] != "processed" or turn["state"] == "needs_reconciliation":
            raise AccessDenied("An unknown natural outcome cannot produce feedback")
        scopes = await self.conversations.scopes(current, remote=remote)
        context = turn["context"]["scope"]
        if len(scopes) != 1 or any(
            scopes[0][key] != context[key] for key in ("repository_id", "project_id", "binding_digest")
        ):
            raise AccessDenied("The exact native read scope changed before natural feedback")
        if (turn.get("decision") or {}).get("intent") == "status":
            status_context = turn["context"]["status_context"]
            sealed = AddressedContextV1.model_validate({**status_context, "digest": digest(status_context)}).model_dump(
                mode="json"
            )
            if current.get("conversation") != sealed or expected.get("conversation") != sealed:
                raise AccessDenied("The retained natural status context or digest changed")
            latest = await self._context(current, scopes)
            selected = turn.get("resolution", {}).get("run_id")
            retained_rows = [row for row in turn["context"]["candidates"] if row.get("run_id") == selected]
            current_rows = [row for row in latest["candidates"] if row.get("run_id") == selected]
            if latest["status_context"] != turn["context"]["status_context"] or current_rows != retained_rows:
                raise AccessDenied("The authenticated ticket status changed before feedback")
        if natural_policy_digest() != turn["policy_digest"]:
            raise AccessDenied("The natural reply policy changed before feedback")

    async def process(self, receipt_id: str) -> Data:
        initial: Data = await self.collaboration.receipt(receipt_id)
        if initial["status"] != "received":
            return initial
        retained = initial["command"].get("adapter") == "retained_question_reply_v1"
        message = (
            RetainedQuestionReplyV1.model_validate(initial["command"])
            if retained
            else AddressedStatusV1.model_validate(initial["command"])
        )
        scopes = await self.conversations.scopes(initial, remote=True)
        context = await self._context(initial, scopes)
        source = initial["source_snapshot"]["body"]
        human = human_message(source, message)
        model = await self._model()
        policy = natural_policy_digest()
        turn = {
            "version": 1,
            "human_message": human,
            "source_digest": initial["digest"],
            "context": context,
            "policy_digest": policy,
            "state": "classifying" if model else "waiting_configuration",
            "model_selection": model[0] if model else None,
            "classifier_requests_reserved": 1 if model else 0,
            "reply": None,
        }
        self.store.reject_credentials(turn)
        await self.store.admit_credentials(turn)
        async with self.conversations._locked(scopes, initial["repository_id"]):
            row = await self.collaboration.inbox.find_by_id(identifier(receipt_id), lock=LockMode.PESSIMISTIC_WRITE)
            if row is None or observation_cut(row.data) != observation_cut(initial):
                raise AccessDenied("The original natural comment changed before reservation")
            if row.status != "received":
                return copy.deepcopy(row.data)
            if (
                await self.conversations.scopes(initial, remote=False) != scopes
                or await self._context(initial, scopes) != context
            ):
                raise AccessDenied("The current natural question or read scope changed before reservation")
            await self.collaboration._write_row(
                row,
                status="needs_reconciliation" if model else "processed",
                action_status="writing" if model else "completed",
                natural_turn=turn,
                feedback_status="not_requested" if model else "pending",
                reason="natural_classifier_reserved" if model else "communication_route_required",
            )
            reserved: Data = copy.deepcopy(row.data)
        if model is None:
            return reserved
        repaired, refreshed = False, False
        repair_errors: list[Data] | None = None
        try:
            while True:
                try:
                    decision = await classify(
                        human,
                        {**context, "repair_contract_errors": repair_errors} if repair_errors else context,
                        model[1],
                    )
                except ConfirmedInvalidReply as invalid:
                    turn = {
                        **turn,
                        "classifier_outcome": "confirmed_invalid",
                        "validation_errors": invalid.errors,
                        "classifier_attempts": [
                            *turn.get("classifier_attempts", []),
                            {
                                "request_index": turn["classifier_requests_reserved"],
                                "context_digest": digest(context),
                                "outcome": "confirmed_invalid",
                                "validation_errors": invalid.errors,
                            },
                        ],
                    }
                    if repaired:
                        raise
                    current = await self.collaboration.receipt(receipt_id)
                    repair_scopes = await self.conversations.scopes(current, remote=True)
                    repair_model = await self._model(model[0])
                    if (
                        observation_cut(current) != observation_cut(initial)
                        or repair_scopes != scopes
                        or await self._context(current, repair_scopes) != context
                        or repair_model is None
                        or repair_model[0] != model[0]
                        or natural_policy_digest() != policy
                    ):
                        raise AccessDenied(
                            "The exact natural reply context changed before its one correction"
                        ) from None
                    async with self.conversations._locked(scopes, initial["repository_id"]):
                        row = await self.collaboration.inbox.find_by_id(
                            identifier(receipt_id), lock=LockMode.PESSIMISTIC_WRITE
                        )
                        if row is None or row.data != reserved:
                            return dict(await self.collaboration.receipt(receipt_id))
                        if (
                            await self.conversations.scopes(row.data, remote=False) != scopes
                            or await self._context(row.data, scopes) != context
                        ):
                            raise AccessDenied(
                                "The natural reply read scope changed before correction reservation"
                            ) from None
                        turn = {
                            **turn,
                            "classifier_requests_reserved": turn["classifier_requests_reserved"] + 1,
                            "classifier_outcome": "pending",
                            "state": "correcting",
                        }
                        await self.collaboration._write_row(
                            row, natural_turn=turn, reason="natural_correction_reserved"
                        )
                        reserved = copy.deepcopy(row.data)
                    repaired, repair_errors = True, invalid.errors
                    continue
                completed = {
                    **turn,
                    "classifier_outcome": "confirmed_valid",
                    "classifier_attempts": [
                        *turn.get("classifier_attempts", []),
                        {
                            "request_index": turn["classifier_requests_reserved"],
                            "context_digest": digest(context),
                            "outcome": "confirmed_valid",
                            "decision": decision.model_dump(mode="json"),
                        },
                    ],
                }
                self.store.reject_credentials(completed)
                await self.store.admit_credentials(completed)
                turn = completed
                current = await self.collaboration.receipt(receipt_id)
                current_scopes = await self.conversations.scopes(current, remote=True)
                current_context = await self._context(current, current_scopes)
                current_model = await self._model(model[0])
                if (
                    observation_cut(current) != observation_cut(initial)
                    or current_scopes != scopes
                    or current_model is None
                    or current_model[0] != model[0]
                    or natural_policy_digest() != policy
                    or refreshed
                    and decision.intent != "status"
                ):
                    raise AccessDenied(
                        "The native source, read permission, model or policy changed during classification"
                    )
                if current_context != context and (
                    refreshed or not _status_progress_only(context, current_context, decision)
                ):
                    raise AccessDenied("The exact natural task context changed during classification")
                async with self.conversations._locked(scopes, initial["repository_id"]):
                    row = await self.collaboration.inbox.find_by_id(
                        identifier(receipt_id), lock=LockMode.PESSIMISTIC_WRITE
                    )
                    if row is None or row.data != reserved:
                        return dict(await self.collaboration.receipt(receipt_id))
                    if await self.conversations.scopes(row.data, remote=False) != scopes:
                        raise AccessDenied("The current membership changed before retaining the answer")
                    latest = await self._context(row.data, scopes)
                    if latest != context:
                        if refreshed or not _status_progress_only(context, latest, decision):
                            raise AccessDenied("The exact natural task context changed before retaining the answer")
                        context = latest
                        turn = {
                            **turn,
                            "context": context,
                            "classifier_requests_reserved": turn["classifier_requests_reserved"] + 1,
                            "classifier_context_refreshes": 1,
                            "classifier_outcome": "pending",
                            "state": "refreshing_status",
                        }
                        await self.collaboration._write_row(
                            row, natural_turn=turn, reason="natural_status_refresh_reserved"
                        )
                        reserved = copy.deepcopy(row.data)
                        refreshed, repair_errors = True, None
                        continue
                    resolution = resolve_task(
                        context["scope"],
                        context["candidates"],
                        decision,
                        answer=human,
                        previous=context["previous"],
                    )
                    status_context = context["status_context"]
                    if decision.intent in {"status", "checkpoint"}:
                        resolution = resolution.model_copy(
                            update={
                                "state": decision.intent if status_context["selection"] == "run" else "waiting",
                                "reason": (
                                    "authenticated_ticket_status_reference"
                                    if decision.intent == "status"
                                    else "authenticated_ticket_checkpoint_reference"
                                )
                                if status_context["selection"] == "run"
                                else "clarification_required",
                                "run_id": status_context["selected_run_id"],
                            }
                        )
                    output = {
                        **turn,
                        "state": "answer_pending_permission" if resolution.state == "answer" else resolution.state,
                        "decision": decision.model_dump(mode="json"),
                        "resolution": resolution.model_dump(mode="json"),
                        "reply": decision.message,
                        "classifier_outcome": "confirmed_valid",
                    }
                    self.store.reject_credentials(output)
                    await self.collaboration._write_row(
                        row,
                        status="processed",
                        action_status="completed",
                        natural_turn=output,
                        reason=None,
                        feedback_status="pending",
                        run_id=resolution.run_id,
                        **(
                            {
                                "conversation": AddressedContextV1.model_validate(
                                    {**status_context, "digest": digest(status_context)}
                                ).model_dump(mode="json")
                            }
                            if decision.intent == "status"
                            else {}
                        ),
                    )
                    return copy.deepcopy(row.data)
        except Exception as error:
            async with self.store._unit():
                row = await self.collaboration.inbox.find_by_id(identifier(receipt_id), lock=LockMode.PESSIMISTIC_WRITE)
                if row is not None and row.data == reserved:
                    await self.collaboration._write_row(
                        row,
                        natural_turn={
                            **turn,
                            "state": "invalid_reply"
                            if isinstance(error, ConfirmedInvalidReply)
                            else "needs_reconciliation",
                            "classifier_outcome": "confirmed_invalid"
                            if isinstance(error, ConfirmedInvalidReply)
                            else turn["classifier_outcome"]
                            if turn.get("classifier_outcome") in {"confirmed_valid", "confirmed_invalid"}
                            else "unknown",
                        },
                        action_status="needs_reconciliation",
                        reason="natural_reply_invalid_after_correction"
                        if isinstance(error, ConfirmedInvalidReply)
                        else "natural_completed_reply_held"
                        if turn.get("classifier_outcome") == "confirmed_valid"
                        else "natural_outcome_unconfirmed",
                        feedback_status="held",
                    )
            return dict(await self.collaboration.receipt(receipt_id))
