# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Independent, bounded notifications for immutable native worker questions."""

from __future__ import annotations

import copy
import hashlib
import json
import time
from typing import Any, Literal

from pydantic import ConfigDict, Field
from pyfly.data.relational.sqlalchemy import LockMode

from firefly_forge.agent_roles import worker_feedback_model
from firefly_forge.collaboration_persistence import ForgeWebhookInboxEntity
from firefly_forge.communication import communication_facts
from firefly_forge.configuration import redact_secrets
from firefly_forge.journal import Journal, digest
from firefly_forge.native_question_owned_unit import question_owned_unit
from firefly_forge.project_access import AccessDenied, ProjectAccess
from firefly_forge.scm_authority_models import Digest, NativeID, StrictContract, canonical_provider_id
from firefly_forge.scm_poll_models import GitHubPollReferenceV1
from firefly_forge.scm_polling import BINDING_KIND, MAX_BYTES, inbox_size, poll_key
from firefly_forge.scm_provider_backoff import ProviderRequestDeferred, retained_deferred
from firefly_forge.scm_source_edit_models import SCMSourceEditCommandV1
from firefly_forge.secrets import redact_environment
from firefly_forge.source_edit_models import PROTOCOL
from firefly_forge.source_human_readmission import _ancestral_unknown, _validated_parent
from firefly_forge.store import identifier, now

Data = dict[str, Any]
RESERVED = "native_question_reserved"
INTENT = "native_question_intent"
RECEIPT = "native_question_receipt"
DEFERRED = "native_question_deferred"
MAX_QUESTIONS = 3


class NativeQuestionReferenceV1(StrictContract):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True)
    version: Literal[1]
    kind: Literal["native_worker_question_v1"]
    run_id: NativeID
    question_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}$")
    feedback_digest: Digest
    activation_digest: Digest
    source_receipt_id: NativeID
    source_authority_digest: Digest
    verified_reference_digest: Digest
    binding_digest: Digest
    native_user_id: NativeID
    native_identity_digest: Digest
    question_cut: Digest


class NativeQuestionPublications:
    def __init__(self, collaboration: Any) -> None:
        self.c, self.store = collaboration, collaboration.store
        self.polling = collaboration.polling
        self.journal: Journal | None = collaboration.deployments.engine.journal if collaboration.deployments else None

    def _journal(self) -> Journal:
        if self.journal is None:
            raise AccessDenied("Native question publication requires the native journal")
        return self.journal

    async def _entries(self, run_id: str) -> list[Data]:
        if self.journal is None:
            raise AccessDenied("Native question publication requires the native journal")
        async with self.store._unit():
            rows = await self.journal.records.find_for_run(identifier(run_id))
            entries = [copy.deepcopy(row.data) for row in rows]
        if (
            len(entries) > 10000
            or len(json.dumps(entries).encode()) > 16 * 1024 * 1024
            or not Journal.verify_chain(entries)
        ):
            raise AccessDenied("Native question publication requires an intact bounded journal")
        return entries

    @staticmethod
    def _source(entries: list[Data], run: Data) -> tuple[Data, Data]:
        marker = run.get("source_edit_continuation") or {}
        event = next(
            (
                e
                for e in entries
                if e["kind"] == "workspace_reconciliation_receipt"
                and e["idempotency_key"] == marker.get("request_id")
                and e["digest"] == marker.get("receipt_digest")
            ),
            None,
        )
        if event is None:
            raise AccessDenied("The pending question has no exact completed native activation")
        actor = event["payload"]["actor"]
        observed = _ancestral_unknown(entries, event, actor)
        from firefly_forge.source_budget_allowances import unwrap_budget_parents

        current = unwrap_budget_parents(entries, event, actor)
        for _ in range(4):
            parent, candidate = _validated_parent(entries, current, actor)
            if (
                current["run_id"] != run["id"]
                or candidate.get("run_id") != run["id"]
                or candidate.get("source_edit", {}).get("protocol") != PROTOCOL
                or parent["previous_task"] != candidate.get("previous_task")
            ):
                raise AccessDenied("The completed ancestry is not the exact source-only native run")
            if evidence := parent.get("scm_source_activation"):
                return event, evidence
            if observed:
                original = next(e for e in entries if e["digest"] in observed)
                evidence = original["payload"].get("scm_source_activation")
                if evidence:
                    return event, evidence
                break
            if candidate.get("completed_output_readmission") is not None:
                from firefly_forge.source_result_readmission import validate_completed_output_parent

                current = validate_completed_output_parent(entries, current, actor)
                continue
            human = candidate.get("human_readmission")
            if human is None:
                break
            previous = human["parent_marker"]
            current = next(
                e
                for e in entries
                if e["kind"] == "workspace_reconciliation_receipt"
                and e["digest"] == previous["receipt_digest"]
                and e["idempotency_key"] == previous["request_id"]
            )
        raise AccessDenied("The native activation has no exact original SCM provenance")

    async def _context(
        self, run_id: str, *, remote: bool = False, pending: bool = True, archived_binding: str | None = None
    ) -> Data:
        if archived_binding is not None and (pending or remote):
            raise AccessDenied("Historical question proof is only available for an existing published reference")
        if self.polling is None or self.journal is None:
            raise AccessDenied("Native question notifications require current polling authority")
        run = await self.store.get_run(run_id)
        entries = await self._entries(run_id)
        request = run.get("worker_request") or {}
        matches = [
            e
            for e in entries
            if e["kind"] == "worker_feedback" and (e["payload"].get("request") or {}).get("id") == request.get("id")
        ]
        if len(matches) != 1:
            raise AccessDenied("The current question requires one immutable worker feedback record")
        feedback_event = matches[0]
        feedback = worker_feedback_model(feedback_event["payload"]).model_validate(feedback_event["payload"])
        expected = (
            {
                **feedback.request.model_dump(),
                "status": request.get("status"),
                "role_id": feedback.role_id,
                "task_id": feedback.task_id,
                "attempt": feedback.attempt,
            }
            if feedback.request
            else {}
        )
        if (
            request != expected
            or feedback.role_id != "implementer"
            or feedback.task_id != "implement"
            or run.get("active_task") != "implement"
            or pending
            and (run.get("status") != "paused" or request.get("status") != "pending")
            or any(
                e["kind"] == "worker_response" and e["payload"].get("request_id") == request.get("id") for e in entries
            )
            and pending
        ):
            raise AccessDenied("The exact native implementer question is no longer pending")
        activation, evidence = self._source(entries, run)
        if activation["sequence"] >= feedback_event["sequence"]:
            raise AccessDenied("The question must follow its completed source activation")
        source = await self.c.receipt(evidence["receipt_id"])
        reference = GitHubPollReferenceV1.model_validate(source.get("verification")).model_dump(mode="json")
        command = SCMSourceEditCommandV1.model_validate(source.get("command"))
        if (
            source.get("source_edit_authority") != evidence
            or evidence.get("transport") != "github_rest_poll_source_edit_v1"
            or evidence.get("verified_reference_digest") != digest(reference)
            or source.get("source_conflict")
            or source.get("provider") != "github"
            or source.get("digest") != reference["payload_digest"]
            or digest(source.get("source_snapshot")) != reference["payload_digest"]
            or command.expected_grant_digest != evidence.get("grant_digest")
            or source.get("repository_id") != run.get("repository_id")
            or source.get("issue") != {"kind": "issue", "number": reference["issue_number"]}
            or source.get("actor") != reference["external_actor_id"]
            or source.get("remote_id") != reference["remote_repository_id"]
            or (source.get("command") or {}).get("run_id") != run_id
        ):
            raise AccessDenied("The native question lost its exact original SCM issue and actor")
        project, repository = run.get("project_id"), run["repository_id"]
        policy, state = await self.polling._policy(repository)
        repo = await self.polling._scope(repository, policy)
        binding = await self.polling._head(project, repository, poll_key(project, reference))
        if (
            binding is None
            or policy["project_id"] != project
            or not state["cursor"]["ready"]
            or not binding["request"]["active"]
            or "status" not in binding["request"]["allowed_actions"]
            or archived_binding is None
            and binding["digest"] != evidence["binding_digest"]
            or binding["request"]["native_user_id"] != evidence["native_user_id"]
            or run.get("component_id") not in binding["request"]["component_ids"]
            or binding["project_binding_digest"] != digest(await self.polling.authority._project(project, repository))
            or any(binding["verified_reference"].get(k) != v for k, v in self.c.scm.delegation_scope(repo).items())
        ):
            raise AccessDenied("Current exact status-read consent is required for question notification")
        proof_binding = binding
        if archived_binding is not None:
            pointers = [
                pointer
                for request_id, pointer in state["requests"].items()
                if request_id.startswith(project + ":") and pointer["digest"] == archived_binding
            ]
            if archived_binding != evidence["binding_digest"] or len(pointers) != 1:
                raise AccessDenied("The published question lost its exact historical polling consent")
            proof_binding = await self.polling._event(pointers[0], BINDING_KIND, project, repository)
            historical = proof_binding["request"]
            if (
                not historical["active"]
                or "status" not in historical["allowed_actions"]
                or historical["native_user_id"] != evidence["native_user_id"]
                or proof_binding["binding_key"] != binding["binding_key"]
                or proof_binding["project_binding_digest"] != binding["project_binding_digest"]
                or sorted(historical["component_ids"]) != sorted(binding["request"]["component_ids"])
            ):
                raise AccessDenied("The historical publication and current owner read scope differ")
        async with self.store._unit():
            user = await self.polling.users.find_by_id(
                identifier(evidence["native_user_id"]), lock=LockMode.PESSIMISTIC_WRITE
            )
            members = await self.polling.memberships.for_user(identifier(evidence["native_user_id"]))
            settings, current_state = await self.polling._state(repository)
            if (
                archived_binding is not None
                and (
                    current_state["configuration"] != state["configuration"]
                    or current_state["heads"].get(binding["binding_key"]) != state["heads"].get(binding["binding_key"])
                )
                or user is None
                or not user.enabled
                or not settings.data.get("enabled")
                or source["actor"] not in settings.data.get("trusted_actors", [])
                or "status"
                not in [*settings.data.get("allowed_commands", []), *settings.data.get("channel_commands", [])]
                or not any(
                    m.project_id.hex == project and m.active and ProjectAccess.permits(m.role, "read") for m in members
                )
            ):
                raise AccessDenied("The mapped native identity needs current project read permission")
            identity = digest({"id": user.id.hex, "issuer": user.issuer, "subject": user.subject})
        cut = {
            key: run.get(key)
            for key in (
                "status",
                "active_task",
                "worker_request",
                "source_edit_continuation",
                "agent",
                "config_revision",
                "execution_principal",
                "role_snapshot",
            )
        }
        native = dict(
            version=1,
            kind="native_worker_question_v1",
            run_id=run_id,
            question_id=request["id"],
            feedback_digest=feedback_event["digest"],
            activation_digest=activation["digest"],
            source_receipt_id=source["id"],
            source_authority_digest=digest(evidence),
            verified_reference_digest=digest(reference),
            binding_digest=proof_binding["digest"],
            native_user_id=user.id.hex,
            native_identity_digest=identity,
            question_cut=digest(cut),
        )
        native = NativeQuestionReferenceV1.model_validate(native).model_dump(mode="json")
        self.store.reject_credentials(native)
        if remote:
            await self.polling._fresh(source, repo)
            latest = await self._context(run_id, pending=pending)
            if latest["native"] != native:
                raise AccessDenied("The native notification authority changed during remote preflight")
        return dict(native=native, run=run, source=source, repo=repo, entries=entries, policy=policy)

    @staticmethod
    def _reservation(context: Data, data: Data) -> None:
        events = [
            e for e in context["entries"] if e["kind"] == RESERVED and e["payload"].get("receipt_id") == data["id"]
        ]
        if len(events) != 1 or events[0]["payload"]["native_question"] != data.get("native_question"):
            raise AccessDenied("The question notification lost its immutable outbox reservation")

    async def authorize_reference(self, data: Data, run: Data) -> None:
        expected = NativeQuestionReferenceV1.model_validate(data.get("native_question")).model_dump(mode="json")
        context = await self._context(run["id"], pending=False, archived_binding=expected["binding_digest"])
        fields = set(context["native"]) - {"question_cut"}
        if any(expected.get(k) != context["native"][k] for k in fields):
            raise AccessDenied("The published question belongs to another native producer or current channel")
        self._reservation(context, data)
        intents = [
            e for e in context["entries"] if e["kind"] == INTENT and e["payload"].get("receipt_id") == data["id"]
        ]
        if len(intents) != 1 or {k: v for k, v in intents[0]["payload"].items() if k != "receipt_id"} != data.get(
            "feedback_intent"
        ):
            raise AccessDenied("The published question differs from its immutable delivery intent")

    async def _current(self, data: Data, *, remote: bool = False) -> Data:
        NativeQuestionReferenceV1.model_validate(data.get("native_question"))
        context = await self._context(data["run_id"], remote=remote)
        self._reservation(context, data)
        async with self.store._unit():
            peers = [r.data for r in await self.c.inbox.feedback_context(data)]
        if len(peers) > 200 or any(
            p["id"] != data["id"] and p.get("feedback_status") in {"writing", "needs_reconciliation"} for p in peers
        ):
            raise AccessDenied("Another remote question or feedback outcome remains uncertain")
        if (
            context["native"] != data.get("native_question")
            or data["issue"] != context["source"]["issue"]
            or data["actor"] != context["source"]["actor"]
        ):
            raise AccessDenied("The exact native question or notification scope changed")
        return context

    async def produce(self, run_id: str) -> Data:
        await self.store.assert_ownership()
        context = await self._context(run_id)
        native = context["native"]
        key = digest(
            {
                "run_id": run_id,
                "feedback": native["feedback_digest"],
                "question": native["question_id"],
                "repository": context["source"]["repository_id"],
                "issue": context["source"]["issue"],
            }
        )
        ownership = self.store.execution_ownership_token
        async with (
            question_owned_unit(self.store, ownership),
            self.polling._locked(
                context["run"]["project_id"],
                context["repo"]["id"],
                [native["native_user_id"], context["policy"]["admin_snapshot"]["user_id"]],
            ),
        ):
            await self._journal()._run(run_id)
            latest = await self._context(run_id)
            if latest["native"] != native:
                raise AccessDenied("The native question changed before reservation")
            existing = await self.c.inbox.find_source(
                identifier(context["repo"]["id"]), "github", "native-question:" + key
            )
            if existing is not None:
                return copy.deepcopy(existing.data)
            previous = [e for e in latest["entries"] if e["kind"] == RESERVED]
            maximum = context["run"].get("role_snapshot", {}).get("max_requests", MAX_QUESTIONS)
            if type(maximum) is not int or not 1 <= maximum <= MAX_QUESTIONS or len(previous) >= maximum:
                raise AccessDenied("The frozen native question notification allowance is exhausted")
            if any(e["payload"]["native_question"]["question_id"] == native["question_id"] for e in previous):
                raise AccessDenied("This native question was already reserved in another scope")
            source = context["source"]
            data = {
                k: copy.deepcopy(source[k])
                for k in (
                    "repository_id",
                    "provider",
                    "remote_id",
                    "actor",
                    "issue",
                    "verification",
                    "source_snapshot",
                )
            }
            data.update(
                id=key[:32],
                delivery_id="native-question:" + key,
                digest=digest(native),
                status="processed",
                action_status="completed",
                reason=None,
                run_id=run_id,
                feedback_status="pending",
                command={"action": "status", "run_id": run_id, "language": source["command"].get("language", "en")},
                native_question=native,
                global_ingress=False,
                created_at=now(),
                updated_at=now(),
            )
            peers = [r.data for r in await self.c.inbox.feedback_context(data)]
            if len(peers) > 200 or any(r.get("feedback_status") in {"writing", "needs_reconciliation"} for r in peers):
                raise AccessDenied("An existing remote feedback outcome must be reconciled first")
            published = [
                r
                for r in peers
                if r.get("feedback_status") == "sent"
                and (r.get("feedback_intent") or {}).get("request_id") == native["question_id"]
            ]
            if published:
                return copy.deepcopy(published[0])
            count, retained = await inbox_size(self.store, data["repository_id"])
            if count >= 5000 or retained + len(json.dumps(data, ensure_ascii=False).encode()) > MAX_BYTES:
                raise AccessDenied("Native question inbox retention is full")
            self.store.reject_credentials(data)
            await self._journal()._append(
                run_id, "implement", key, RESERVED, {"receipt_id": data["id"], "native_question": native}
            )
            await self.c.inbox.save(
                ForgeWebhookInboxEntity(
                    id=identifier(data["id"]),
                    repository_id=identifier(data["repository_id"]),
                    provider="github",
                    delivery_id=data["delivery_id"],
                    source_key="native-question:" + key,
                    status=data["status"],
                    data=data,
                )
            )
            return copy.deepcopy(data)

    async def _save(self, expected: Data, ownership: object, **fields: Any) -> Data | None:
        async with question_owned_unit(self.store, ownership):
            if fields.get("feedback_status") == "writing":
                await self._current(expected)
            row = await self.c.inbox.find_by_id(identifier(expected["id"]), lock=LockMode.PESSIMISTIC_WRITE)
            if row is None or row.status != expected["status"] or row.data != expected:
                return None
            await self.c._write_row(row, **fields)
            result: Data = copy.deepcopy(row.data)
            return result

    async def publish(self, receipt_id: str) -> None:
        initial = await self.c.receipt(receipt_id)
        if initial.get("feedback_status") != "pending":
            return
        ownership = self.store.execution_ownership_token
        admitted = None
        active = False
        started = False
        deferred = None
        intent: Data = {"body": "", "communication": {"mode": "held", "reason": "native_question_not_current"}}
        try:
            deferred = retained_deferred(initial)
            if deferred is not None and deferred["retry_at"] > time.time():
                return
            context = await self._current(initial)
            admitted = await self._save(initial, ownership, feedback_status="writing")
            if admitted is None:
                return
            self.c._active_writes.add(receipt_id)
            active = True
            context = await self._current(admitted, remote=True)
            run = context["run"]
            from firefly_forge.collaboration_feedback import feedback_card

            def safe(value: str) -> str:
                self.store.reject_credentials(value)
                return redact_environment(redact_secrets(value, self.c.scm.redaction_settings()))

            enriched = {**admitted, "_feedback_origin": self.c.browser_origin, "_feedback_actions": []}
            native_card = feedback_card(enriched, run, context["repo"], self.c.scm._host("github"), safe)
            retained = initial.get("feedback_intent") if deferred else None
            if retained is not None:
                assert deferred is not None
                prior = [
                    e["payload"]
                    for e in context["entries"]
                    if e["kind"] == INTENT and e["payload"].get("receipt_id") == receipt_id
                ]
                proof = {
                    "receipt_id": receipt_id,
                    "intent_sha256": deferred["intent_sha256"],
                    "outcome": "not_dispatched",
                }
                if (
                    prior != [{"receipt_id": receipt_id, **retained}]
                    or len([e for e in context["entries"] if e["kind"] == DEFERRED and e["payload"] == proof]) != 1
                    or any(
                        e["kind"] == RECEIPT and e["payload"].get("receipt_id") == receipt_id
                        for e in context["entries"]
                    )
                    or not isinstance(retained.get("body"), str)
                    or hashlib.sha256(retained["body"].encode()).hexdigest() != retained.get("body_sha256")
                ):
                    raise AccessDenied("The native question nondispatch provenance changed")
                drafted = {"body": retained["body"], "communication": retained["communication"]}
            else:
                drafted = await self.c.communication_writer.write(run, native_card, safe)
            if drafted["communication"]["mode"] == "held":
                await self._save(
                    admitted,
                    ownership,
                    feedback_status="held",
                    feedback_intent={"body": "", "communication": drafted["communication"]},
                    reason="ai_communication_" + drafted["communication"]["reason"],
                )
                return
            if not drafted["body"] or len(drafted["body"]) > 6000:
                raise ValueError("Native question feedback exceeds its public text bound")
            if digest(communication_facts(run)) != drafted["communication"]["facts_sha256"]:
                raise AccessDenied("The question writer did not retain its exact typed facts")
            self.c.communication_writer.assert_current(drafted["communication"])
            intent = dict(
                key=admitted["digest"],
                body=drafted["body"],
                communication=drafted["communication"],
                body_sha256=hashlib.sha256(drafted["body"].encode()).hexdigest(),
                stage="paused",
                request_id=admitted["native_question"]["question_id"],
            )
            if retained is not None and intent != retained:
                raise AccessDenied("The original native question publication intent changed")
            await self._current(admitted, remote=True)
            await self.store.admit_credentials(intent)
            async with question_owned_unit(self.store, ownership):
                await self._journal()._run(admitted["run_id"])
                await self._current(admitted)
                row = await self.c.inbox.find_by_id(identifier(receipt_id), lock=LockMode.PESSIMISTIC_WRITE)
                if row is None or row.data != admitted or row.status != admitted["status"]:
                    return
                await self._journal()._append(
                    admitted["run_id"], "implement", admitted["digest"], INTENT, {"receipt_id": receipt_id, **intent}
                )
                await self.c._write_row(row, feedback_intent=intent)
                admitted = copy.deepcopy(row.data)
            token, api = await self.c.scm.issue_authentication(context["repo"])
            await self._current(admitted, remote=True)
            async with (
                question_owned_unit(self.store, ownership, complete_during_drain=True),
                self.polling._locked(
                    context["run"]["project_id"],
                    admitted["repository_id"],
                    [admitted["native_question"]["native_user_id"], context["policy"]["admin_snapshot"]["user_id"]],
                ),
            ):
                await self._journal()._run(admitted["run_id"])
                await self._current(admitted)
                row = await self.c.inbox.find_by_id(identifier(receipt_id), lock=LockMode.PESSIMISTIC_WRITE)
                if row is None or row.data != admitted or row.status != admitted["status"]:
                    return
                self.store.reject_credentials(intent)
                self.c.communication_writer.assert_current(intent["communication"])
                if (
                    getattr(self.store, "execution_stopping", False)
                    or ownership is not self.store.execution_ownership_token
                ):
                    raise AccessDenied("Question notification dispatch is closed while the owner drains")
                started = True
                try:
                    response = await self.c.scm._request(
                        "POST",
                        api + self.c._issue_path("github", context["repo"], admitted["issue"]),
                        headers={"Authorization": "Bearer " + token, "Accept": "application/json"},
                        json={"body": intent["body"]},
                    )
                    result = response.json()
                    comment_id = result.get("id") if isinstance(result, dict) else None
                    if type(comment_id) not in {str, int}:
                        raise ValueError("The question publication has no exact GitHub comment identity")
                    canonical_id = canonical_provider_id("github", str(comment_id))
                    expected_url = (
                        self.c._ticket_url(context["repo"], admitted["issue"]) + "#issuecomment-" + canonical_id
                    )
                    if result.get("html_url") != expected_url:
                        raise ValueError("The question publication does not confirm its exact GitHub issue scope")
                    remote = {
                        "id": result.get("id"),
                        "url": result.get("html_url"),
                        "number": result.get("number", result.get("id")),
                    }
                except ProviderRequestDeferred as error:
                    started = False
                    if row.data == admitted and row.status == admitted["status"]:
                        await self._journal()._append(
                            admitted["run_id"],
                            "implement",
                            admitted["digest"],
                            DEFERRED,
                            {
                                "receipt_id": receipt_id,
                                "intent_sha256": error.evidence(intent)["intent_sha256"],
                                "outcome": "not_dispatched",
                            },
                        )
                        await self.c._write_row(
                            row,
                            feedback_status="pending",
                            reason="provider_request_deferred",
                            feedback_deferred=error.evidence(intent),
                        )
                except Exception:
                    if row.data == admitted and row.status == admitted["status"]:
                        await self._journal()._append(
                            admitted["run_id"],
                            "implement",
                            admitted["digest"],
                            RECEIPT,
                            {
                                "receipt_id": receipt_id,
                                "feedback_digest": admitted["native_question"]["feedback_digest"],
                                "body_sha256": intent["body_sha256"],
                                "outcome": "unknown",
                                "remote": None,
                            },
                        )
                        await self.c._write_row(
                            row,
                            feedback_status="needs_reconciliation",
                            reason="native_question_outcome_unknown",
                            feedback_deferred=None,
                        )
                else:
                    if row.data == admitted and row.status == admitted["status"]:
                        await self._journal()._append(
                            admitted["run_id"],
                            "implement",
                            admitted["digest"],
                            RECEIPT,
                            {
                                "receipt_id": receipt_id,
                                "feedback_digest": admitted["native_question"]["feedback_digest"],
                                "body_sha256": intent["body_sha256"],
                                "outcome": "sent",
                                "remote": remote,
                            },
                        )
                        await self.c._write_row(
                            row,
                            feedback_status="sent",
                            feedback_deferred=None,
                            feedback_remote=remote,
                            feedback_request_id=intent["request_id"],
                            feedback_stage="paused",
                            feedback_key=intent["key"],
                            feedback_history=[{**intent, "remote": remote, "created_at": now()}],
                        )
        except ProviderRequestDeferred as error:
            expected = admitted or initial
            kept = intent if intent.get("body") else expected.get("feedback_intent")
            try:
                async with question_owned_unit(self.store, ownership):
                    context = await self._current(expected)
                    row = await self.c.inbox.find_by_id(identifier(receipt_id), lock=LockMode.PESSIMISTIC_WRITE)
                    if row is None or row.data != expected or row.status != expected["status"]:
                        return
                    if kept is not None:
                        self.c.communication_writer.assert_current(kept["communication"])
                        if digest(communication_facts(context["run"])) != kept["communication"]["facts_sha256"]:
                            raise AccessDenied("The deferred question facts changed")
                        await self._journal()._append(
                            expected["run_id"],
                            "implement",
                            expected["digest"],
                            INTENT,
                            {"receipt_id": receipt_id, **kept},
                        )
                        await self._journal()._append(
                            expected["run_id"],
                            "implement",
                            expected["digest"],
                            DEFERRED,
                            {
                                "receipt_id": receipt_id,
                                "intent_sha256": error.evidence(kept)["intent_sha256"],
                                "outcome": "not_dispatched",
                            },
                        )
                    await self.c._write_row(
                        row,
                        feedback_status="pending",
                        feedback_intent=kept,
                        feedback_deferred=error.evidence(kept),
                        reason="provider_request_deferred",
                    )
            except Exception:
                try:
                    await self._save(
                        expected, ownership, feedback_status="held", reason="deferred_question_context_changed"
                    )
                except Exception:
                    return
        except Exception:
            try:
                if admitted is None:
                    self._reservation({"entries": await self._entries(initial["run_id"])}, initial)
                await self._save(
                    admitted or initial,
                    ownership,
                    feedback_status="needs_reconciliation" if started else "held",
                    feedback_intent=initial.get("feedback_intent")
                    if deferred
                    else intent
                    if started
                    else {"body": "", "communication": {"mode": "held", "reason": "native_question_not_current"}},
                    reason="native_question_outcome_unknown" if started else "native_question_not_current",
                )
            except Exception:
                # A lost lease cannot persist even a hold over its successor's custody.
                return
        finally:
            if active:
                self.c._active_writes.discard(receipt_id)

    async def interrupted(self, data: Data) -> None:
        if data["id"] in self.c._active_writes or data.get("feedback_status") != "writing":
            return
        ownership = self.store.execution_ownership_token
        await self._save(data, ownership, feedback_status="needs_reconciliation", reason="native_question_interrupted")

    async def sync(self) -> None:
        if self.polling is None or self.journal is None or self.c.source_edit_authority is None:
            return
        async with self.store._unit():
            rows = await self.store.run_records.find_native_worker_questions()
            ids = [r.id.hex for r in rows]
        for run_id in ids:
            try:
                record = await self.produce(run_id)
                await self.publish(record["id"])
            except (AccessDenied, KeyError, ValueError):
                continue
