# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Explicit whole-product SCM capability with current native edit admission."""

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
from sqlalchemy import Text, cast, func, select, text

from firefly_forge.access_persistence import (
    ForgeAccessEventEntity,
    ForgeAccessEventRepository,
    ForgeMembershipRepository,
)
from firefly_forge.collaboration_persistence import (
    ForgeCollaborationSettingsRepository,
    ForgeProductDefinitionEntity,
    ForgeWebhookInboxRepository,
)
from firefly_forge.identity import Principal
from firefly_forge.identity_persistence import ForgeIdentityUserRepository
from firefly_forge.journal import digest
from firefly_forge.product_lifecycle import ProductLifecycle
from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied, ProjectAccess
from firefly_forge.project_persistence import ForgeProjectEntity, ForgeProjectRepository
from firefly_forge.scm_actor_authority import SCMActorAuthority, binding_key
from firefly_forge.scm_authority_models import VerifiedSCMReferenceV1
from firefly_forge.scm_product_change_models import (
    SCMProductChangeCommandV1,
    SCMProductChangeGrantInputV1,
    SCMProductChangeGrantReceiptV1,
    SCMProductChangeReceiptV1,
)
from firefly_forge.store import Store, identifier, now

Data = dict[str, Any]
OPERATION_KIND = "scm_product_change_receipt"
STATE_KEY = "_scm_product_change_authority_v1"
EVENT_KIND = "scm_product_change_grant_revision"


def grant_state(data: Data) -> Data:
    state = copy.deepcopy(data.get(STATE_KEY, {"version": 1, "heads": {}, "requests": {}, "operations": {}}))
    if (
        not isinstance(state, dict)
        or set(state) != {"version", "heads", "requests", "operations"}
        or type(state["version"]) is not int
        or state["version"] != 1
        or not isinstance(state["heads"], dict)
        or not isinstance(state["requests"], dict)
        or not isinstance(state["operations"], dict)
        or len(state["operations"]) > 5000
        or len(state["heads"]) > 100
        or len(state["requests"]) > 5000
        or len(json.dumps(state, ensure_ascii=False).encode()) > 16 * 1024 * 1024
    ):
        raise ValueError("The product-edit grant index is invalid or exceeds its bounded retention")
    return state


def validate_product_scope(project: Data, definition: Data) -> None:
    components = {item["id"] for item in project["definition"]["components"]}
    for feature in definition["features"]:
        if set(feature["component_refs"]) - components:
            raise ValueError("Product feature references require components in the reviewed project")
    for integration in definition.get("integrations", []):
        if integration["kind"] == "owned" and integration.get("service_ref") not in components:
            raise ValueError("Owned product integration references require a component in the reviewed project")


class SCMProductChangeAuthority:
    """A Factory bean; all mutable admission checks are repeated under native locks."""

    def __init__(
        self,
        store: Store,
        authority: SCMActorAuthority,
        settings: ForgeCollaborationSettingsRepository,
        inbox: ForgeWebhookInboxRepository,
        events: ForgeAccessEventRepository,
    ) -> None:
        self.store, self.authority = store, authority
        self.settings, self.inbox, self.events = settings, inbox, events

    async def _record(self, event_id: str, project: str, repository: str) -> Data:
        async with self.store._unit() as session:
            size = await session.scalar(
                select(func.octet_length(cast(ForgeAccessEventEntity.data, Text))).where(
                    ForgeAccessEventEntity.id == identifier(event_id)
                )
            )
            if size is None:
                raise KeyError("Product edit grant not found")
            if size > 65536:
                raise ValueError("Product edit grant exceeds 64 KiB")
            row = await self.events.find_by_id(identifier(event_id))
            if (
                row is None
                or row.project_id.hex != project
                or set(row.data) != {"kind", "receipt"}
                or row.data["kind"] != EVENT_KIND
            ):
                raise ValueError("Product edit grant event scope is invalid")
            receipt = SCMProductChangeGrantReceiptV1.model_validate(row.data["receipt"]).model_dump(mode="json")
        if set(receipt) != {
            "version",
            "event_id",
            "project_id",
            "repository_id",
            "binding_key",
            "native_user_id",
            "revision",
            "previous_digest",
            "prepared_at",
            "administering_user_id",
            "request",
            "request_digest",
            "digest",
        }:
            raise ValueError("Product edit grant envelope is invalid")
        request = SCMProductChangeGrantInputV1.model_validate(receipt["request"]).model_dump(mode="json")
        if (
            receipt["version"] != 1
            or type(receipt["version"]) is not int
            or receipt["event_id"] != event_id
            or receipt["project_id"] != project
            or receipt["repository_id"] != repository
            or request != receipt["request"]
            or digest(request) != receipt["request_digest"]
            or receipt["previous_digest"] != request["expected_previous_digest"]
            or digest({k: v for k, v in receipt.items() if k != "digest"}) != receipt["digest"]
            or type(receipt["revision"]) is not int
            or not 1 <= receipt["revision"] <= 50
        ):
            raise ValueError("Product edit grant immutable binding is invalid")
        binding = await self.authority._existing(project, repository, request["expected_binding_request_id"])
        if (
            binding is None
            or binding["digest"] != request["expected_binding_digest"]
            or binding["binding_key"] != receipt["binding_key"]
            or binding["request"]["native_user_id"] != receipt["native_user_id"]
        ):
            raise ValueError("The product grant does not bind its original immutable identity mapping")
        return receipt

    async def _grant(
        self, project: str, repository: str, *, key: str | None = None, request_id: str | None = None
    ) -> Data | None:
        async with self.store._unit() as session:
            row = await self.settings.find_for_repository(identifier(repository))
            state = grant_state(row.data if row else {})
            pointer = state["heads"].get(key) if key else state["requests"].get(project + ":" + str(request_id))
            latest = None
            if key:
                latest = await session.scalar(
                    select(ForgeAccessEventEntity.id)
                    .where(
                        ForgeAccessEventEntity.project_id == identifier(project),
                        ForgeAccessEventEntity.data["kind"].as_string() == EVENT_KIND,
                        ForgeAccessEventEntity.data["receipt"]["repository_id"].as_string() == repository,
                        ForgeAccessEventEntity.data["receipt"]["binding_key"].as_string() == key,
                    )
                    .order_by(ForgeAccessEventEntity.data["receipt"]["revision"].as_integer().desc())
                    .limit(1)
                )
        if pointer is None:
            if latest is not None:
                raise ValueError("Product edit history has no consistent current head")
            return None
        if not isinstance(pointer, dict) or set(pointer) != {"event_id", "digest"}:
            raise ValueError("Product edit grant pointer is invalid")
        retained = await self._record(pointer["event_id"], project, repository)
        if (
            retained["digest"] != pointer["digest"]
            or key
            and (latest is None or latest.hex != retained["event_id"] or retained["binding_key"] != key)
            or request_id
            and retained["request"]["request_id"] != request_id
        ):
            raise ValueError("Product edit grant pointer does not bind immutable history")
        return retained

    async def _return(self, actor: Any, receipt: Data, *, admin: bool) -> Data:
        await self.store.admit_credentials(receipt)
        await self.authority._reader(actor, receipt["project_id"], receipt["repository_id"], admin=admin)
        self.store.reject_credentials(receipt)
        return copy.deepcopy(receipt)

    async def get(self, project: str, repository: str, request_id: str) -> Data:
        if current_unit_of_work() is not None:
            raise ValueError("Read product edit grants outside a caller transaction")
        actor = CURRENT_PRINCIPAL.get()
        await self.authority._reader(actor, project, repository, admin=False)
        receipt = await self._grant(project, repository, request_id=request_id)
        if receipt is None:
            raise KeyError("Product edit grant request not found; absence never permits another write")
        return await self._return(actor, receipt, admin=False)

    async def current(self, project: str, repository: str, binding_request: str) -> Data:
        if current_unit_of_work() is not None:
            raise ValueError("Read product edit grants outside a caller transaction")
        actor = CURRENT_PRINCIPAL.get()
        await self.authority._reader(actor, project, repository, admin=False)
        binding = await self.authority._existing(project, repository, binding_request)
        if binding is None:
            raise KeyError("V1 identity binding not found")
        receipt = await self._grant(project, repository, key=binding["binding_key"])
        if receipt is None:
            raise KeyError("No separately consented product-edit grant exists")
        return await self._return(actor, receipt, admin=False)

    async def _binding(self, project: str, repository: str, request: SCMProductChangeGrantInputV1) -> Data:
        binding = await self.authority._existing(project, repository, request.expected_binding_request_id)
        if binding is None:
            raise AccessDenied("Review an exact current V1 mapping first")
        head = await self.authority._head(project, repository, binding["binding_key"])
        project_cut = await self._project(project, repository)
        if (
            head != binding
            or binding["digest"] != request.expected_binding_digest
            or not binding["request"]["active"]
            or set(request.component_ids) != {item["id"] for item in project_cut["definition"]["components"]}
            or project_cut["definition"].get("product_repository_id") != repository
            or digest(project_cut) != request.expected_project_binding_digest
            or binding["project_binding_digest"] != digest(project_cut)
        ):
            raise AccessDenied("The reviewed V1 mapping or project scope changed")
        repository_cut = await self.store.repository(repository)
        reference = binding["verified_reference"]
        scope = self.authority.scm.delegation_scope(repository_cut)
        if scope != {k: reference[k] for k in scope} or not self.authority._key_current(repository, reference):
            raise AccessDenied("The V1 connection or verification key changed")
        await self._permissions(binding["request"]["native_user_id"], project, repository)
        if self.authority.scm.delegation_scope(repository_cut) != scope or not self.authority._key_current(
            repository, reference
        ):
            raise AccessDenied("The source connection changed during product capability admission")
        return binding

    async def _preflight(self, context: Data) -> None:
        binding = context["binding"]
        repository = await self.store.repository(binding["repository_id"])
        reference = binding["verified_reference"]
        grants = await self.authority.scm.repositories(reference["provider"])
        if not any(
            str(item["id"]).strip("{}") == reference["remote_repository_id"] and item["url"] == repository["url"]
            for item in grants
        ):
            raise AccessDenied("The application repository grant is no longer available")

    async def configure(self, project: str, repository: str, request: SCMProductChangeGrantInputV1) -> Data:
        if current_unit_of_work() is not None:
            raise ValueError("Admit product edit consent outside a caller transaction")
        request = SCMProductChangeGrantInputV1.model_validate(request.model_dump(mode="json"))
        actor = CURRENT_PRINCIPAL.get()
        await self.authority._reader(actor, project, repository, admin=True)
        assert actor is not None
        incoming = request.model_dump(mode="json")
        await self.store.admit_credentials(incoming)
        await self.authority._reader(actor, project, repository, admin=True)
        retained = await self._grant(project, repository, request_id=request.request_id)
        if retained is not None:
            if retained["request"] != incoming or retained["administering_user_id"] != actor.id.hex:
                raise AccessDenied("The retained request belongs to another input or administrator")
            return await self._return(actor, retained, admin=True)
        binding = await self._binding(project, repository, request)
        await self._preflight({"binding": binding})
        async with self.store._unit() as session:
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": self.store.schema + ":members:" + project},
            )
            for user in sorted({actor.id.hex, binding["request"]["native_user_id"]}):
                identity = await ForgeIdentityUserRepository().find_by_id(
                    identifier(user), lock=LockMode.PESSIMISTIC_WRITE
                )
                if identity is None or not identity.enabled:
                    raise AccessDenied("Both administrator and delegated user must remain enabled")
            await ForgeProjectRepository().find_by_id(identifier(project), lock=LockMode.PESSIMISTIC_WRITE)
            await self.store.repository_records.find_by_id(identifier(repository), lock=LockMode.PESSIMISTIC_WRITE)
            await self.authority._reader(actor, project, repository, admin=True)
            if await self._binding(project, repository, request) != binding:
                raise AccessDenied("The reviewed authority cut changed during admission")
            existing = await self._grant(project, repository, request_id=request.request_id)
            if existing is not None:
                if existing["request"] != incoming or existing["administering_user_id"] != actor.id.hex:
                    raise AccessDenied("The retained request belongs to another input or administrator")
                retained = existing
            else:
                row = await self.settings.find_for_repository(identifier(repository))
                if row is None:
                    raise AccessDenied("Configure collaboration before consenting to product edit")
                state = grant_state(row.data)
                previous = await self._grant(project, repository, key=binding["binding_key"])
                if request.expected_previous_digest != (previous["digest"] if previous else None):
                    raise AccessDenied("Product edit grant revision changed; preserve the reviewed request")
                revision = previous["revision"] + 1 if previous else 1
                if (
                    revision > 50
                    or len(state["requests"]) >= 5000
                    or binding["binding_key"] not in state["heads"]
                    and len(state["heads"]) >= 100
                ):
                    raise ValueError("Product edit grant retention limit reached")
                retained = dict(
                    version=1,
                    event_id=uuid4().hex,
                    project_id=project,
                    repository_id=repository,
                    binding_key=binding["binding_key"],
                    native_user_id=binding["request"]["native_user_id"],
                    revision=revision,
                    previous_digest=request.expected_previous_digest,
                    prepared_at=now(),
                    administering_user_id=actor.id.hex,
                    request=incoming,
                    request_digest=digest(incoming),
                )
                retained["digest"] = digest(retained)
                SCMProductChangeGrantReceiptV1.model_validate(retained)
                envelope = {"kind": EVENT_KIND, "receipt": retained}
                total = await session.scalar(
                    select(
                        func.coalesce(func.sum(func.octet_length(cast(ForgeAccessEventEntity.data, Text))), 0)
                    ).where(
                        ForgeAccessEventEntity.data["kind"].as_string().in_([EVENT_KIND, OPERATION_KIND]),
                        ForgeAccessEventEntity.data["receipt"]["repository_id"].as_string() == repository,
                    )
                )
                if (
                    len(json.dumps(envelope, ensure_ascii=False).encode()) > 65536
                    or int(total or 0) + len(json.dumps(envelope, ensure_ascii=False).encode()) > 16 * 1024 * 1024
                ):
                    raise ValueError("Product edit grant history exceeds its byte budget")
                self.store.reject_credentials(retained)
                await self.events.save(
                    ForgeAccessEventEntity(
                        id=identifier(retained["event_id"]), project_id=identifier(project), data=envelope
                    )
                )
                pointer = {"event_id": retained["event_id"], "digest": retained["digest"]}
                state["heads"][binding["binding_key"]] = pointer
                state["requests"][project + ":" + request.request_id] = pointer
                grant_state({STATE_KEY: state})
                row.data = {**row.data, STATE_KEY: state}
                await self.settings.save(row)
            await self.authority._reader(actor, project, repository, admin=True)
            if await self._binding(project, repository, request) != binding:
                raise AccessDenied("The product capability source changed before commit")
            self.store.reject_credentials(retained)
        return await self._return(actor, retained, admin=True)

    async def _project(self, project: str, repository: str) -> Data:
        async with self.store._unit() as session:
            data = await session.scalar(
                select(ForgeProjectEntity.data).where(
                    ForgeProjectEntity.id == identifier(project),
                    func.octet_length(cast(ForgeProjectEntity.data, Text)) <= 1024 * 1024,
                )
            )
            if (
                data is None
                or data.get("id") != project
                or type(data.get("revision")) is not int
                or data["revision"] < 1
            ):
                raise AccessDenied("The native product project is unavailable or exceeds 1 MiB")
            definition = data.get("definition", {})
            if definition.get("product_repository_id") != repository or not any(
                item["repository_id"] == repository for item in definition.get("components", [])
            ):
                raise AccessDenied("Select this existing project's exact product repository")
            return copy.deepcopy(data)

    async def _permissions(self, user: str, project: str, repository: str) -> list[str]:
        async with self.store._unit() as session:
            related = list(
                await session.scalars(
                    select(ForgeProjectEntity.id)
                    .where(
                        (ForgeProjectEntity.data["definition"]["product_repository_id"].as_string() == repository)
                        | ForgeProjectEntity.data["definition"]["components"].contains([{"repository_id": repository}])
                    )
                    .order_by(ForgeProjectEntity.id)
                    .limit(101)
                )
            )
            if len(related) > 100 or identifier(project) not in related:
                raise AccessDenied("The product repository's project scope is unavailable or exceeds 100 projects")
            identity = await ForgeIdentityUserRepository().find_by_id(identifier(user))
            if identity is None or not identity.enabled:
                raise AccessDenied("The delegated native identity is unavailable or suspended")
            memberships = await ForgeMembershipRepository().for_user(identity.id)
        for project_id in related:
            if not any(
                item.project_id == project_id
                and all(ProjectAccess.permits(item.role, permission) for permission in ("read", "edit"))
                for item in memberships
            ):
                raise AccessDenied(
                    "Current native read and edit access is required in every project sharing the product repository"
                )
        return [item.hex for item in related]

    async def _delivery(self, receipt_id: str) -> Data:
        async with self.store._unit() as session:
            from firefly_forge.collaboration_persistence import ForgeWebhookInboxEntity

            size = await session.scalar(
                select(func.octet_length(cast(ForgeWebhookInboxEntity.data, Text))).where(
                    ForgeWebhookInboxEntity.id == identifier(receipt_id)
                )
            )
            if not isinstance(size, int) or not 0 < size <= 262144:
                raise AccessDenied("The signed delivery is unavailable or exceeds 256 KiB")
            row = await self.inbox.find_by_id(identifier(receipt_id))
            if (
                row is None
                or row.id.hex != row.data.get("id")
                or row.repository_id.hex != row.data.get("repository_id")
                or row.provider != row.data.get("provider")
                or row.status != row.data.get("status")
            ):
                raise AccessDenied("The signed delivery scalar scope is inconsistent")
            return copy.deepcopy(row.data)

    async def _context(self, delivery: Data) -> Data:
        command = SCMProductChangeCommandV1.model_validate(delivery["command"])
        project, repository = command.project_id, delivery["repository_id"]
        reference = VerifiedSCMReferenceV1.model_validate(delivery.get("verification")).model_dump(mode="json")
        if (
            reference["payload_digest"] != delivery.get("digest")
            or reference["provider"] != delivery.get("provider")
            or reference["external_actor_id"] != delivery.get("actor")
            or reference["remote_repository_id"] != delivery.get("remote_id")
        ):
            raise AccessDenied("The signed product edit does not bind its verified source")
        binding = await self.authority._head(project, repository, binding_key(project, reference))
        grant = await self._grant(project, repository, key=binding["binding_key"])
        if (
            grant is None
            or grant["request"]["request_id"] != command.expected_grant_request_id
            or grant["digest"] != command.expected_grant_digest
            or not grant["request"]["active"]
        ):
            raise AccessDenied("A current separately consented whole-product edit capability is required")
        if (
            await self._binding(project, repository, SCMProductChangeGrantInputV1.model_validate(grant["request"]))
            != binding
        ):
            raise AccessDenied("The product edit binding changed")
        if (
            grant["native_user_id"] != binding["request"]["native_user_id"]
            or grant["request"]["expected_project_binding_digest"] != command.expected_project_binding_digest
        ):
            raise AccessDenied("The current project or native user differs from the reviewed product edit")
        if {k: v for k, v in reference.items() if k != "payload_digest"} != {
            k: v for k, v in binding["verified_reference"].items() if k != "payload_digest"
        }:
            raise AccessDenied("The signed account, installation, connection or verification key changed")
        async with self.store._unit():
            settings_row = await self.settings.find_for_repository(identifier(repository))
            settings = settings_row.data if settings_row else {}
            if not settings.get("enabled") or delivery["actor"] not in settings.get("trusted_actors", []):
                raise AccessDenied("The repository collaboration actor is no longer enabled")
            identity = await ForgeIdentityUserRepository().find_by_id(identifier(grant["native_user_id"]))
            if identity is None or not identity.enabled:
                raise AccessDenied("The delegated native user is unavailable or suspended")
            principal = Principal(
                identity.id,
                identity.issuer,
                identity.subject,
                authentication_method="scm_mapping",
                scm_delegation={
                    "action": "product_change",
                    "project_id": project,
                    "repository_id": repository,
                    "grant_digest": grant["digest"],
                },
            )
        if delivery.get("global_ingress"):
            channel = self.store.configuration._read(self.store.configuration.directory / "channel-bootstrap.json").get(
                reference["provider"], {}
            )
            if not channel.get("enabled") or delivery["actor"] not in channel.get("trusted_actors", []):
                raise AccessDenied("The signed global ingress actor is no longer enabled")
        projects = await self._permissions(principal.id.hex, project, repository)
        project_cut = await self._project(project, repository)
        if digest(project_cut) != command.expected_project_binding_digest:
            raise AccessDenied("The reviewed product project cut changed")
        repository_cut = await self.store.repository(repository)
        if self.authority.scm.delegation_scope(repository_cut) != {
            key: reference[key] for key in self.authority.scm.delegation_scope(repository_cut)
        } or not self.authority._key_current(repository, reference):
            raise AccessDenied("The product source connection or verification key changed")
        if delivery.get("global_ingress"):
            channel = self.store.configuration._read(self.store.configuration.directory / "channel-bootstrap.json").get(
                reference["provider"], {}
            )
            if not channel.get("enabled") or delivery["actor"] not in channel.get("trusted_actors", []):
                raise AccessDenied("The global product ingress actor changed")
        return {
            "project": project_cut,
            "repository": repository,
            "binding": binding,
            "grant": grant,
            "user_id": principal.id.hex,
            "projects": projects,
            "reference": reference,
        }

    async def _product(self, repository: str, revision: int | None = None) -> Data:
        from firefly_forge.collaboration_persistence import ForgeProductDefinitionRepository

        async with self.store._unit() as session:
            query = select(
                ForgeProductDefinitionEntity.id,
                ForgeProductDefinitionEntity.revision,
                ForgeProductDefinitionEntity.digest,
                func.octet_length(cast(ForgeProductDefinitionEntity.data, Text)),
            ).where(ForgeProductDefinitionEntity.repository_id == identifier(repository))
            if revision is not None:
                query = query.where(ForgeProductDefinitionEntity.revision == revision)
            projected = (
                await session.execute(query.order_by(ForgeProductDefinitionEntity.revision.desc()).limit(1))
            ).one_or_none()
            if projected is None or not isinstance(projected[3], int) or not 0 < projected[3] <= 1024 * 1024:
                raise ValueError("The saved product revision is unavailable or exceeds 1 MiB")
            row = await ForgeProductDefinitionRepository().find_by_id(projected[0])
            if (
                row is None
                or row.repository_id.hex != repository
                or row.revision != projected[1]
                or row.digest != projected[2]
                or row.data.get("revision") != row.revision
                or row.data.get("repository_id") != repository
            ):
                raise ValueError("The immutable product revision scalar binding is inconsistent")
            unsigned = {k: v for k, v in row.data.items() if k != "digest"}
            expected = (
                digest(unsigned)
                if "change_receipt" in row.data
                else hashlib.sha256(json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            )
            if expected != row.digest:
                raise ValueError("The immutable product revision binding is inconsistent")
            return copy.deepcopy(row.data)

    async def _operation(self, project: str, repository: str, request_id: str) -> Data | None:
        async with self.store._unit() as session:
            row = await self.settings.find_for_repository(identifier(repository))
            state = grant_state(row.data if row else {})
            pointer = state["operations"].get(project + ":" + request_id)
            event_ids = list(
                await session.scalars(
                    select(ForgeAccessEventEntity.id)
                    .where(
                        ForgeAccessEventEntity.project_id == identifier(project),
                        ForgeAccessEventEntity.data["kind"].as_string() == OPERATION_KIND,
                        ForgeAccessEventEntity.data["receipt"]["repository_id"].as_string() == repository,
                        ForgeAccessEventEntity.data["receipt"]["request"]["change"]["request_id"].as_string()
                        == request_id,
                    )
                    .limit(2)
                )
            )
            if pointer is None:
                if event_ids:
                    raise ValueError("The exact product edit has no consistent retained index")
                return None
            if (
                not isinstance(pointer, dict)
                or set(pointer) != {"event_id", "digest"}
                or len(event_ids) != 1
                or event_ids[0].hex != pointer["event_id"]
            ):
                raise ValueError("The product edit pointer does not bind immutable history")
            event_id = event_ids[0]
            size = await session.scalar(
                select(func.octet_length(cast(ForgeAccessEventEntity.data, Text))).where(
                    ForgeAccessEventEntity.id == event_id
                )
            )
            if not isinstance(size, int) or not 0 < size <= 131072:
                raise ValueError("The immutable product edit exceeds 128 KiB")
            event = await self.events.find_by_id(event_id)
            if (
                event is None
                or event.project_id.hex != project
                or set(event.data) != {"kind", "receipt"}
                or event.data["kind"] != OPERATION_KIND
            ):
                raise ValueError("The immutable product edit event scope is invalid")
            receipt = SCMProductChangeReceiptV1.model_validate(event.data["receipt"]).model_dump(mode="json")
        if (
            receipt["event_id"] != event_id.hex
            or receipt["project_id"] != project
            or receipt["repository_id"] != repository
            or receipt["request"]["change"]["request_id"] != request_id
            or receipt["request"]["project_id"] != project
            or digest(receipt["request"]) != receipt["request_digest"]
            or digest({k: v for k, v in receipt.items() if k != "digest"}) != receipt["digest"]
            or pointer["digest"] != receipt["digest"]
        ):
            raise ValueError("The immutable product edit envelope is inconsistent")
        grant = await self._grant(project, repository, request_id=receipt["grant_request_id"])
        if (
            grant is None
            or grant["digest"] != receipt["grant_digest"]
            or grant["native_user_id"] != receipt["native_user_id"]
            or grant["request"]["expected_binding_request_id"] != receipt["binding_request_id"]
            or grant["request"]["expected_binding_digest"] != receipt["binding_digest"]
            or receipt["request"]["expected_grant_request_id"] != receipt["grant_request_id"]
            or receipt["request"]["expected_grant_digest"] != receipt["grant_digest"]
            or receipt["request"]["expected_project_binding_digest"]
            != grant["request"]["expected_project_binding_digest"]
        ):
            raise ValueError("The immutable product edit does not bind its original capability")
        product = await self._product(repository, receipt["product_revision"])
        if (
            product["digest"] != receipt["product_digest"]
            or product.get("change_receipt", {}).get("request_id") != request_id
            or product["change_receipt"].get("actor_id") != receipt["native_user_id"]
            or product["change_receipt"].get("request_digest") != digest(receipt["request"]["change"])
        ):
            raise ValueError("The exact product edit does not bind its immutable product revision")
        return receipt

    async def operation(self, project: str, repository: str, request_id: str) -> Data:
        if current_unit_of_work() is not None:
            raise ValueError("Read product edit receipts outside a caller transaction")
        actor = CURRENT_PRINCIPAL.get()
        await self.authority._reader(actor, project, repository, admin=False)
        retained = await self._operation(project, repository, request_id)
        if retained is None:
            raise KeyError("The original product edit is unconfirmed; absence never permits another write")
        return await self._return(actor, retained, admin=False)

    @asynccontextmanager
    async def _locked(self, delivery: Data, context: Data) -> AsyncIterator[Any]:
        async with self.store._unit() as session:
            for project in sorted(context["projects"]):
                await session.execute(
                    text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                    {"key": self.store.schema + ":members:" + project},
                )
            identity = await ForgeIdentityUserRepository().find_by_id(
                identifier(context["user_id"]), lock=LockMode.PESSIMISTIC_WRITE
            )
            if identity is None or not identity.enabled:
                raise AccessDenied("The locked delegated native identity is unavailable")
            for project in sorted(context["projects"]):
                if (
                    await ForgeProjectRepository().find_by_id(identifier(project), lock=LockMode.PESSIMISTIC_WRITE)
                    is None
                ):
                    raise AccessDenied("A product repository project is unavailable")
            if (
                await self.store.repository_records.find_by_id(
                    identifier(context["repository"]), lock=LockMode.PESSIMISTIC_WRITE
                )
                is None
            ):
                raise AccessDenied("The product repository is unavailable")
            row = await self.inbox.find_by_id(identifier(delivery["id"]), lock=LockMode.PESSIMISTIC_WRITE)
            if (
                row is None
                or row.data.get("command") != delivery["command"]
                or row.data.get("verification") != delivery.get("verification")
                or row.data.get("digest") != delivery.get("digest")
                or row.repository_id.hex != context["repository"]
                or row.status != row.data.get("status")
            ):
                raise AccessDenied("The signed product edit changed during admission")
            yield row

    async def _same_operation(self, retained: Data, context: Data, command: SCMProductChangeCommandV1) -> None:
        if (
            retained["request_digest"] != digest(command.model_dump(mode="json"))
            or retained["native_user_id"] != context["user_id"]
            or retained["binding_digest"] != context["binding"]["digest"]
            or retained["grant_digest"] != context["grant"]["digest"]
            or retained["project_id"] != command.project_id
            or retained["repository_id"] != context["repository"]
        ):
            raise AccessDenied("The retained product edit belongs to another request, native actor or authority cut")

    async def _save_operation(
        self, delivery: Data, context: Data, command: SCMProductChangeCommandV1, product: Data
    ) -> Data:
        retained = dict(
            version=1,
            event_id=uuid4().hex,
            project_id=command.project_id,
            repository_id=context["repository"],
            native_user_id=context["user_id"],
            authentication_method="scm_mapping",
            binding_request_id=context["binding"]["request"]["request_id"],
            binding_digest=context["binding"]["digest"],
            grant_request_id=context["grant"]["request"]["request_id"],
            grant_digest=context["grant"]["digest"],
            verified_receipt_id=delivery["id"],
            verified_reference=context["reference"],
            request=command.model_dump(mode="json"),
            request_digest=digest(command.model_dump(mode="json")),
            product_revision=product["revision"],
            product_digest=product["digest"],
            prepared_at=now(),
        )
        retained["digest"] = digest(retained)
        SCMProductChangeReceiptV1.model_validate(retained)
        envelope = {"kind": OPERATION_KIND, "receipt": retained}
        async with self.store._unit() as session:
            total = await session.scalar(
                select(func.coalesce(func.sum(func.octet_length(cast(ForgeAccessEventEntity.data, Text))), 0)).where(
                    ForgeAccessEventEntity.data["kind"].as_string().in_([EVENT_KIND, OPERATION_KIND]),
                    ForgeAccessEventEntity.data["receipt"]["repository_id"].as_string() == context["repository"],
                )
            )
            size = len(json.dumps(envelope, ensure_ascii=False).encode())
            if size > 131072 or int(total or 0) + size > 16 * 1024 * 1024:
                raise ValueError("Product edit evidence exceeds its bounded retention")
            row = await self.settings.find_for_repository(identifier(context["repository"]))
            if row is None:
                raise AccessDenied("Product collaboration is unavailable")
            state = grant_state(row.data)
            if len(state["operations"]) >= 5000:
                raise ValueError("Product edit retention reached 5000 operations")
            self.store.reject_credentials(envelope)
            await self.events.save(
                ForgeAccessEventEntity(
                    id=identifier(retained["event_id"]), project_id=identifier(command.project_id), data=envelope
                )
            )
            state["operations"][command.project_id + ":" + command.change.request_id] = {
                "event_id": retained["event_id"],
                "digest": retained["digest"],
            }
            grant_state({STATE_KEY: state})
            row.data = {**row.data, STATE_KEY: state}
            await self.settings.save(row)
        return retained

    async def _mark_processed(self, row: Any, retained: Data) -> None:
        row.data = {
            **row.data,
            "status": "processed",
            "action_status": "completed",
            "action_result": retained,
            "reason": None,
            "updated_at": now(),
        }
        row.status = "processed"
        await self.inbox.save(row)

    async def process(self, receipt_id: str, lifecycle: ProductLifecycle) -> Data:
        if current_unit_of_work() is not None:
            raise ValueError("Admit signed product edits outside a caller transaction")
        caller = CURRENT_PRINCIPAL.get()
        initial = await self._delivery(receipt_id)
        command = SCMProductChangeCommandV1.model_validate(initial["command"])
        context = await self._context(initial)
        await self._preflight(context)
        await self.store.admit_credentials({"command": command.model_dump(mode="json"), "grant": context["grant"]})
        context = await self._context(await self._delivery(receipt_id))
        if CURRENT_PRINCIPAL.get() != caller:
            raise AccessDenied("The current product edit processing context changed")
        retained = await self._operation(command.project_id, context["repository"], command.change.request_id)
        prepared = None
        if retained is None:
            base = await self._product(context["repository"], command.change.base_revision)
            prepared = await lifecycle.prepare(context["repository"], command.change, previous=base)
        async with self._locked(initial, context) as row:
            locked = await self._context(row.data)
            if locked != context or CURRENT_PRINCIPAL.get() != caller:
                raise AccessDenied("The reviewed native product edit authority cut changed")
            retained = await self._operation(command.project_id, context["repository"], command.change.request_id)
            if retained is not None:
                await self._same_operation(retained, context, command)
            elif row.status != "received" or row.data.get("action_status") not in {None, "not_dispatched"}:
                raise AccessDenied("The original product edit outcome requires reconciliation without another request")
            else:
                current = await self._product(context["repository"])
                if (
                    current["revision"] != command.change.base_revision
                    or current["digest"] != command.change.base_digest
                ):
                    raise ValueError("The current product base revision or digest changed")
                if prepared is None:
                    raise AccessDenied("The original product edit preparation is unavailable")
                validate_product_scope(context["project"], prepared["prepared"]["definition"])
                product = await lifecycle.apply_prepared_locked(
                    context["repository"], command.change, prepared, actor_id=identifier(context["user_id"])
                )
                retained = await self._save_operation(initial, context, command, product)
            self.store.reject_credentials(retained)
            if row.status == "processed":
                if row.data.get("action_status") != "completed" or row.data.get("action_result") != retained:
                    raise AccessDenied("The delivery does not bind its original exact completed product edit")
            elif row.status == "received" and row.data.get("action_status") in {None, "not_dispatched"}:
                await self._mark_processed(row, retained)
            else:
                raise AccessDenied("The original delivery is unavailable for an admitted product edit")
            if await self._context(row.data) != context or CURRENT_PRINCIPAL.get() != caller:
                raise AccessDenied("The current product edit processing context changed before commit")
            self.store.reject_credentials(row.data)
            output: Data = copy.deepcopy(row.data)
        await self.store.admit_credentials(output)
        await self._context(await self._delivery(receipt_id))
        if CURRENT_PRINCIPAL.get() != caller:
            raise AccessDenied("The product edit committed but permission to return changed")
        self.store.reject_credentials(output)
        return output
