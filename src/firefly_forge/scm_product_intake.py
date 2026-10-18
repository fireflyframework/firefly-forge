# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Separately consented first-product registration from a verified GitHub delivery."""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import uuid4

from pyfly.data.relational.sqlalchemy import LockMode
from pyfly.data.transaction import current_unit_of_work
from sqlalchemy import select, text

from firefly_forge.access_persistence import ForgeAccessEventEntity, ForgeAccessEventRepository, ForgeMembershipEntity
from firefly_forge.collaboration_persistence import (
    ForgeCollaborationSettingsRepository,
    ForgeProductDefinitionEntity,
    ForgeProductDefinitionRepository,
    ForgeWebhookInboxRepository,
)
from firefly_forge.identity import Principal
from firefly_forge.identity_persistence import ForgeIdentityUserRepository
from firefly_forge.journal import digest
from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied, ProjectAccess
from firefly_forge.project_models import ProjectComponent, ProjectDefinition
from firefly_forge.project_persistence import ForgeProjectEntity, ForgeProjectRepository, ForgeProjectRevisionEntity
from firefly_forge.scm_actor_authority import SCMActorAuthority
from firefly_forge.scm_authority_models import VerifiedSCMReferenceV1
from firefly_forge.scm_connections import SCMConnections
from firefly_forge.scm_product_intake_models import SCMProductIntakeCommandV1, SCMProductIntakeConsentInputV1
from firefly_forge.store import Store, identifier, now

Data = dict[str, Any]
STATE_KEY = "_scm_product_intake_v1"
EVENT_KIND = "scm_product_intake_receipt"


def intake_state(data: Data) -> Data:
    state = copy.deepcopy(data.get(STATE_KEY, {"version": 1, "head": None, "requests": {}, "operation": None}))
    if (
        not isinstance(state, dict)
        or set(state) != {"version", "head", "requests", "operation"}
        or type(state["version"]) is not int
        or state["version"] != 1
        or not isinstance(state["requests"], dict)
        or len(state["requests"]) > 32
        or len(json.dumps(state).encode()) > 1048576
    ):
        raise AccessDenied("The intake consent index is invalid or exceeds its retention bound")
    return state


class SCMProductIntake:
    def __init__(
        self,
        store: Store,
        scm: SCMConnections,
        settings: ForgeCollaborationSettingsRepository,
        inbox: ForgeWebhookInboxRepository,
        events: ForgeAccessEventRepository,
    ) -> None:
        self.store, self.scm, self.settings, self.inbox, self.events = store, scm, settings, inbox, events
        self.users = ForgeIdentityUserRepository()
        self.projects = ForgeProjectRepository()
        self.products = ForgeProductDefinitionRepository()
        self.access = ProjectAccess(store)
        self.authority = SCMActorAuthority(store, scm)

    @staticmethod
    def _outside(caller: Principal | None) -> None:
        if current_unit_of_work() is not None or CURRENT_PRINCIPAL.get() != caller:
            raise AccessDenied("Prepare intake outside a native unit with the original caller context")

    async def _native(self, actor: Data, identity: Data) -> None:
        if actor.get("authentication_method") not in {"local_bootstrap", "oidc_bearer", "oidc_session"}:
            raise AccessDenied("Intake requires separately opted-in native administrator authority")
        async with self.store._unit():
            user = await self.users.find_by_id(identifier(actor["user_id"]))
            if user is None or not user.enabled or {"issuer": user.issuer, "subject": user.subject} != identity:
                raise AccessDenied("The intake native identity is unavailable or changed")
            if not await self.access.authorize_actor(actor, None, "members"):
                raise AccessDenied("Intake requires current factory administrator authority")

    async def _admin(self, caller: Principal | None) -> tuple[Data, Data]:
        if caller is None or CURRENT_PRINCIPAL.get() != caller or caller.authentication_method == "scm_mapping":
            raise AccessDenied("A current native administrator must explicitly opt in")
        actor = self.access.actor_snapshot(caller)
        identity = {"issuer": caller.issuer, "subject": caller.subject}
        await self._native(actor, identity)
        if CURRENT_PRINCIPAL.get() != caller:
            raise AccessDenied("The intake administrator context changed")
        return actor, identity

    async def _delivery(self, receipt_id: str, repository: str) -> Data:
        async with self.store._unit():
            row = await self.inbox.find_by_id(identifier(receipt_id))
            if (
                row is None
                or row.id.hex != receipt_id
                or row.repository_id.hex != repository
                or row.data.get("id") != receipt_id
                or row.data.get("repository_id") != repository
                or row.provider != row.data.get("provider")
                or row.status != row.data.get("status")
                or len(json.dumps(row.data).encode()) > 262144
            ):
                raise AccessDenied("The original signed intake delivery is unavailable or inconsistent")
            return copy.deepcopy(row.data)

    @staticmethod
    def _reference(delivery: Data) -> Data:
        reference = VerifiedSCMReferenceV1.model_validate(delivery.get("verification")).model_dump(mode="json")
        if (
            reference["provider"] != "github"
            or delivery.get("provider") != "github"
            or reference["payload_digest"] != delivery.get("digest")
            or reference["external_actor_id"] != delivery.get("actor")
            or reference["remote_repository_id"] != delivery.get("remote_id")
        ):
            raise AccessDenied("Intake requires the original verified GitHub account and repository")
        return reference

    def _source_cut(self, repository: str, delivery: Data, repo: Data, reference: Data) -> None:
        """Native row locks do not protect these files; read them after the last native await."""
        scope = self.scm.delegation_scope(repo)
        if scope != {key: reference[key] for key in scope} or not self.authority._key_current(repository, reference):
            raise AccessDenied("The intake application connection or signing key changed")
        if delivery.get("global_ingress"):
            settings = self.store.configuration._read(
                self.store.configuration.directory / "channel-bootstrap.json"
            ).get("github", {})
            if not settings.get("enabled") or any(
                delivery["actor"] not in settings.get(key, []) for key in ("trusted_actors", "configuration_actors")
            ):
                raise AccessDenied("The global intake configuration actor changed")

    async def _local(self, repository: str, delivery: Data) -> tuple[Data, Data]:
        reference = self._reference(delivery)
        repo = await self.store.repository(repository)
        async with self.store._unit():
            row = await self.settings.find_for_repository(identifier(repository))
            settings = row.data if row else {}
            if not settings.get("enabled") or any(
                delivery["actor"] not in settings.get(key, []) for key in ("trusted_actors", "configuration_actors")
            ):
                raise AccessDenied("The intake actor is not a current repository configuration actor")
        self._source_cut(repository, delivery, repo, reference)
        return repo, reference

    async def _preflight(self, repository: str, delivery: Data) -> None:
        if current_unit_of_work() is not None:
            raise AccessDenied("Read the application grant outside native locks")
        repo, reference = await self._local(repository, delivery)
        grants = await self.scm.repositories("github")
        if not any(
            str(item["id"]) == reference["remote_repository_id"]
            and item["url"] == repo["url"]
            and item["name"] == repo["name"]
            for item in grants
        ):
            raise AccessDenied("The GitHub App no longer grants the intake repository")
        if await self._local(repository, delivery) != (repo, reference):
            raise AccessDenied("The intake source changed during application readback")

    @asynccontextmanager
    async def _locked(self, repository: str, user_id: str) -> AsyncIterator[Any]:
        async with self.store._unit() as session:
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"),
                {"key": self.store.schema + ":product-intake:" + repository},
            )
            await self.users.find_by_id(identifier(user_id), lock=LockMode.PESSIMISTIC_WRITE)
            row = await self.store.repository_records.find_by_id(
                identifier(repository), lock=LockMode.PESSIMISTIC_WRITE
            )
            if row is None:
                raise AccessDenied("The intake repository is unavailable")
            settings = await self.settings.find_for_repository(identifier(repository))
            if settings is None:
                raise AccessDenied("The intake repository has no explicit collaboration configuration")
            yield session, settings

    @staticmethod
    def _repository_snapshot(repository: Data) -> Data:
        return {
            key: repository.get(key)
            for key in (
                "id",
                "name",
                "url",
                "default_branch",
                "provider",
                "connection_provider",
                "remote_repository_id",
            )
        }

    async def configure(self, repository: str, request: SCMProductIntakeConsentInputV1) -> Data:
        repository = identifier(repository).hex
        request = SCMProductIntakeConsentInputV1.model_validate(request.model_dump(mode="json"))
        caller = CURRENT_PRINCIPAL.get()
        self._outside(caller)
        actor, identity = await self._admin(caller)
        delivery = await self._delivery(request.verified_receipt_id, repository)
        reference = self._reference(delivery)
        repository_snapshot = self._repository_snapshot(await self.store.repository(repository))
        if digest(reference) != request.expected_verified_reference_digest:
            raise AccessDenied("Review the exact original verified account reference")
        if request.active:
            await self._preflight(repository, delivery)
        await self.store.admit_credentials({"request": request.model_dump(mode="json"), "reference": reference})
        self._outside(caller)
        async with self._locked(repository, actor["user_id"]) as (_, row):
            await self._admin(caller)
            current_delivery = await self._delivery(request.verified_receipt_id, repository)
            if self._reference(current_delivery) != reference:
                raise AccessDenied("The original consent verification reference changed")
            if request.active:
                current_repository, _ = await self._local(repository, current_delivery)
                if self._repository_snapshot(current_repository) != repository_snapshot:
                    raise AccessDenied("The reviewed intake repository changed")
            state = intake_state(row.data)
            if not request.active and (
                state["head"] is None
                or {k: v for k, v in reference.items() if k != "payload_digest"}
                != {k: v for k, v in state["head"]["verified_reference"].items() if k != "payload_digest"}
            ):
                raise AccessDenied("Revocation must name the original consent source")
            previous = state["requests"].get(request.request_id)
            if previous is not None:
                if previous["request"] != request.model_dump(mode="json") or previous["native_actor"] != actor:
                    raise AccessDenied("The intake consent request already belongs to another actor or body")
                self.store.reject_credentials(previous)
                return copy.deepcopy(previous)
            limit = 31 if request.active else 32
            if (state["head"] or {}).get("digest") != request.expected_previous_digest or len(
                state["requests"]
            ) >= limit:
                raise AccessDenied("The intake consent head changed or its history bound was reached")
            receipt = {
                "version": 1,
                "repository_id": repository,
                "request": request.model_dump(mode="json"),
                "native_actor": actor,
                "native_identity": identity,
                "repository_snapshot": repository_snapshot if request.active else state["head"]["repository_snapshot"],
                "verified_reference": reference,
                "created_at": now(),
            }
            receipt["digest"] = digest(receipt)
            state["requests"][request.request_id] = state["head"] = receipt
            row.data = {**row.data, STATE_KEY: state}
            await self.settings.save(row)
            await self._admin(caller)
            if request.active:
                current_repository, _ = await self._local(
                    repository, await self._delivery(request.verified_receipt_id, repository)
                )
                if self._repository_snapshot(current_repository) != repository_snapshot:
                    raise AccessDenied("The intake repository changed before consent commit")
            self.store.reject_credentials(receipt)
            result = copy.deepcopy(receipt)
        self._outside(caller)
        return result

    async def consent(self, repository: str, request_id: str) -> Data:
        caller = CURRENT_PRINCIPAL.get()
        self._outside(caller)
        await self._admin(caller)
        async with self.store._unit():
            row = await self.settings.find_for_repository(identifier(repository))
            found = intake_state(row.data if row else {})["requests"].get(identifier(request_id).hex)
            if found is None:
                raise KeyError("Intake consent not found; absence does not authorize another write")
            result: Data = copy.deepcopy(found)
        await self._admin(caller)
        self._outside(caller)
        self.store.reject_credentials(result)
        return result

    async def _context(self, repository: str, delivery: Data) -> Data:
        command = SCMProductIntakeCommandV1.model_validate(delivery["command"])
        repo, reference = await self._local(repository, delivery)
        async with self.store._unit():
            row = await self.settings.find_for_repository(identifier(repository))
            consent = intake_state(row.data if row else {})["head"]
        if (
            consent is None
            or consent.get("repository_snapshot") != self._repository_snapshot(repo)
            or consent["request"]["request_id"] != command.expected_consent_request_id
            or consent["digest"] != command.expected_consent_digest
            or not consent["request"]["active"]
            or digest({key: value for key, value in consent.items() if key != "digest"}) != consent["digest"]
            or {k: v for k, v in reference.items() if k != "payload_digest"}
            != {k: v for k, v in consent["verified_reference"].items() if k != "payload_digest"}
        ):
            raise AccessDenied("A current separately consented native intake capability is required")
        await self._native(consent["native_actor"], consent["native_identity"])
        self._source_cut(repository, delivery, repo, reference)
        return {"repository": repo, "consent": consent, "reference": reference}

    async def _operation(self, state: Data, command: SCMProductIntakeCommandV1 | None, repository: str) -> Data | None:
        pointer = state["operation"]
        if pointer is None:
            return None
        row = await self.events.find_by_id(identifier(pointer["event_id"]))
        if row is None or not isinstance(row.data.get("receipt"), dict):
            raise AccessDenied("The original immutable intake receipt is unavailable")
        receipt: Data = copy.deepcopy(row.data["receipt"])
        if (
            receipt is None
            or row.project_id.hex != pointer["project_id"]
            or row.data.get("kind") != EVENT_KIND
            or row.id.hex != pointer["event_id"]
            or receipt.get("event_id") != row.id.hex
            or receipt.get("project_id") != pointer["project_id"]
            or receipt.get("repository_id") != repository
            or receipt.get("request_digest") != digest(receipt["request"])
            or receipt.get("digest") != pointer["digest"]
            or digest({k: v for k, v in receipt.items() if k != "digest"}) != receipt["digest"]
            or command is not None
            and receipt["request"] != command.model_dump(mode="json")
        ):
            raise AccessDenied("This repository already has another original intake request or receipt")
        return receipt

    async def _create_locked(
        self, session: Any, repository: str, delivery: Data, context: Data, command: SCMProductIntakeCommandV1
    ) -> Data:
        existing_project = await session.scalar(
            select(ForgeProjectEntity.id)
            .where(ForgeProjectEntity.data["definition"]["components"].contains([{"repository_id": repository}]))
            .limit(1)
        )
        if await self.products.find_current(identifier(repository)) is not None or existing_project is not None:
            raise AccessDenied("Intake only creates a first product; inspect the existing project and definition")
        definition = ProjectDefinition(
            name=command.name,
            description=command.description,
            topology="monorepo",
            product_repository_id=repository,
            owners=command.definition.owners,
            components=[
                ProjectComponent.model_validate({**command.component.model_dump(), "repository_id": repository})
            ],
        )
        self.store.roles.snapshot(definition.roles, self.store.policies.selection())
        project_id, event_id, at = uuid4(), uuid4(), now()
        project = {
            "id": project_id.hex,
            "revision": 1,
            "definition": definition.model_dump(mode="json"),
            "created_at": at,
            "updated_at": at,
        }
        project["digest"] = hashlib.sha256(json.dumps(project["definition"], sort_keys=True).encode()).hexdigest()
        product = {
            "repository_id": repository,
            "revision": 1,
            "definition": command.definition.model_dump(mode="json"),
            "created_at": at,
            "origin": {"kind": "manual"},
        }
        product["digest"] = hashlib.sha256(
            json.dumps(product, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        user_id = context["consent"]["native_actor"]["user_id"]
        receipt = {
            "version": 1,
            "event_id": event_id.hex,
            "project_id": project_id.hex,
            "repository_id": repository,
            "native_user_id": user_id,
            "authentication_method": "scm_mapping",
            "verified_receipt_id": delivery["id"],
            "verified_reference": context["reference"],
            "consent_digest": context["consent"]["digest"],
            "request": command.model_dump(mode="json"),
            "request_digest": digest(command.model_dump(mode="json")),
            "project_revision": 1,
            "project_digest": project["digest"],
            "product_revision": 1,
            "product_digest": product["digest"],
            "created_at": at,
            "status": "registered",
        }
        receipt["digest"] = digest(receipt)
        session.add(ForgeProjectEntity(id=project_id, data=project))
        await session.flush()
        session.add(ForgeProjectRevisionEntity(project_id=project_id, revision=1, data=copy.deepcopy(project)))
        session.add(
            ForgeProductDefinitionEntity(
                repository_id=identifier(repository), revision=1, digest=product["digest"], data=product
            )
        )
        session.add(
            ForgeMembershipEntity(
                project_id=project_id, user_id=identifier(user_id), role="owner", active=True, revision=1
            )
        )
        session.add(
            ForgeAccessEventEntity(
                project_id=project_id,
                data={
                    "kind": "membership_changed",
                    "user_id": user_id,
                    "previous_role": None,
                    "role": "owner",
                    "revision": 1,
                    "actor_id": user_id,
                    "at": at,
                },
            )
        )
        session.add(
            ForgeAccessEventEntity(id=event_id, project_id=project_id, data={"kind": EVENT_KIND, "receipt": receipt})
        )
        return receipt

    async def process(self, receipt_id: str) -> Data:
        caller = CURRENT_PRINCIPAL.get()
        self._outside(caller)
        async with self.store._unit():
            row = await self.inbox.find_by_id(identifier(receipt_id))
            if row is None:
                raise KeyError("Original intake delivery not found")
            repository = row.repository_id.hex
        delivery = await self._delivery(receipt_id, repository)
        command = SCMProductIntakeCommandV1.model_validate(delivery["command"])
        context = await self._context(repository, delivery)
        await self._preflight(repository, delivery)
        await self.store.admit_credentials({"request": command.model_dump(mode="json"), "consent": context["consent"]})
        self._outside(caller)
        async with self._locked(repository, context["consent"]["native_actor"]["user_id"]) as (session, settings):
            row = await self.inbox.find_by_id(identifier(receipt_id), lock=LockMode.PESSIMISTIC_WRITE)
            if row is None:
                raise AccessDenied("The original intake delivery disappeared")
            current = await self._delivery(receipt_id, repository)
            if SCMProductIntakeCommandV1.model_validate(current["command"]).model_dump(
                mode="json"
            ) != command.model_dump(mode="json"):
                raise AccessDenied("The original intake command changed during preparation")
            if await self._context(repository, current) != context or CURRENT_PRINCIPAL.get() != caller:
                raise AccessDenied("The intake authority changed before its native commit")
            state = intake_state(settings.data)
            retained = await self._operation(state, command, repository)
            if retained is None:
                if row.status != "received" or row.data.get("action_status") not in {None, "not_dispatched"}:
                    raise AccessDenied("The original intake outcome requires exact receipt readback")
                retained = await self._create_locked(session, repository, current, context, command)
                state["operation"] = {key: retained[key] for key in ("event_id", "project_id", "digest")}
                settings.data = {**settings.data, STATE_KEY: state}
                await self.settings.save(settings)
            if (
                retained["native_user_id"] != context["consent"]["native_actor"]["user_id"]
                or retained["consent_digest"] != context["consent"]["digest"]
            ):
                raise AccessDenied("The original intake belongs to another native consent")
            if row.status == "processed":
                if row.data.get("action_result") != retained or row.data.get("action_status") != "completed":
                    raise AccessDenied("The intake delivery does not bind its original completed receipt")
            elif row.status == "received" and row.data.get("action_status") in {None, "not_dispatched"}:
                row.status = "processed"
                row.data = {
                    **row.data,
                    "status": "processed",
                    "reason": None,
                    "action_status": "completed",
                    "action_result": retained,
                    "feedback_status": "pending",
                    "feedback_stage": "action_completed",
                    "updated_at": now(),
                }
                await self.inbox.save(row)
            else:
                raise AccessDenied("The original intake delivery cannot be silently retried")
            if await self._context(repository, row.data) != context or CURRENT_PRINCIPAL.get() != caller:
                raise AccessDenied("The intake authority changed before commit")
            self.store.reject_credentials(row.data)
            output = copy.deepcopy(row.data)
        await self.store.admit_credentials(output)
        await self._context(repository, await self._delivery(receipt_id, repository))
        self._outside(caller)
        self.store.reject_credentials(output)
        return output

    async def authorize_feedback(self, receipt_id: str, *, remote: bool = True) -> None:
        caller = CURRENT_PRINCIPAL.get()
        self._outside(caller)
        async with self.store._unit():
            row = await self.inbox.find_by_id(identifier(receipt_id))
            if row is None:
                raise AccessDenied("Original intake feedback receipt unavailable")
            repository = row.repository_id.hex
        delivery = await self._delivery(receipt_id, repository)
        if delivery["status"] != "processed" or delivery.get("action_status") != "completed":
            raise AccessDenied("Intake feedback requires its completed original operation")
        context = await self._context(repository, delivery)
        async with self.store._unit():
            settings = await self.settings.find_for_repository(identifier(repository))
            operation = await self._operation(
                intake_state(settings.data if settings else {}),
                SCMProductIntakeCommandV1.model_validate(delivery["command"]),
                repository,
            )
            if operation is None or delivery.get("action_result") != operation:
                raise AccessDenied("Feedback must retain its original immutable intake result")
        if remote:
            await self._preflight(repository, delivery)
        self._source_cut(repository, delivery, context["repository"], context["reference"])
        self._outside(caller)

    async def result(self, receipt_id: str) -> Data:
        caller = CURRENT_PRINCIPAL.get()
        self._outside(caller)
        await self.authorize_feedback(receipt_id, remote=False)
        async with self.store._unit():
            row = await self.inbox.find_by_id(identifier(receipt_id))
            if row is None:
                raise AccessDenied("The original intake result is unavailable")
            repository = row.repository_id.hex
        output = await self._delivery(receipt_id, repository)
        await self.store.admit_credentials(output)
        await self.authorize_feedback(receipt_id, remote=False)
        self._outside(caller)
        self.store.reject_credentials(output)
        return output

    async def operation(self, repository: str, request_id: str) -> Data:
        caller = CURRENT_PRINCIPAL.get()
        self._outside(caller)
        await self._admin(caller)
        repository, request_id = identifier(repository).hex, identifier(request_id).hex
        async with self.store._unit():
            row = await self.settings.find_for_repository(identifier(repository))
            result = await self._operation(intake_state(row.data if row else {}), None, repository)
            if result is None or result["request"]["request_id"] != request_id:
                raise KeyError("Original intake request not found; absence does not authorize another write")
        await self.store.admit_credentials(result)
        await self._admin(caller)
        self._outside(caller)
        self.store.reject_credentials(result)
        return result
