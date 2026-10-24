# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Exact command projections retain their human source and separate execution authority."""

from __future__ import annotations

import copy
import json
from contextlib import suppress
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from pyfly.data.relational.sqlalchemy import LockMode

from firefly_forge.channel_actions import ChannelCommand
from firefly_forge.collaboration_persistence import ForgeWebhookInboxEntity
from firefly_forge.journal import digest
from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied, ProjectAccess
from firefly_forge.scm_resume_models import SCMResumeCommandV2
from firefly_forge.store import identifier

Data = dict[str, Any]


class NaturalReadmission(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    request_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    expected_intake_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    expected_cut_digest: str = Field(pattern=r"^[a-f0-9]{64}$")


def intake_digest(parent: Data) -> str:
    turn = parent["natural_turn"]
    return digest(
        {
            key: turn.get(key)
            for key in (
                "version",
                "human_message",
                "source_digest",
                "context",
                "policy_digest",
                "model_selection",
                "decision",
                "resolution",
            )
        }
    )


def effect_state(parent: Data) -> str:
    return digest(
        {
            key: parent.get(key)
            for key in (
                "status",
                "action_status",
                "reason",
                "source_conflict",
                "natural_turn",
                "natural_effects",
                "natural_admissions",
            )
        }
    )


def answer_command(parent: Data) -> Data:
    resolution = parent["natural_turn"]["resolution"]
    if resolution["state"] != "answer" or not resolution.get("question_id") or not resolution.get("answer"):
        raise ValueError("Only an exact retained human answer can be projected")
    if parent["natural_turn"].get("human_message", resolution["answer"]) != resolution["answer"]:
        raise ValueError("The retained human answer changed")
    return ChannelCommand(
        action="respond", run_id=resolution["run_id"], request_id=resolution["question_id"], answer=resolution["answer"]
    ).model_dump(exclude_none=True)


def validate_answer(parent: Data, current: Data) -> None:
    resolution = parent["natural_turn"]["resolution"]
    if (
        any(current.get(key) != resolution.get(key) for key in ("run_id", "question_id", "question_digest"))
        or current.get("question_status") != "pending"
        or current.get("consumed") is not False
        or current.get("run_status") != "paused"
    ):
        raise ValueError("The original pending question changed; ask for clarification")
    answer_command(parent)


def projected_delivery(
    parent: Data, command: Data, *, stage: Literal["answer", "resume"], admission: str, scope_digest: str | None = None
) -> Data:
    if parent["issue"]["kind"] != "issue":
        raise ValueError("Natural pull-request conversations currently confer read authority only")
    command = (
        (ChannelCommand if stage == "answer" else SCMResumeCommandV2)
        .model_validate(command)
        .model_dump(exclude_none=True)
    )
    resolution = parent["natural_turn"]["resolution"]
    if command.get("run_id") != resolution["run_id"] or stage == "answer" and command != answer_command(parent):
        raise ValueError("A natural command cannot expand its exact retained question scope")
    marker = {
        "version": 1,
        "receipt_id": parent["id"],
        "stage": stage,
        "admission_id": admission,
        "intake_digest": intake_digest(parent),
        "command_digest": digest(command),
        "scope_digest": scope_digest or digest(parent["natural_turn"]["resolution"].get("scope", {})),
    }
    child_id = digest(marker)[:32]
    return {
        **{
            key: copy.deepcopy(parent[key])
            for key in (
                "repository_id",
                "provider",
                "remote_id",
                "actor",
                "digest",
                "issue",
                "verification",
                "source_snapshot",
            )
        },
        "id": child_id,
        "delivery_id": "natural:" + child_id,
        "command": command,
        "status": "received",
        "action_status": "not_dispatched",
        "feedback_status": "not_requested",
        "run_id": None,
        "reason": None,
        "global_ingress": False,
        "natural_parent": marker,
        "created_at": parent.get("created_at"),
        "updated_at": parent.get("updated_at"),
    }


def validate_projection(child: Data, parent: Data) -> None:
    marker = child.get("natural_parent")
    if (
        not isinstance(marker, dict)
        or set(marker)
        != {
            "version",
            "receipt_id",
            "stage",
            "admission_id",
            "intake_digest",
            "command_digest",
            "scope_digest",
        }
        or type(marker.get("version")) is not int
        or marker["version"] != 1
    ):
        raise ValueError("The natural command projection receipt is malformed")
    if (
        parent.get("status") != "processed"
        or parent["natural_turn"].get("state") not in {"answer_pending_permission", "answer_saved_pending_resume"}
        or marker["receipt_id"] != parent["id"]
        or marker["intake_digest"] != intake_digest(parent)
        or (parent.get("natural_effects") or {}).get(child["id"]) != marker
        or marker["command_digest"] != digest(child["command"])
        or marker["stage"] not in {"answer", "resume"}
    ):
        raise ValueError("The exact natural command projection changed")
    expected = projected_delivery(
        parent,
        child["command"],
        stage=marker["stage"],
        admission=marker["admission_id"],
        scope_digest=marker["scope_digest"],
    )
    for key in (
        "id",
        "repository_id",
        "provider",
        "remote_id",
        "actor",
        "digest",
        "issue",
        "verification",
        "source_snapshot",
        "command",
    ):
        if expected[key] != child.get(key):
            raise ValueError("The natural command differs from its exact original human source")


async def authorize_projection(polling: Any, child: Data) -> None:
    """Repeat parent, read-scope and immutable-question checks inside native effect admission."""
    from firefly_forge.agent_roles import worker_feedback_model

    marker = child.get("natural_parent")
    if not isinstance(marker, dict) or marker.get("receipt_id") == child["id"]:
        raise AccessDenied("The original natural intake reference is malformed")
    parent_id = marker.get("receipt_id")
    if not isinstance(parent_id, str):
        raise AccessDenied("The original natural intake identity is unavailable")
    async with polling.store._unit():
        row = await polling.inbox.find_by_id(identifier(parent_id))
        if row is None or row.data.get("natural_parent") is not None:
            raise AccessDenied("A natural command must refer directly to its original human intake")
        parent = copy.deepcopy(row.data)
    parent = await polling._observation(
        parent["natural_turn"]["context"]["scope"]["project_id"], child["repository_id"], parent_id
    )
    validate_projection(child, parent)
    scope = await polling.conversation_scope(parent, remote=False)
    if digest({"scopes": [scope]}) != child["natural_parent"]["scope_digest"]:
        raise AccessDenied("The natural command's exact current owner or read binding changed")
    admission = (parent.get("natural_admissions") or {}).get(marker["admission_id"])
    if admission is not None:
        operator = admission["operator"]
        async with polling.store._unit():
            identity = await polling.users.find_by_id(identifier(operator["user_id"]), lock=LockMode.PESSIMISTIC_WRITE)
            expected_identity = admission["operator_identity"]
            if (
                identity is None
                or not identity.enabled
                or (identity.issuer, identity.subject) != (expected_identity["issuer"], expected_identity["subject"])
            ):
                raise AccessDenied("The exact natural answer reviewer is no longer current")
            await polling.access.authorize_actor(operator, admission["project_id"], "execute")
    resolution = parent["natural_turn"]["resolution"]
    run = await polling.store.get_run(resolution["run_id"])
    if (
        run.get("project_id") != scope["project_id"]
        or run["repository_id"] != parent["repository_id"]
        or run.get("component_id") not in scope["component_ids"]
    ):
        raise AccessDenied("The natural task no longer belongs to its current authorized project")
    journal = polling.collaboration.deployments.engine.journal
    async with polling.store._unit():
        entries = await journal.records.find_for_run(identifier(run["id"]))
    questions = [
        entry.data
        for entry in entries
        if entry.kind == "worker_feedback" and entry.digest == resolution["question_digest"]
    ]
    if len(questions) != 1:
        raise AccessDenied("The original natural question journal record changed")
    feedback = worker_feedback_model(questions[0]["payload"]).model_validate(questions[0]["payload"])
    request = run.get("worker_request") or {}
    if (
        feedback.request is None
        or request
        != {
            **feedback.request.model_dump(),
            "status": request.get("status"),
            "role_id": feedback.role_id,
            "task_id": feedback.task_id,
            "attempt": feedback.attempt,
        }
        or feedback.request.id != resolution["question_id"]
    ):
        raise AccessDenied("The exact natural question changed before the native effect")


class NaturalEffects:
    def __init__(self, natural: Any) -> None:
        self.natural, self.collaboration, self.store = natural, natural.collaboration, natural.store

    async def review(self, receipt_id: str, *, remote: bool = True) -> Data:
        from firefly_forge.scm_natural_tasks import natural_policy_digest

        parent = await self.collaboration.receipt(receipt_id)
        turn = parent.get("natural_turn") or {}
        if parent["status"] != "processed" or turn.get("state") == "needs_reconciliation":
            raise AccessDenied("Only a known retained natural answer or continuation can be admitted")
        if turn.get("policy_digest") != natural_policy_digest():
            raise AccessDenied("The retained natural answer's interpretation policy changed")
        resolution = turn.get("resolution") or {}
        if resolution.get("state") not in {"answer", "resume"}:
            raise AccessDenied("The natural message did not resolve one exact execution question")
        scopes = await self.natural.conversations.scopes(parent, remote=remote)
        context = await self.natural._context(parent, scopes)
        current = next((row for row in context["candidates"] if row["run_id"] == resolution["run_id"]), None)
        if current is None or any(
            current.get(key) != resolution.get(key) for key in ("question_id", "question_digest")
        ):
            raise AccessDenied("The original issue, owner, task or question changed")
        if resolution["state"] == "answer" and current["question_status"] == "pending":
            validate_answer(parent, current)
        elif (
            current["question_status"] != "answered"
            or current["consumed"] is not False
            or current["run_status"] != "paused"
        ):
            raise AccessDenied("The original answer is stale, consumed or outside a paused task")
        if resolution["state"] == "answer" and current["question_status"] == "answered":
            response_id = current.get("response_receipt_id")
            marker = (parent.get("natural_effects") or {}).get(response_id)
            if not marker or marker.get("stage") != "answer" or current.get("response_answer") != resolution["answer"]:
                raise AccessDenied("The original retained answer cannot claim another answer's custody")
            child = await self.collaboration.receipt(response_id)
            validate_projection(child, parent)
            if (
                child.get("status") != "processed"
                or child.get("action_status") != "completed"
                or child["command"] != answer_command(parent)
            ):
                raise AccessDenied("The original answer's exact completed native receipt is unavailable")
        polling = self.collaboration.polling
        binding = await polling._head(context["scope"]["project_id"], parent["repository_id"], scopes[0]["binding_key"])
        resume = self.collaboration.poll_resume_authority
        grant = (
            await resume._grant(context["scope"]["project_id"], parent["repository_id"], key=scopes[0]["binding_key"])
            if resume and current.get("answer_only") is not True
            else None
        )
        if current.get("answer_only") is True and resolution["state"] != "answer":
            raise AccessDenied("Native maintain communication authorizes answers without continuation")
        native_cut = None
        if grant and grant["request"]["active"] and current["question_status"] == "answered":
            binding = await resume._binding(
                context["scope"]["project_id"],
                parent["repository_id"],
                resume.request_type.model_validate(grant["request"]),
            )
            native_context = {
                "run": await resume._run_data(current["run_id"]),
                "grant": grant,
                "binding": binding,
                "user_id": grant["native_user_id"],
                "delivery": parent,
            }
            await resume._original(native_context["run"])
            if remote:
                await resume._preflight(native_context)
            native_cut = await resume._cut(native_context)
        cut = {
            "version": 1,
            "receipt_id": receipt_id,
            "intake_digest": intake_digest(parent),
            "scopes": scopes,
            "question": current,
            "binding_digest": binding["digest"],
            "respond_allowed": "respond" in binding["request"]["allowed_actions"],
            "resume_grant_digest": grant["digest"] if grant and grant["request"]["active"] else None,
            "effects": copy.deepcopy(parent.get("natural_effects", {})),
            "native_resume_cut": native_cut,
        }
        return {**cut, "digest": digest(cut)}

    async def _child(
        self, parent: Data, cut: Data, command: Data, stage: Literal["answer", "resume"], admission: str
    ) -> Data:
        child = projected_delivery(
            parent, command, stage=stage, admission=admission, scope_digest=digest({"scopes": cut["scopes"]})
        )
        from firefly_forge.scm_polling import MAX_BYTES, inbox_size

        async with self.natural.conversations._locked(cut["scopes"], parent["repository_id"]):
            row = await self.collaboration.inbox.find_by_id(identifier(parent["id"]), lock=LockMode.PESSIMISTIC_WRITE)
            if (
                row is None
                or intake_digest(row.data) != cut["intake_digest"]
                or (await self.review(parent["id"], remote=False))["digest"] != cut["digest"]
            ):
                raise AccessDenied("The sealed natural answer admission cut changed")
            existing = await self.collaboration.inbox.find_by_id(
                identifier(child["id"]), lock=LockMode.PESSIMISTIC_WRITE
            )
            if existing is not None:
                validate_projection(existing.data, row.data)
                return copy.deepcopy(existing.data)
            count, size = await inbox_size(self.store, parent["repository_id"])
            if count >= 5000 or size + len(json.dumps(child, ensure_ascii=False).encode()) > MAX_BYTES:
                raise AccessDenied("Natural command receipt retention is full")
            effects = {**row.data.get("natural_effects", {}), child["id"]: child["natural_parent"]}
            if len(effects) > 50:
                raise AccessDenied("Natural answer admission history is full")
            self.store.reject_credentials(child)
            await self.collaboration.inbox.save(
                ForgeWebhookInboxEntity(
                    id=identifier(child["id"]),
                    repository_id=identifier(parent["repository_id"]),
                    provider=parent["provider"],
                    delivery_id=child["delivery_id"],
                    source_key=child["delivery_id"],
                    status="received",
                    data=child,
                )
            )
            await self.collaboration._write_row(row, natural_effects=effects)
        return child

    async def apply(self, receipt_id: str, *, admission: str | None = None, explicit: bool = False) -> Data:
        parent: Data = await self.collaboration.receipt(receipt_id)
        expected = copy.deepcopy(parent)
        try:
            cut = await self.review(receipt_id)
        except (AccessDenied, ValueError, KeyError):
            return dict(parent)
        admission = admission or digest({"receipt_id": receipt_id, "intake_digest": cut["intake_digest"]})[:32]
        if not explicit and any(
            cut["scopes"][0][key] != parent["natural_turn"]["context"]["scope"][key]
            for key in ("project_id", "repository_id", "binding_digest")
        ):
            return dict(parent)
        if explicit and admission not in (parent.get("natural_admissions") or {}):
            raise AccessDenied("Explicit natural answer admission requires its retained operator receipt")
        if (
            not explicit
            and cut["effects"]
            and admission not in {value["admission_id"] for value in cut["effects"].values()}
        ):
            # Only explicit readmission may create a new identity after a known refusal.
            return dict(parent)
        try:
            if cut["question"]["question_status"] == "pending":
                if not cut["respond_allowed"]:
                    return dict(parent)
                child = await self._child(parent, cut, answer_command(parent), "answer", admission)
                expected = {
                    **parent,
                    "natural_effects": {**parent.get("natural_effects", {}), child["id"]: child["natural_parent"]},
                }
                current = await self.collaboration.receipt(receipt_id)
                if effect_state(current) != effect_state(expected):
                    return dict(current)
                if child["status"] == "received":
                    await self.collaboration._process_delegated(child["id"])
                completed = await self.collaboration.receipt(child["id"])
                if completed.get("status") != "processed" or completed.get("action_status") != "completed":
                    known_wait = False
                    if completed.get("status") == "received":
                        with suppress(ValueError):
                            known_wait = self.collaboration._delegated_wait(completed) is not None
                    return await self._retain_state(
                        receipt_id,
                        "answer_pending_permission"
                        if completed["status"] == "denied" or known_wait
                        else "needs_reconciliation",
                        expected=expected,
                    )
                parent = await self.collaboration.receipt(receipt_id)
                if effect_state(parent) != effect_state(expected):
                    return dict(parent)
                cut = await self.review(receipt_id)
            if cut["resume_grant_digest"] is None:
                return await self._retain_state(receipt_id, "answer_saved_pending_resume", expected=expected)
            resume = self.collaboration.poll_resume_authority
            if cut["native_resume_cut"] is None:
                return await self._retain_state(receipt_id, "answer_saved_pending_resume", expected=expected)
            command = SCMResumeCommandV2(
                version=2,
                action="resume",
                run_id=cut["question"]["run_id"],
                expected_grant_digest=cut["resume_grant_digest"],
                expected_question_id=cut["question"]["question_id"],
                expected_response_digest=cut["question"]["response_digest"],
                expected_cut_digest=cut["native_resume_cut"]["digest"],
            ).model_dump(mode="json")
            child = await self._child(parent, cut, command, "resume", admission)
            expected = {
                **parent,
                "natural_effects": {**parent.get("natural_effects", {}), child["id"]: child["natural_parent"]},
            }
            current = await self.collaboration.receipt(receipt_id)
            if effect_state(current) != effect_state(expected):
                return dict(current)
            if child["status"] != "received":
                return await self._retain_state(receipt_id, "needs_reconciliation", expected=expected)
            await resume.process(child["id"])
            completed = await self.collaboration.receipt(child["id"])
            return await self._retain_state(
                receipt_id,
                "resumed" if completed.get("action_status") == "completed" else "needs_reconciliation",
                expected=expected,
            )
        except (AccessDenied, ValueError, KeyError):
            return await self._retain_state(
                receipt_id,
                "answer_pending_permission"
                if cut["question"]["question_status"] == "pending"
                else "answer_saved_pending_resume",
                expected=expected,
            )

    async def _retain_state(self, receipt_id: str, state: str, *, expected: Data) -> Data:
        async with self.store._unit():
            row = await self.collaboration.inbox.find_by_id(identifier(receipt_id), lock=LockMode.PESSIMISTIC_WRITE)
            if row is not None and effect_state(row.data) == effect_state(expected):
                await self.collaboration._write_row(row, natural_turn={**row.data["natural_turn"], "state": state})
        return dict(await self.collaboration.receipt(receipt_id))

    async def readmit(self, receipt_id: str, request: NaturalReadmission) -> Data:
        actor = CURRENT_PRINCIPAL.get()
        parent: Data = await self.collaboration.receipt(receipt_id)
        original = (parent.get("natural_admissions") or {}).get(request.request_id)
        if original is not None:
            if (
                actor is None
                or original["request"] != request.model_dump(mode="json")
                or original["operator"] != ProjectAccess.actor_snapshot(actor)
                or original["operator_identity"] != {"issuer": actor.issuer, "subject": actor.subject}
            ):
                raise AccessDenied("The original natural admission belongs to another input or operator")
            await ProjectAccess(self.store).authorize_actor(original["operator"], original["project_id"], "execute")
            return dict(parent)
        cut = await self.review(receipt_id)
        if cut["intake_digest"] != request.expected_intake_digest or cut["digest"] != request.expected_cut_digest:
            raise AccessDenied("Review the same retained answer and exact current question before readmission")
        if actor is None:
            raise AccessDenied("An authenticated current project executor must review this answer admission")
        await ProjectAccess(self.store).authorize_actor(
            ProjectAccess.actor_snapshot(actor), cut["question"]["project_id"], "execute"
        )
        await self.store.admit_credentials(request.model_dump(mode="json"))
        if await self.review(receipt_id) != cut:
            raise AccessDenied("The current question or consent changed during answer readmission")
        async with self.natural.conversations._locked(cut["scopes"], parent["repository_id"]):
            identity = await self.collaboration.polling.users.find_by_id(actor.id, lock=LockMode.PESSIMISTIC_WRITE)
            if (
                identity is None
                or not identity.enabled
                or (identity.issuer, identity.subject) != (actor.issuer, actor.subject)
                or CURRENT_PRINCIPAL.get() != actor
            ):
                raise AccessDenied("The current reviewing operator changed before answer admission")
            await ProjectAccess(self.store).authorize_actor(
                ProjectAccess.actor_snapshot(actor), cut["question"]["project_id"], "execute"
            )
            current = await self.review(receipt_id, remote=False)
            if current != cut:
                raise AccessDenied("The exact reviewed natural answer cut changed before admission")
            row = await self.collaboration.inbox.find_by_id(identifier(receipt_id), lock=LockMode.PESSIMISTIC_WRITE)
            assert row is not None
            for child_id in row.data.get("natural_effects", {}):
                child = await self.collaboration.inbox.find_by_id(identifier(child_id), lock=LockMode.PESSIMISTIC_WRITE)
                if child is None or not (
                    child.status == "denied"
                    and child.data.get("action_status") == "not_dispatched"
                    or child.status == "processed"
                    and child.data.get("action_status") == "completed"
                ):
                    raise AccessDenied("An original natural effect is unknown; reconcile it without replay")
            admissions = copy.deepcopy(row.data.get("natural_admissions", {}))
            if request.request_id in admissions:
                existing = admissions[request.request_id]
                if existing["request"] != request.model_dump(mode="json") or existing[
                    "operator"
                ] != ProjectAccess.actor_snapshot(actor):
                    raise AccessDenied("The original natural admission identity changed")
                return copy.deepcopy(row.data)
            if len(admissions) >= 50:
                raise AccessDenied("Natural answer admission retention is full")
            admissions[request.request_id] = {
                "version": 1,
                "request": request.model_dump(mode="json"),
                "operator": ProjectAccess.actor_snapshot(actor),
                "operator_identity": {"issuer": actor.issuer, "subject": actor.subject},
                "project_id": cut["question"]["project_id"],
                "cut_digest": cut["digest"],
                "intake_digest": cut["intake_digest"],
            }
            await self.collaboration._write_row(row, natural_admissions=admissions)
        return await self.apply(receipt_id, admission=request.request_id, explicit=True)
