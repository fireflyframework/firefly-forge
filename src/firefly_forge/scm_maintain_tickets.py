# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Native communication bindings for independently admitted checkpoint maintenance."""

from __future__ import annotations

import copy
import json
from time import time
from typing import Any
from uuid import uuid4

from pyfly.data.transaction import current_unit_of_work

from firefly_forge.checkpoint_test_models import frozen_checkpoint_source
from firefly_forge.collaboration_persistence import ForgeWebhookInboxEntity
from firefly_forge.journal import digest
from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied, ProjectAccess
from firefly_forge.run_creation_requests import RunCreationRequests
from firefly_forge.scm_conversations import SCMConversations, observation_cut
from firefly_forge.scm_maintain_ticket_models import (
    MaintainTicketInput,
    MaintainTicketMarker,
    MaintainTicketReceipt,
    request_digest,
)
from firefly_forge.scm_poll_models import PollPointer
from firefly_forge.scm_polling import MAX_BYTES, inbox_size
from firefly_forge.store import identifier, now

Data = dict[str, Any]
STATE_KEY = "_maintain_ticket_v1"
KIND = "native_maintain_ticket_v1"


def clock_ms() -> int:
    return int(time() * 1000)


class MaintainNotificationNotDispatched(AccessDenied):
    """Current local authority refused this notification before HTTP dispatch."""


def state_of(data: Data) -> Data:
    value = copy.deepcopy(data.get(STATE_KEY, {"version": 1, "requests": {}, "heads": {}}))
    if (
        set(value) != {"version", "requests", "heads"}
        or type(value["version"]) is not int
        or value["version"] != 1
        or type(value["requests"]) is not dict
        or type(value["heads"]) is not dict
        or len(value["requests"]) > 100
        or len(value["heads"]) > 100
        or len(json.dumps(value).encode()) > MAX_BYTES
    ):
        raise ValueError("The native maintain communication state changed")
    for pointer in [*value["requests"].values(), *value["heads"].values()]:
        PollPointer.model_validate(pointer)
    return dict(value)


def creation_scope(run: Data, receipt: Data) -> None:
    if (
        receipt.get("run_id") != run["id"]
        or receipt.get("source_checkpoint") != run.get("source_checkpoint_snapshot")
        or run.get("operation") != "maintain"
        or run.get("publish") is not False
        or receipt.get("input", {}).get("operation") != "maintain"
        or receipt.get("input", {}).get("publish", False) is not False
        or any(receipt.get(k) != run.get(k) for k in ("repository_id", "project_id", "component_id"))
    ):
        raise AccessDenied("The ticket requires one exact native maintain creation")
    frozen_checkpoint_source(run, {"id": run["repository_id"]})


class MaintainTickets:
    def __init__(self, collaboration: Any) -> None:
        self.c, self.store, self.polling = collaboration, collaboration.store, collaboration.polling

    async def _creation(self, request_id: str) -> Data:
        async with self.store._unit():
            receipt = await RunCreationRequests(self.store)._load(request_id)
        if receipt is None:
            raise AccessDenied("The exact native creation receipt is unavailable")
        return receipt

    async def _load(self, pointer: Data, project: str, repository: str) -> Data:
        value = await self.polling._event(pointer, KIND, project, repository)
        return MaintainTicketReceipt.model_validate(value).model_dump(mode="json")

    async def _target(self, source: Data, project: str, repository: str, *, remote: bool) -> tuple[Data, Data, Data]:
        scope = await self.polling.conversation_scope(source, remote=remote)
        if scope["project_id"] != project or scope["repository_id"] != repository:
            raise AccessDenied("The current observed owner belongs to another project or repository")
        head = await self.polling._head(project, repository, scope["binding_key"])
        if not {"status", "respond"}.issubset(head["request"]["allowed_actions"]):
            raise AccessDenied("Current status and owner-answer consent are required")
        async with self.store._unit():
            user = await self.polling.users.find_by_id(identifier(scope["native_user_id"]))
            if user is None or not user.enabled:
                raise AccessDenied("The mapped maintain owner is unavailable")
            identity = digest({"id": user.id.hex, "issuer": user.issuer, "subject": user.subject})
        await ProjectAccess(self.store).authorize_actor(
            {"user_id": scope["native_user_id"], "authentication_method": "oidc_bearer"}, project, "execute"
        )
        return scope, head, {"user_id": scope["native_user_id"], "identity_digest": identity}

    async def configure(self, project: str, repository: str, request: MaintainTicketInput) -> Data:
        if current_unit_of_work() is not None:
            raise ValueError("Configure maintain communication outside a caller transaction")
        actor = CURRENT_PRINCIPAL.get()
        await self.polling.authority._reader(actor, project, repository, admin=True)
        assert actor is not None
        incoming = request.model_dump(mode="json")
        await self.store.admit_credentials(incoming)
        async with self.store._unit():
            row, _ = await self.polling._state(repository)
            state = state_of(row.data)
        request_key = project + ":" + request.request_id
        if pointer := state["requests"].get(request_key):
            kept = await self._load(pointer, project, repository)
            if kept["request"] != incoming or kept["admin_snapshot"]["user_id"] != actor.id.hex:
                raise AccessDenied("The native communication request identity belongs to another input or actor")
            return kept
        run = await self.store.get_run(request.run_id)
        receipt = await self._creation(request.creation_request_id)
        creation_scope(run, receipt)
        if receipt["digest"] != request.expected_creation_digest or (run["project_id"], run["repository_id"]) != (
            project,
            repository,
        ):
            raise AccessDenied("The reviewed maintain creation or scope changed")
        source = await self.polling._observation(project, repository, request.observed_receipt_id)
        if source.get("source_conflict") or digest(source["verification"]) != request.expected_reference_digest:
            raise AccessDenied("The real GitHub owner observation changed")
        scope, head, owner = await self._target(source, project, repository, remote=True)
        references, _ = await SCMConversations(self.c).references(source, [scope], durable=True)
        parent_id = run["source_checkpoint_snapshot"]["reference"]["run_id"]
        parents = [value for value in references if value["run_id"] == parent_id]
        if len(parents) != 1 or run["component_id"] not in scope["component_ids"]:
            raise AccessDenied("The observed ticket has no exact authenticated checkpoint parent")
        parent = await SCMConversations(self.c).resolve_reference(parents[0], source, [scope])
        parent_cut = observation_cut(parent)
        await self.store.admit_credentials({"request": incoming, "source": observation_cut(source)})
        async with self.polling._locked(project, repository, [actor.id.hex, owner["user_id"]]):
            await self.polling.authority._reader(actor, project, repository, admin=True)
            current_scope, current_head, current_owner = await self._target(source, project, repository, remote=False)
            if (current_scope, current_head, current_owner) != (scope, head, owner):
                raise AccessDenied("The maintain communication owner or current consent changed")
            current_receipt = await self._creation(request.creation_request_id)
            current_run = await self.store.get_run(request.run_id)
            creation_scope(current_run, current_receipt)
            current_parent = await SCMConversations(self.c).resolve_reference(parents[0], source, [current_scope])
            if current_receipt != receipt or observation_cut(current_parent) != parent_cut:
                raise AccessDenied("The retained maintain creation or ticket parent changed")
            row, _ = await self.polling._state(repository)
            state = state_of(row.data)
            if pointer := state["requests"].get(request_key):
                kept = await self._load(pointer, project, repository)
                if kept["request"] != incoming or kept["admin_snapshot"]["user_id"] != actor.id.hex:
                    raise AccessDenied("The concurrent maintain request identity changed")
                return kept
            old_pointer = state["heads"].get(request.run_id)
            old = await self._load(old_pointer, project, repository) if old_pointer else None
            if (old["digest"] if old else None) != request.expected_previous_digest:
                raise ValueError("Maintain communication conflict; retain this native request identity")
            if old is not None:
                prior = await self.c.inbox.find_by_id(identifier(old["outbox_id"]))
                if prior is not None and prior.data.get("feedback_status") in {"writing", "needs_reconciliation"}:
                    raise AccessDenied("The prior notification outcome requires reconciliation")
            if len(state["requests"]) >= 100:
                raise ValueError("Native maintain communication retention is full")
            prepared = clock_ms()
            outbox_id = digest({"kind": KIND, "project": project, "request": incoming})[:32]
            value = dict(
                version=1,
                event_id=uuid4().hex,
                project_id=project,
                repository_id=repository,
                run_id=request.run_id,
                binding_key=request.run_id,
                revision=old["revision"] + 1 if old else 1,
                previous_digest=request.expected_previous_digest,
                request=incoming,
                request_digest=request_digest(request),
                admin_snapshot=ProjectAccess.actor_snapshot(actor),
                creation_digest=receipt["digest"],
                parent_reference=parents[0],
                parent_observation=parent_cut,
                observation=observation_cut(source),
                polling_scope=scope,
                owner=owner,
                outbox_id=outbox_id,
                prepared_at_ms=prepared,
                expires_at_ms=prepared + request.lifetime_seconds * 1000,
            )
            from firefly_forge.scm_natural_tasks import natural_policy_digest

            value["policy_digest"] = natural_policy_digest()
            value["digest"] = digest(value)
            value = MaintainTicketReceipt.model_validate(value).model_dump(mode="json")
            pointer = await self.polling._save_event(KIND, value)
            state["requests"][request_key] = pointer
            state["heads"][request.run_id] = pointer
            state_of({STATE_KEY: state})
            marker = {
                "version": 1,
                "kind": KIND,
                "project_id": project,
                "request_id": request.request_id,
                "run_id": request.run_id,
                "binding_digest": value["digest"],
            }
            data = {
                key: copy.deepcopy(source[key])
                for key in (
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
                id=outbox_id,
                delivery_id="maintain-ticket:" + outbox_id,
                digest=digest(marker),
                status="processed",
                action_status="completed",
                reason=None,
                run_id=request.run_id,
                feedback_status="pending",
                maintain_ticket=marker,
                global_ingress=False,
                command={"action": "status", "run_id": request.run_id, "language": "en"},
                created_at=now(),
                updated_at=now(),
            )
            count, retained = await inbox_size(self.store, repository)
            if count >= 5000 or retained + len(json.dumps(data).encode()) > MAX_BYTES:
                raise ValueError("Maintain notification retention is full")
            self.store.reject_credentials(data)
            await self.c.inbox.save(
                ForgeWebhookInboxEntity(
                    id=identifier(outbox_id),
                    repository_id=identifier(repository),
                    provider="github",
                    delivery_id=data["delivery_id"],
                    source_key=data["delivery_id"],
                    status="processed",
                    data=data,
                )
            )
            row.data = {**row.data, STATE_KEY: state}
            await self.polling.settings.save(row)
            self.confirm_local_cut({"binding": value})
        return copy.deepcopy(value)

    async def read(self, project: str, repository: str, request_id: str) -> Data:
        await self.polling.authority._reader(CURRENT_PRINCIPAL.get(), project, repository, admin=False)
        async with self.store._unit():
            row, _ = await self.polling._state(repository)
            pointer = state_of(row.data)["requests"].get(project + ":" + request_id)
        if pointer is None:
            raise KeyError("The native maintain ticket request is unavailable")
        return await self._load(pointer, project, repository)

    async def current(self, data: Data, *, remote: bool = False, dispatch: bool = False) -> Data:
        from firefly_forge.scm_natural_tasks import natural_policy_digest

        marker = MaintainTicketMarker.model_validate(data.get("maintain_ticket")).model_dump(mode="json")
        project, repository = marker["project_id"], data["repository_id"]
        async with self.store._unit():
            row, _ = await self.polling._state(repository)
            state = state_of(row.data)
            pointer = state["heads"].get(marker["run_id"])
        if pointer is None or pointer.get("digest") != marker["binding_digest"]:
            raise AccessDenied("The maintain ticket binding is no longer current")
        value = await self._load(pointer, project, repository)
        request = MaintainTicketInput.model_validate(value["request"])
        async with self.store._unit():
            administrator = await self.polling.users.find_by_id(identifier(value["admin_snapshot"]["user_id"]))
            if administrator is None or not administrator.enabled:
                raise AccessDenied("The maintain communication administrator is unavailable")
        if not await ProjectAccess(self.store).authorize_actor(value["admin_snapshot"], project, "members"):
            raise AccessDenied("Current factory administrator authority is required for maintain communication")
        if (
            value["request"]["request_id"] != marker["request_id"]
            or await self.polling._latest(project, repository, KIND, request.run_id) != value["event_id"]
            or value["policy_digest"] != natural_policy_digest()
            or data["id"] != value["outbox_id"]
            or data["digest"] != digest(marker)
            or data["run_id"] != request.run_id
        ):
            raise AccessDenied("The native maintain notification lost its exact current receipt")
        source = await self.polling._observation(project, repository, request.observed_receipt_id)
        if observation_cut(source) != value["observation"] or source.get("source_conflict"):
            raise AccessDenied("The native maintain observation changed")
        for key in ("repository_id", "provider", "remote_id", "actor", "issue", "verification", "source_snapshot"):
            if data.get(key) != source.get(key):
                raise AccessDenied("The maintain notification destination changed")
        if data.get("command") != {"action": "status", "run_id": request.run_id, "language": "en"}:
            raise AccessDenied("A maintain notification cannot become an inbound command")
        creation = await self._creation(request.creation_request_id)
        run = await self.store.get_run(request.run_id)
        creation_scope(run, creation)
        if worker_request := run.get("worker_request"):
            from firefly_forge.agent_roles import worker_feedback_model
            from firefly_forge.native_question_publications import NativeQuestionPublications

            entries = await NativeQuestionPublications(self.c)._entries(run["id"])
            questions = [
                entry
                for entry in entries
                if entry["kind"] == "worker_feedback"
                and (entry["payload"].get("request") or {}).get("id") == worker_request.get("id")
            ]
            if len(questions) != 1:
                raise AccessDenied("The maintain question requires one exact immutable worker feedback")
            payload = questions[0]["payload"]
            feedback = worker_feedback_model(payload).model_validate(payload)
            if feedback.request is None or worker_request != {
                **feedback.request.model_dump(),
                "status": worker_request.get("status"),
                "role_id": feedback.role_id,
                "task_id": feedback.task_id,
                "attempt": feedback.attempt,
            }:
                raise AccessDenied("The maintain question changed from its native feedback")
        if creation["digest"] != value["creation_digest"]:
            raise AccessDenied("The maintain child creation changed")
        scope, _, owner = await self._target(source, project, repository, remote=remote)
        parent = await SCMConversations(self.c).resolve_reference(value["parent_reference"], source, [scope])
        if observation_cut(parent) != value["parent_observation"]:
            raise AccessDenied("The exact native parent ticket provenance changed")
        if (
            scope != value["polling_scope"]
            or owner != value["owner"]
            or run["component_id"] not in scope["component_ids"]
        ):
            raise AccessDenied("The current maintain owner or native scope changed")
        if dispatch:
            async with self.store._unit():
                row = await self.c.inbox.find_by_id(identifier(data["id"]))
            if row is None or row.data.get("feedback_status") not in {"pending", "writing", "sent"}:
                raise AccessDenied("An unknown maintain notification cannot be retried")
            if len(row.data.get("feedback_history") or []) >= request.max_notifications:
                raise AccessDenied("The explicit maintain notification allowance is exhausted")
        context = {"binding": value, "run": run, "source": source, "scope": scope}
        self.confirm_local_cut(context)
        return context

    @staticmethod
    def confirm_local_cut(context: Data) -> None:
        from firefly_forge.scm_natural_tasks import natural_policy_digest

        value = context["binding"]
        if value["policy_digest"] != natural_policy_digest() or clock_ms() >= value["expires_at_ms"]:
            raise MaintainNotificationNotDispatched("The maintain communication policy or finite consent expired")

    async def authorize_feedback(self, data: Data, *, remote: bool = False) -> Data:
        try:
            return await self.current(data, remote=remote, dispatch=True)
        except (AccessDenied, KeyError, ValueError) as error:
            raise MaintainNotificationNotDispatched(
                "Current native maintain communication was refused before HTTP"
            ) from error

    async def authorize_reference(self, data: Data, run: Data, delivery: Data, scopes: list[Data]) -> None:
        context = await self.current(data)
        if (
            context["run"]["id"] != run["id"]
            or delivery["actor"] != data["actor"]
            or not any(scope["native_user_id"] == context["binding"]["owner"]["user_id"] for scope in scopes)
        ):
            raise AccessDenied("Another project reader retains only checkpoint status access")
