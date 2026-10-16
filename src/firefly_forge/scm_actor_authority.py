# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Explicit, project-scoped native authority delegated through verified SCM channels."""

from __future__ import annotations

import copy
import json
from typing import Any
from uuid import UUID, uuid4

from pyfly.data.relational.sqlalchemy import LockMode
from pyfly.data.transaction import current_unit_of_work
from sqlalchemy import Text, cast, func, literal, select, text
from sqlalchemy.dialects.postgresql import JSONB

from firefly_forge.access_persistence import ForgeAccessEventEntity, ForgeAccessEventRepository
from firefly_forge.collaboration_persistence import (
    ForgeCollaborationSettingsRepository,
    ForgeWebhookInboxRepository,
)
from firefly_forge.identity import Principal
from firefly_forge.identity_persistence import ForgeIdentityUserRepository
from firefly_forge.journal import digest
from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied, Permission, ProjectAccess
from firefly_forge.project_persistence import ForgeProjectRepository
from firefly_forge.scm_authority_models import (
    SCMActorBindingInputV1,
    SCMActorBindingReceiptV1,
    SCMActorBindingSummaryV1,
    VerifiedSCMReferenceV1,
)
from firefly_forge.scm_connections import SCMConnections
from firefly_forge.store import Store, identifier, now

Data = dict[str, Any]
AUTHORITY_KEY = "_scm_authority_v1"
EVENT_KIND = "scm_actor_binding_revision"
MAX_BYTES = 16 * 1024 * 1024


def binding_summary(receipt: Data) -> Data:
    request, reference = receipt["request"], receipt["verified_reference"]
    summary = {
        key: receipt[key]
        for key in (
            "event_id",
            "project_id",
            "repository_id",
            "project_binding_digest",
            "binding_key",
            "revision",
            "digest",
            "prepared_at",
        )
    }
    summary.update(
        {key: request[key] for key in ("request_id", "native_user_id", "component_ids", "allowed_actions", "active")}
    )
    summary.update({key: reference[key] for key in ("provider", "external_actor_id")})
    summary["version"] = 1
    summary["summary_digest"] = digest(summary)
    return SCMActorBindingSummaryV1.model_validate(summary).model_dump(mode="json")


def binding_key(project_id: str, reference: Data) -> str:
    return digest(
        {
            "project_id": project_id,
            **{
                key: reference[key]
                for key in (
                    "provider",
                    "api_origin",
                    "installation_id",
                    "remote_repository_id",
                    "external_actor_id",
                )
            },
        }
    )


def authority_state(data: Data) -> Data:
    value = copy.deepcopy(data.get(AUTHORITY_KEY, {"version": 1, "heads": {}, "requests": {}}))
    if (
        not isinstance(value, dict)
        or set(value) != {"version", "heads", "requests"}
        or type(value["version"]) is not int
        or value["version"] != 1
        or not isinstance(value["heads"], dict)
        or not isinstance(value["requests"], dict)
        or len(value["heads"]) > 100
        or len(value["requests"]) > 5000
        or len(json.dumps(value, ensure_ascii=False).encode()) > MAX_BYTES
    ):
        raise ValueError("SCM authority index is invalid or exceeds its retention limit")
    return value


class SCMActorAuthority:
    def __init__(self, store: Store, scm: SCMConnections | None = None) -> None:
        self.store, self.scm = store, scm or SCMConnections(store)
        self.settings = ForgeCollaborationSettingsRepository()
        self.inbox = ForgeWebhookInboxRepository()
        self.events = ForgeAccessEventRepository()
        self.access = ProjectAccess(store)

    async def _reader(self, actor: Principal | None, project_id: str, repository_id: str, *, admin: bool) -> None:
        if actor is None or CURRENT_PRINCIPAL.get() != actor or actor.authentication_method == "scm_mapping":
            raise AccessDenied("A current native operator is required to administer SCM delegation")
        if admin and not await self.access.current_administrator(actor):
            raise AccessDenied("SCM delegation requires a current factory administrator")
        await self.access.require_project(actor, project_id, "members" if admin else "read")
        await self.access.require_repository(actor, repository_id, "read")
        async with self.store._unit():
            user = await ForgeIdentityUserRepository().find_by_id(actor.id)
            if user is None or not user.enabled or (user.issuer, user.subject) != (actor.issuer, actor.subject):
                raise AccessDenied("The native operator identity is unavailable or changed")
        if CURRENT_PRINCIPAL.get() != actor:
            raise AccessDenied("The current native operator changed during admission")

    async def _project(self, project_id: str, repository_id: str, components: list[str] | None = None) -> Data:
        async with self.store._unit():
            row = await ForgeProjectRepository().find_by_id(identifier(project_id))
            if row is None:
                raise KeyError("Project not found")
            project = copy.deepcopy(row.data)
        members = project["definition"]["components"]
        selected = [component for component in members if component["repository_id"] == repository_id]
        if not selected or components is not None and not set(components) <= {item["id"] for item in selected}:
            raise AccessDenied("Select explicit components in this project's connected repository")
        return project

    async def _record(self, event_id: str, project_id: str, repository_id: str) -> Data:
        async with self.store._unit() as session:
            size = await session.scalar(
                select(func.octet_length(cast(ForgeAccessEventEntity.data, Text))).where(
                    ForgeAccessEventEntity.id == identifier(event_id),
                )
            )
            if size is None:
                raise KeyError("SCM delegation receipt not found")
            if size > 65536:
                raise ValueError("SCM delegation receipt exceeds 64 KiB")
            row = await self.events.find_by_id(identifier(event_id))
            if (
                row is None
                or row.project_id.hex != project_id
                or set(row.data) != {"kind", "receipt", "summary"}
                or row.data["kind"] != EVENT_KIND
            ):
                raise ValueError("SCM delegation receipt scope is inconsistent")
            receipt = SCMActorBindingReceiptV1.model_validate(row.data["receipt"]).model_dump(mode="json")
            if row.data["summary"] != binding_summary(receipt):
                raise ValueError("SCM delegation summary does not match its immutable receipt")
        unsigned = {key: value for key, value in receipt.items() if key != "digest"}
        if (
            receipt["event_id"] != event_id
            or receipt["project_id"] != project_id
            or receipt["repository_id"] != repository_id
            or digest(unsigned) != receipt["digest"]
            or digest(receipt["request"]) != receipt["request_digest"]
            or binding_key(project_id, receipt["verified_reference"]) != receipt["binding_key"]
            or digest(receipt["verified_reference"]) != receipt["request"]["expected_verified_reference_digest"]
            or receipt["previous_digest"] != receipt["request"]["expected_previous_digest"]
            or receipt["project_binding_digest"] != receipt["request"]["expected_project_binding_digest"]
        ):
            raise ValueError("SCM delegation receipt binding is inconsistent")
        return receipt

    async def _existing(self, project_id: str, repository_id: str, request_id: str) -> Data | None:
        async with self.store._unit():
            row = await self.settings.find_for_repository(identifier(repository_id))
            state = authority_state(row.data if row else {})
        pointer = state["requests"].get(project_id + ":" + request_id)
        if pointer is None:
            return None
        if not isinstance(pointer, dict) or set(pointer) != {"event_id", "digest"}:
            raise ValueError("SCM delegation request pointer is invalid")
        receipt = await self._record(pointer["event_id"], project_id, repository_id)
        if receipt["digest"] != pointer["digest"] or receipt["request"]["request_id"] != request_id:
            raise ValueError("SCM delegation request pointer does not match its immutable receipt")
        return receipt

    async def _return(self, actor: Principal | None, receipt: Data, *, admin: bool) -> Data:
        if current_unit_of_work() is not None:
            raise ValueError("Read SCM delegation outside an active caller transaction")
        await self.store.admit_credentials(receipt)
        await self._reader(actor, receipt["project_id"], receipt["repository_id"], admin=admin)
        self.store.reject_credentials(receipt)
        return copy.deepcopy(receipt)

    async def get(self, project_id: str, repository_id: str, request_id: str) -> Data:
        actor = CURRENT_PRINCIPAL.get()
        await self._reader(actor, project_id, repository_id, admin=False)
        await self._project(project_id, repository_id)
        receipt = await self._existing(project_id, repository_id, request_id)
        if receipt is None:
            raise KeyError("SCM delegation request not found; absence is not permission to repeat a write")
        return await self._return(actor, receipt, admin=False)

    async def configure(self, project_id: str, repository_id: str, request: SCMActorBindingInputV1) -> Data:
        request = SCMActorBindingInputV1.model_validate(request.model_dump(mode="json", warnings=False))
        if current_unit_of_work() is not None:
            raise ValueError("Configure SCM delegation outside an active caller transaction")
        actor = CURRENT_PRINCIPAL.get()
        await self._reader(actor, project_id, repository_id, admin=True)
        assert actor is not None
        incoming = request.model_dump(mode="json")
        await self.store.admit_credentials(incoming)
        await self._reader(actor, project_id, repository_id, admin=True)
        existing = await self._existing(project_id, repository_id, request.request_id)
        if existing is not None:
            if existing["request"] != incoming or existing["administering_user_id"] != actor.id.hex:
                raise AccessDenied("SCM delegation request identity belongs to another actor or input")
            return await self._return(actor, existing, admin=True)
        project = await self._project(project_id, repository_id, request.component_ids)
        if digest(project) != request.expected_project_binding_digest:
            raise AccessDenied("The reviewed project component cut changed; review it before another explicit request")
        repository = await self.store.repository(repository_id)
        async with self.store._unit():
            delivery = await self.inbox.find_by_id(identifier(request.verified_receipt_id))
            if delivery is None or delivery.repository_id.hex != repository_id:
                raise AccessDenied("Select a verified delivery in this repository")
            data = copy.deepcopy(delivery.data)
        reference = VerifiedSCMReferenceV1.model_validate(data.get("verification")).model_dump(mode="json")
        if (
            reference["payload_digest"] != data["digest"]
            or digest(reference) != request.expected_verified_reference_digest
        ):
            raise ValueError("The verified delivery reference changed")
        if self.scm.delegation_scope(repository) != {
            key: reference[key] for key in self.scm.delegation_scope(repository)
        }:
            raise AccessDenied("The application connection changed; inspect a fresh signed delivery")
        grants = await self.scm.repositories(reference["provider"])
        if not any(
            str(item["id"]).strip("{}") == reference["remote_repository_id"] and item["url"] == repository["url"]
            for item in grants
        ):
            raise AccessDenied("The application no longer grants this repository")
        await self._reader(actor, project_id, repository_id, admin=True)
        await self.store.admit_credentials({"request": incoming, "reference": reference})
        await self._reader(actor, project_id, repository_id, admin=True)
        key = binding_key(project_id, reference)
        async with self.store._unit() as session:
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {
                    "key": self.store.schema + ":members:" + project_id,
                },
            )
            identities = ForgeIdentityUserRepository()
            for user_id in sorted({actor.id.hex, request.native_user_id}):
                identity = await identities.find_by_id(identifier(user_id), lock=LockMode.PESSIMISTIC_WRITE)
                if identity is None or not identity.enabled:
                    raise AccessDenied("Both the administrator and delegated user must be registered and enabled")
                if user_id == actor.id.hex and (identity.issuer, identity.subject) != (actor.issuer, actor.subject):
                    raise AccessDenied("The administering identity changed")
            project_row = await ForgeProjectRepository().find_by_id(
                identifier(project_id), lock=LockMode.PESSIMISTIC_WRITE
            )
            if project_row is None or digest(project_row.data) != request.expected_project_binding_digest:
                raise AccessDenied("The reviewed project component cut changed during admission")
            repo_row = await self.store.repository_records.find_by_id(
                identifier(repository_id), lock=LockMode.PESSIMISTIC_WRITE
            )
            if repo_row is None or await self.store.repository(repository_id) != repository:
                raise AccessDenied("Repository registration changed during admission")
            await self._reader(actor, project_id, repository_id, admin=True)
            if digest(await self._project(project_id, repository_id, request.component_ids)) != digest(project):
                raise AccessDenied("Project component scope changed during admission")
            current_scope = self.scm.delegation_scope(repository)
            if current_scope != {name: reference[name] for name in current_scope}:
                raise AccessDenied("Application connection changed during admission")
            if not self._key_current(repository_id, reference):
                raise AccessDenied("Webhook verification key changed during admission")
            row = await self.settings.find_for_repository(identifier(repository_id))
            state = authority_state(row.data if row else {})
            existing = await self._existing(project_id, repository_id, request.request_id)
            if existing is not None:
                if existing["request"] != incoming or existing["administering_user_id"] != actor.id.hex:
                    raise AccessDenied("SCM delegation request identity belongs to another actor or input")
                retained = existing
            else:
                head = state["heads"].get(key)
                previous = await self._head(project_id, repository_id, key) if head else None
                if head is None:
                    prior = await session.scalar(
                        select(ForgeAccessEventEntity.id)
                        .where(
                            ForgeAccessEventEntity.project_id == identifier(project_id),
                            ForgeAccessEventEntity.data["kind"].as_string() == EVENT_KIND,
                            ForgeAccessEventEntity.data["receipt"]["repository_id"].as_string() == repository_id,
                            ForgeAccessEventEntity.data["receipt"]["binding_key"].as_string() == key,
                        )
                        .limit(1)
                    )
                    if prior is not None:
                        raise ValueError("SCM delegation history has no consistent current head")
                if (previous["digest"] if previous else None) != request.expected_previous_digest:
                    raise ValueError("SCM delegation revision conflict; retain this request identity")
                if len(state["requests"]) >= 5000 or key not in state["heads"] and len(state["heads"]) >= 100:
                    raise ValueError("SCM delegation retention limit reached")
                revision = previous["revision"] + 1 if previous else 1
                if revision > 50:
                    raise ValueError("SCM delegation revision limit reached")
                retained = {
                    "version": 1,
                    "event_id": uuid4().hex,
                    "project_id": project_id,
                    "repository_id": repository_id,
                    "project_binding_digest": digest(project),
                    "binding_key": key,
                    "revision": revision,
                    "previous_digest": request.expected_previous_digest,
                    "prepared_at": now(),
                    "administering_user_id": actor.id.hex,
                    "request": incoming,
                    "request_digest": digest(incoming),
                    "verified_reference": reference,
                }
                retained["digest"] = digest(retained)
                SCMActorBindingReceiptV1.model_validate(retained)
                envelope = {"kind": EVENT_KIND, "receipt": retained, "summary": binding_summary(retained)}
                serialized = json.dumps(envelope, ensure_ascii=False).encode()
                if len(serialized) > 65536:
                    raise ValueError("SCM delegation receipt exceeds 64 KiB")
                envelope_bytes = await session.scalar(
                    select(func.octet_length(cast(literal(envelope, type_=JSONB), Text)))
                )
                if not isinstance(envelope_bytes, int) or envelope_bytes > 65536:
                    raise ValueError("SCM delegation receipt exceeds 64 KiB")
                total = await session.scalar(
                    select(
                        func.coalesce(func.sum(func.octet_length(cast(ForgeAccessEventEntity.data, Text))), 0)
                    ).where(
                        ForgeAccessEventEntity.data["kind"].as_string() == EVENT_KIND,
                        ForgeAccessEventEntity.data["receipt"]["repository_id"].as_string() == repository_id,
                    )
                )
                if int(total or 0) + envelope_bytes > MAX_BYTES:
                    raise ValueError("SCM delegation history exceeds 16 MiB")
                self.store.reject_credentials(retained)
                await self.events.save(
                    ForgeAccessEventEntity(
                        id=identifier(retained["event_id"]),
                        project_id=identifier(project_id),
                        data=envelope,
                    )
                )
                pointer = {"event_id": retained["event_id"], "digest": retained["digest"]}
                state["heads"][key] = pointer
                state["requests"][project_id + ":" + request.request_id] = pointer
                if row is None:
                    raise ValueError("Configure repository collaboration before delegating authority")
                row.data = {**row.data, AUTHORITY_KEY: state}
                await self.settings.save(row)
            self.store.reject_credentials(retained)
            await self._reader(actor, project_id, repository_id, admin=True)
            if self.scm.delegation_scope(repository) != current_scope or not self._key_current(
                repository_id, reference
            ):
                raise AccessDenied("SCM configuration changed before the mapping commit")
            self.store.reject_credentials(retained)
        # A refusal after this commit does not establish that no mapping was written.
        return await self._return(actor, retained, admin=True)

    async def candidates(self, project_id: str, repository_id: str) -> Data:
        from firefly_forge.collaboration_persistence import ForgeWebhookInboxEntity

        if current_unit_of_work() is not None:
            raise ValueError("Read SCM delegation outside an active caller transaction")
        actor = CURRENT_PRINCIPAL.get()
        await self._reader(actor, project_id, repository_id, admin=True)
        project = await self._project(project_id, repository_id)
        repository = await self.store.repository(repository_id)
        scope = self.scm.delegation_scope(repository)
        async with self.store._unit() as session:
            rows = (
                await session.execute(
                    select(
                        ForgeWebhookInboxEntity.id,
                        ForgeWebhookInboxEntity.data["verification"],
                        ForgeWebhookInboxEntity.data["digest"],
                    )
                    .where(
                        ForgeWebhookInboxEntity.repository_id == identifier(repository_id),
                        func.jsonb_typeof(ForgeWebhookInboxEntity.data["verification"]) == "object",
                        func.octet_length(cast(ForgeWebhookInboxEntity.data["verification"], Text)) <= 8192,
                    )
                    .order_by(ForgeWebhookInboxEntity.sequence.desc())
                    .limit(50)
                )
            ).all()
        items: list[Data] = []
        for event_id, value, payload_digest in rows:
            if not isinstance(event_id, UUID):
                raise ValueError("SCM candidate row identity is invalid")
            reference = VerifiedSCMReferenceV1.model_validate(value).model_dump(mode="json")
            if reference["payload_digest"] != payload_digest:
                raise ValueError("The candidate does not bind its retained delivery")
            if scope == {key: reference[key] for key in scope} and self._key_current(repository_id, reference):
                items.append(
                    {
                        "verified_receipt_id": event_id.hex,
                        "verified_reference": reference,
                        "verified_reference_digest": digest(reference),
                    }
                )
        result = {
            "version": 1,
            "project_id": project_id,
            "repository_id": repository_id,
            "project_binding_digest": digest(project),
            "items": items,
            "next_cursor": None,
        }
        await self.store.admit_credentials(result)
        await self._reader(actor, project_id, repository_id, admin=True)
        if digest(await self._project(project_id, repository_id)) != digest(project):
            raise AccessDenied("Project component scope changed before returning candidates")
        current_repository = await self.store.repository(repository_id)
        if current_repository != repository or self.scm.delegation_scope(current_repository) != scope:
            raise AccessDenied("SCM candidate scope changed before return")
        if any(not self._key_current(repository_id, item["verified_reference"]) for item in items):
            raise AccessDenied("The webhook key changed before return")
        self.store.reject_credentials(result)
        return result

    async def _current_heads(self, project_id: str, repository_id: str) -> Data:
        event = ForgeAccessEventEntity
        key = event.data["receipt"]["binding_key"].as_string()
        revision = event.data["receipt"]["revision"].as_integer()
        async with self.store._unit() as session:
            settings = await self.settings.find_for_repository(identifier(repository_id))
            state = authority_state(settings.data if settings else {})
            rows = (
                await session.execute(
                    select(
                        key,
                        event.id,
                        revision,
                        event.data["receipt"]["digest"].as_string(),
                    )
                    .where(
                        event.project_id == identifier(project_id),
                        event.data["kind"].as_string() == EVENT_KIND,
                        event.data["receipt"]["repository_id"].as_string() == repository_id,
                    )
                    .distinct(key)
                    .order_by(key, revision.desc())
                    .limit(101)
                )
            ).all()
        if len(rows) > 100:
            raise ValueError("SCM current-head count exceeds its bounded envelope")
        result: Data = {}
        for binding, event_id, number, receipt_digest in rows:
            if (
                not isinstance(binding, str)
                or not isinstance(event_id, UUID)
                or type(number) is not int
                or not 1 <= number <= 50
            ):
                raise ValueError("SCM current-head metadata is inconsistent")
            pointer = {"event_id": event_id.hex, "digest": receipt_digest}
            if state["heads"].get(binding) != pointer:
                raise ValueError("SCM current-head index does not match immutable history")
            result[binding] = {**pointer, "revision": number}
        return result

    async def list(self, project_id: str, repository_id: str, cursor: str | None = None) -> Data:
        import re

        if current_unit_of_work() is not None:
            raise ValueError("Read SCM delegation outside an active caller transaction")
        if cursor is not None and not re.fullmatch(r"[a-f0-9]{32}", cursor):
            raise ValueError("Use an exact current-head cursor")
        actor = CURRENT_PRINCIPAL.get()
        await self._reader(actor, project_id, repository_id, admin=False)
        project = await self._project(project_id, repository_id)
        heads = await self._current_heads(project_id, repository_id)
        ordered = sorted(value["event_id"] for value in heads.values() if cursor is None or value["event_id"] > cursor)
        selected = ordered[:51]
        async with self.store._unit() as session:
            sizes = (
                await session.execute(
                    select(
                        func.octet_length(cast(ForgeAccessEventEntity.data["summary"], Text)),
                        func.octet_length(cast(ForgeAccessEventEntity.data["receipt"]["verified_reference"], Text)),
                    ).where(ForgeAccessEventEntity.id.in_([identifier(value) for value in selected]))
                )
            ).all()
        if (
            len(sizes) != len(selected)
            or any(
                type(summary_size) is not int
                or not 0 < summary_size <= 16384
                or type(reference_size) is not int
                or not 0 < reference_size <= 8192
                for summary_size, reference_size in sizes
            )
            or sum(
                summary_size + reference_size
                for summary_size, reference_size in sizes
                if isinstance(summary_size, int) and isinstance(reference_size, int)
            )
            > MAX_BYTES
        ):
            raise ValueError("SCM current-head page exceeds its bounded envelope")
        event = ForgeAccessEventEntity
        receipt = event.data["receipt"]
        projection = {
            "version": 1,
            **{
                name: receipt[name]
                for name in (
                    "event_id",
                    "project_id",
                    "repository_id",
                    "project_binding_digest",
                    "binding_key",
                    "revision",
                    "digest",
                    "prepared_at",
                )
            },
            **{
                name: receipt["request"][name]
                for name in ("request_id", "native_user_id", "component_ids", "allowed_actions", "active")
            },
            **{name: receipt["verified_reference"][name] for name in ("provider", "external_actor_id")},
        }
        expression = func.jsonb_build_object(*[part for item in projection.items() for part in item])
        async with self.store._unit() as session:
            values = (
                await session.execute(
                    select(
                        event.id,
                        event.project_id,
                        event.data["summary"],
                        expression,
                        receipt["verified_reference"],
                    )
                    .where(event.id.in_([identifier(value) for value in selected]))
                    .order_by(event.id)
                )
            ).all()
        if len(values) != len(selected):
            raise ValueError("SCM current-head page changed during its bounded read")
        repository = await self.store.repository(repository_id)
        try:
            scope = self.scm.delegation_scope(repository)
        except ValueError:
            scope = None
        items: list[Data] = []
        key_cuts: list[tuple[Data, bool]] = []
        for event_id, scoped_project, raw, expected, verification in values:
            if not isinstance(event_id, UUID) or not isinstance(scoped_project, UUID):
                raise ValueError("SCM current-head row identity is invalid")
            summary = SCMActorBindingSummaryV1.model_validate(raw).model_dump(mode="json")
            unsigned = {key: value for key, value in summary.items() if key != "summary_digest"}
            reference = VerifiedSCMReferenceV1.model_validate(verification).model_dump(mode="json")
            pointer = heads.get(summary["binding_key"])
            key_cuts.append((reference, self._key_current(repository_id, reference)))
            if (
                unsigned != expected
                or summary["summary_digest"] != digest(unsigned)
                or summary["event_id"] != event_id.hex
                or summary["project_id"] != scoped_project.hex
                or scoped_project.hex != project_id
                or summary["repository_id"] != repository_id
                or pointer != {"event_id": event_id.hex, "digest": summary["digest"], "revision": summary["revision"]}
            ):
                raise ValueError("SCM current-head summary binding is inconsistent")
            status = "active"
            if not summary["active"]:
                status = "revoked"
            elif summary["project_binding_digest"] != digest(project):
                status = "stale_project"
            elif (
                scope is None
                or scope != {key: reference[key] for key in scope}
                or not self._key_current(repository_id, reference)
            ):
                status = "stale_connection"
            items.append({**summary, "current_head": True, "head_status": status})
        result = {
            "version": 1,
            "project_id": project_id,
            "repository_id": repository_id,
            "project_binding_digest": digest(project),
            "heads_only": True,
            "items": items[:50],
            "next_cursor": selected[49] if len(selected) == 51 else None,
        }
        await self.store.admit_credentials(result)
        await self._reader(actor, project_id, repository_id, admin=False)
        if await self._current_heads(project_id, repository_id) != heads or digest(
            await self._project(project_id, repository_id)
        ) != digest(project):
            raise AccessDenied("SCM current-head scope changed before return")
        current_repository = await self.store.repository(repository_id)
        try:
            current_scope = self.scm.delegation_scope(current_repository)
        except ValueError:
            current_scope = None
        if (
            repository != current_repository
            or current_scope != scope
            or any(self._key_current(repository_id, reference) != was_current for reference, was_current in key_cuts)
        ):
            raise AccessDenied("SCM connection changed before returning current heads")
        self.store.reject_credentials(result)
        return result

    def _key_revision(self, repository_id: str) -> str | None:
        value = self.store.configuration._read(self.store.configuration.directory / "collaboration-secrets.json").get(
            repository_id
        )
        return value.get("revision") if isinstance(value, dict) else None

    def _key_current(self, repository_id: str, reference: Data) -> bool:
        if self._key_revision(repository_id) == reference["webhook_key_revision"]:
            return True
        private = self.store.configuration
        global_key = private._read(private.directory / "channel-webhook-secrets.json").get(reference["provider"])
        settings = private._read(private.directory / "channel-bootstrap.json").get(reference["provider"], {})
        return bool(
            settings.get("enabled")
            and isinstance(global_key, dict)
            and global_key.get("revision") == reference["webhook_key_revision"]
        )

    async def _head(self, project_id: str, repository_id: str, key: str) -> Data:
        async with self.store._unit() as session:
            row = await self.settings.find_for_repository(identifier(repository_id))
            state = authority_state(row.data if row else {})
            pointer = state["heads"].get(key)
            if not isinstance(pointer, dict) or set(pointer) != {"event_id", "digest"}:
                raise AccessDenied("Configure an explicit SCM delegation for this project and account")
            latest = await session.scalar(
                select(ForgeAccessEventEntity.id)
                .where(
                    ForgeAccessEventEntity.project_id == identifier(project_id),
                    ForgeAccessEventEntity.data["kind"].as_string() == EVENT_KIND,
                    ForgeAccessEventEntity.data["receipt"]["repository_id"].as_string() == repository_id,
                    ForgeAccessEventEntity.data["receipt"]["binding_key"].as_string() == key,
                )
                .order_by(ForgeAccessEventEntity.data["receipt"]["revision"].as_integer().desc())
                .limit(1)
            )
            if latest is None or latest.hex != pointer["event_id"]:
                raise AccessDenied("SCM delegation head does not match immutable history")
        receipt = await self._record(pointer["event_id"], project_id, repository_id)
        if receipt["digest"] != pointer["digest"] or receipt["binding_key"] != key:
            raise AccessDenied("SCM delegation head is inconsistent")
        return receipt

    async def authorize_snapshot(self, actor: Data, project_id: str | None, permission: Permission) -> None:
        from firefly_forge.access_persistence import ForgeMembershipRepository

        context = actor.get("scm_delegation")
        if isinstance(context, dict) and context.get("transport") == "github_rest_poll_v1":
            polling = getattr(self.store, "scm_polling", None)
            if polling is None:
                raise AccessDenied("The native polling authority is unavailable")
            await polling.authorize_snapshot(actor, project_id, permission)
            return
        if not isinstance(context, dict) or set(context) != {
            "project_id",
            "repository_id",
            "component_id",
            "run_id",
            "binding_key",
            "binding_digest",
            "binding_request_id",
            "verified_receipt_id",
            "verified_reference",
            "action",
        }:
            raise AccessDenied("SCM delegation context is missing or invalid")
        if (
            actor.get("global_admin") is not False
            or actor.get("api_audience") is not None
            or context["project_id"] != project_id
        ):
            raise AccessDenied("SCM delegation cannot confer global or foreign-project authority")
        if (
            context["action"] not in {"respond", "status", "cancel"}
            or permission not in {"read", "execute", "release"}
            or context["action"] == "status"
            and permission != "read"
        ):
            raise AccessDenied("This action requires a separately qualified SCM delegation adapter")
        project = await self._project(context["project_id"], context["repository_id"], [context["component_id"]])
        receipt = await self._head(context["project_id"], context["repository_id"], context["binding_key"])
        request = receipt["request"]
        reference = VerifiedSCMReferenceV1.model_validate(context["verified_reference"]).model_dump(mode="json")
        if {key: value for key, value in reference.items() if key != "payload_digest"} != {
            key: value for key, value in receipt["verified_reference"].items() if key != "payload_digest"
        }:
            raise AccessDenied("The channel verification cut changed; review a new mapping revision")
        if (
            not request["active"]
            or request["native_user_id"] != actor["user_id"]
            or receipt["digest"] != context["binding_digest"]
            or request["request_id"] != context["binding_request_id"]
            or context["component_id"] not in request["component_ids"]
            or context["action"] not in request["allowed_actions"]
            or receipt["project_binding_digest"] != digest(project)
            or binding_key(context["project_id"], reference) != context["binding_key"]
        ):
            raise AccessDenied("SCM delegation is revoked, changed or outside its reviewed scope")
        async with self.store._unit():
            delivery = await self.inbox.find_by_id(identifier(context["verified_receipt_id"]))
            if delivery is None or delivery.repository_id.hex != context["repository_id"]:
                raise AccessDenied("The verified SCM delivery is unavailable")
            data = copy.deepcopy(delivery.data)
            settings_row = await self.settings.find_for_repository(identifier(context["repository_id"]))
            settings = copy.deepcopy(settings_row.data) if settings_row else {}
            if (
                not settings.get("enabled")
                or data["actor"] not in settings.get("trusted_actors", [])
                or context["action"]
                not in [*settings.get("allowed_commands", []), *settings.get("channel_commands", [])]
            ):
                raise AccessDenied("The collaboration actor or command is no longer enabled")
            if (
                data.get("verification") != reference
                or data["digest"] != reference["payload_digest"]
                or (data.get("command") or {}).get("run_id") != context["run_id"]
                or (data.get("command") or {}).get("action") != context["action"]
            ):
                raise AccessDenied("The SCM delivery no longer binds this action")
            identity = await ForgeIdentityUserRepository().find_by_id(identifier(actor["user_id"]))
            if identity is None or not identity.enabled:
                raise AccessDenied("The delegated native user is unavailable or suspended")
            memberships = await ForgeMembershipRepository().for_user(identity.id)
            if not any(
                item.project_id.hex == project_id and self.access.permits(item.role, permission) for item in memberships
            ):
                raise AccessDenied("The delegated user no longer has the required project permission")
            run = await self.store.get_run(context["run_id"])
            if (run.get("project_id"), run["repository_id"], run.get("component_id")) != (
                context["project_id"],
                context["repository_id"],
                context["component_id"],
            ):
                raise AccessDenied("The delegated run scope changed")
        repository = await self.store.repository(context["repository_id"])
        scope = self.scm.delegation_scope(repository)
        if data.get("global_ingress"):
            channel = self.store.configuration._read(self.store.configuration.directory / "channel-bootstrap.json").get(
                reference["provider"], {}
            )
            if not channel.get("enabled") or data["actor"] not in channel.get("trusted_actors", []):
                raise AccessDenied("The signed ingress actor is no longer enabled")
        if scope != {key: reference[key] for key in scope} or not self._key_current(repository["id"], reference):
            raise AccessDenied("The SCM connection or webhook key changed")
        self.store.reject_credentials({"actor": actor, "receipt": receipt})

    async def resolve(self, delivery: Data) -> Principal:
        if (delivery.get("verification") or {}).get("transport") == "github_rest_poll_v1":
            polling = getattr(self.store, "scm_polling", None)
            if polling is None:
                raise AccessDenied("The native polling authority is unavailable")
            resolved = await polling.resolve(delivery)
            if not isinstance(resolved, Principal):
                raise AccessDenied("The polling authority returned an invalid principal")
            return resolved
        command = delivery.get("command") or {}
        if command.get("action") not in {"respond", "status", "cancel"} or not command.get("run_id"):
            raise AccessDenied("This SCM action requires explicit project/component setup and a qualified adapter")
        reference = VerifiedSCMReferenceV1.model_validate(delivery.get("verification")).model_dump(mode="json")
        run = await self.store.get_run(command["run_id"])
        if (
            not run.get("project_id")
            or not run.get("component_id")
            or run["repository_id"] != delivery["repository_id"]
        ):
            raise AccessDenied("An SCM action must target a run with an explicit project and component")
        key = binding_key(run["project_id"], reference)
        receipt = await self._head(run["project_id"], delivery["repository_id"], key)
        context = {
            "project_id": run["project_id"],
            "repository_id": delivery["repository_id"],
            "component_id": run["component_id"],
            "run_id": run["id"],
            "binding_key": key,
            "binding_digest": receipt["digest"],
            "binding_request_id": receipt["request"]["request_id"],
            "verified_receipt_id": delivery["id"],
            "verified_reference": reference,
            "action": command["action"],
        }
        async with self.store._unit():
            identity = await ForgeIdentityUserRepository().find_by_id(identifier(receipt["request"]["native_user_id"]))
            if identity is None or not identity.enabled:
                raise AccessDenied("The delegated native user is unavailable or suspended")
            actor = Principal(
                identity.id,
                identity.issuer,
                identity.subject,
                authentication_method="scm_mapping",
                scm_delegation=context,
            )
        snapshot = self.access.actor_snapshot(actor)
        permission: Permission = "read" if command["action"] == "status" else "execute"
        await self.authorize_snapshot(snapshot, run["project_id"], permission)
        repository = await self.store.repository(delivery["repository_id"])
        grants = await self.scm.repositories(reference["provider"])
        if not any(
            str(item["id"]).strip("{}") == reference["remote_repository_id"] and item["url"] == repository["url"]
            for item in grants
        ):
            raise AccessDenied("The application no longer grants this repository")
        await self.store.admit_credentials({"actor": snapshot, "receipt": receipt})
        await self.authorize_snapshot(snapshot, run["project_id"], permission)
        if run.get("publish") and permission == "execute":
            await self.authorize_snapshot(snapshot, run["project_id"], "release")
        return actor
