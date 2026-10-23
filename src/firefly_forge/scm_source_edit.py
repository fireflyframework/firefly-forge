# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""One administrator-sealed source-only trigger through API-observed GitHub polling."""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from pyfly.data.relational.sqlalchemy import LockMode
from pyfly.data.transaction import current_unit_of_work
from sqlalchemy import Text, cast, func, select, text

from firefly_forge.access_persistence import ForgeAccessEventEntity
from firefly_forge.identity import Principal
from firefly_forge.identity_persistence import ForgeIdentityUserRepository
from firefly_forge.journal import digest
from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied, ProjectAccess
from firefly_forge.project_persistence import ForgeProjectRepository
from firefly_forge.scm_poll_models import GitHubPollReferenceV1
from firefly_forge.scm_polling import BINDING_KIND, poll_key
from firefly_forge.scm_resume_service import grant_state
from firefly_forge.scm_source_edit_models import (
    SCMSourceEditCommandV1,
    SCMSourceEditGrantInputV1,
    SCMSourceEditGrantReceiptV1,
    SealedSourceEditAdmission,
)
from firefly_forge.store import identifier, now

Data = dict[str, Any]
USED_KEY = "_scm_source_edit_used_v1"


def utc_now() -> datetime:
    return datetime.now(UTC)


def validate_expiration(request: Data, *, admission: bool = True) -> None:
    expiry = datetime.fromisoformat(request["expires_at"])
    if expiry.utcoffset() is None or expiry <= utc_now():
        raise AccessDenied("The separate source-only consent expired")
    if admission and expiry > utc_now() + timedelta(minutes=60):
        raise ValueError("Source-only consent may last at most 60 minutes")


def source_state(data: Data) -> Data:
    used = copy.deepcopy(data.get(USED_KEY, {}))
    if (
        not isinstance(used, dict)
        or len(used) > 5000
        or any(
            not isinstance(k, str) or len(k) != 64 or v is not None and (not isinstance(v, str) or len(v) != 32)
            for k, v in used.items()
        )
    ):
        raise AccessDenied("The durable source-only trigger ledger is invalid or full")
    return used


class SCMSourceEditAuthority:
    state_key = "_scm_poll_source_edit_v1"
    event_kind = "scm_poll_source_edit_grant_v1"
    grant_version = 1
    request_type = SCMSourceEditGrantInputV1
    receipt_type = SCMSourceEditGrantReceiptV1
    actor_transport = "github_rest_poll_source_edit_v1"

    def __init__(self, store: Any, polling: Any, workspaces: Any) -> None:
        self.store, self.polling, self.workspaces = store, polling, workspaces
        self.authority = polling.authority
        self.settings, self.inbox, self.events = polling.settings, polling.inbox, polling.events

    async def _record(self, event_id: str, project: str, repository: str) -> Data:
        async with self.store._unit() as session:
            size = await session.scalar(
                select(func.octet_length(cast(ForgeAccessEventEntity.data, Text))).where(
                    ForgeAccessEventEntity.id == identifier(event_id)
                )
            )
            if size is None:
                raise KeyError("Source-only grant not found")
            if size > 65536:
                raise ValueError("Source-only grant exceeds 64 KiB")
            row = await self.events.find_by_id(identifier(event_id))
            if (
                row is None
                or row.project_id.hex != project
                or set(row.data) != {"kind", "receipt"}
                or row.data["kind"] != self.event_kind
            ):
                raise ValueError("Source-only grant event scope is invalid")
            receipt = self.receipt_type.model_validate(row.data["receipt"]).model_dump(mode="json")
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
            "admin_snapshot",
            "request",
            "request_digest",
            "digest",
        }:
            raise ValueError("Source-only grant envelope is invalid")
        request = self.request_type.model_validate(receipt["request"]).model_dump(mode="json")
        if (
            receipt["version"] != self.grant_version
            or type(receipt["version"]) is not int
            or receipt["event_id"] != event_id
            or receipt["project_id"] != project
            or receipt["repository_id"] != repository
            or receipt["admin_snapshot"].get("user_id") != receipt["administering_user_id"]
            or receipt["admin_snapshot"].get("authentication_method") == "scm_mapping"
            or request != receipt["request"]
            or digest(request) != receipt["request_digest"]
            or receipt["previous_digest"] != request["expected_previous_digest"]
            or digest({k: v for k, v in receipt.items() if k != "digest"}) != receipt["digest"]
            or type(receipt["revision"]) is not int
            or not 1 <= receipt["revision"] <= 50
        ):
            raise ValueError("Source-only grant immutable binding is invalid")
        return receipt

    async def _grant(
        self, project: str, repository: str, *, key: str | None = None, request_id: str | None = None
    ) -> Data | None:
        async with self.store._unit() as session:
            row = await self.settings.find_for_repository(identifier(repository))
            state = grant_state(row.data if row else {}, self.state_key, self.grant_version)
            pointer = state["heads"].get(key) if key else state["requests"].get(project + ":" + str(request_id))
            latest = None
            if key:
                latest = await session.scalar(
                    select(ForgeAccessEventEntity.id)
                    .where(
                        ForgeAccessEventEntity.project_id == identifier(project),
                        ForgeAccessEventEntity.data["kind"].as_string() == self.event_kind,
                        ForgeAccessEventEntity.data["receipt"]["repository_id"].as_string() == repository,
                        ForgeAccessEventEntity.data["receipt"]["binding_key"].as_string() == key,
                    )
                    .order_by(ForgeAccessEventEntity.data["receipt"]["revision"].as_integer().desc())
                    .limit(1)
                )
        if pointer is None:
            if latest is not None:
                raise ValueError("Source-only history has no consistent current head")
            return None
        if not isinstance(pointer, dict) or set(pointer) != {"event_id", "digest"}:
            raise ValueError("Source-only grant pointer is invalid")
        retained = await self._record(pointer["event_id"], project, repository)
        if (
            retained["digest"] != pointer["digest"]
            or key
            and (latest is None or latest.hex != retained["event_id"] or retained["binding_key"] != key)
            or request_id
            and retained["request"]["request_id"] != request_id
        ):
            raise ValueError("Source-only grant pointer does not bind immutable history")
        return retained

    async def _return(self, actor: Any, receipt: Data, *, admin: bool) -> Data:
        await self.store.admit_credentials(receipt)
        await self.authority._reader(actor, receipt["project_id"], receipt["repository_id"], admin=admin)
        self.store.reject_credentials(receipt)
        return copy.deepcopy(receipt)

    async def get(self, project: str, repository: str, request_id: str) -> Data:
        if current_unit_of_work() is not None:
            raise ValueError("Read source-only grants outside a caller transaction")
        actor = CURRENT_PRINCIPAL.get()
        await self.authority._reader(actor, project, repository, admin=False)
        receipt = await self._grant(project, repository, request_id=request_id)
        if receipt is None:
            raise KeyError("Source-only grant request not found; absence never permits another write")
        return await self._return(actor, receipt, admin=False)

    async def current(self, project: str, repository: str, binding_request: str) -> Data:
        if current_unit_of_work() is not None:
            raise ValueError("Read source-only grants outside a caller transaction")
        actor = CURRENT_PRINCIPAL.get()
        await self.authority._reader(actor, project, repository, admin=False)
        binding = await self._current_binding(project, repository, binding_request)
        if binding is None:
            raise KeyError("Polling V2 binding not found")
        receipt = await self._grant(project, repository, key=binding["binding_key"])
        if receipt is None:
            raise KeyError("No separately consented source-only grant exists")
        return await self._return(actor, receipt, admin=False)

    async def configure(self, project: str, repository: str, request: SCMSourceEditGrantInputV1) -> Data:
        if current_unit_of_work() is not None:
            raise ValueError("Admit source-only consent outside a caller transaction")
        request = self.request_type.model_validate(request.model_dump(mode="json"))
        actor = CURRENT_PRINCIPAL.get()
        await self.authority._reader(actor, project, repository, admin=True)
        assert actor is not None
        incoming = request.model_dump(mode="json")
        validate_expiration(incoming)
        await self.store.admit_credentials(incoming)
        await self.authority._reader(actor, project, repository, admin=True)
        retained = await self._grant(project, repository, request_id=request.request_id)
        if retained is not None:
            if retained["request"] != incoming or retained["administering_user_id"] != actor.id.hex:
                raise AccessDenied("The retained request belongs to another input or administrator")
            return await self._return(actor, retained, admin=True)
        binding = await self._binding(project, repository, request)
        if request.active:
            candidate = await self.workspaces.source_candidate(request.run_id)
            snapshot = {
                **ProjectAccess.actor_snapshot(actor),
                "identity_digest": digest({"issuer": actor.issuer, "subject": actor.subject}),
            }
            if (
                candidate["digest"] != request.activation.candidate_digest
                or candidate["actor"] != snapshot
                or candidate["run_id"] != request.run_id
                or digest(candidate["source_edit"]["qualification"]) != request.expected_source_profile_digest
            ):
                raise AccessDenied("The reviewed source candidate, administrator or profile changed")
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
                    raise AccessDenied("Configure collaboration before consenting to source-only")
                state = grant_state(row.data, self.state_key, self.grant_version)
                previous = await self._grant(project, repository, key=binding["binding_key"])
                if request.expected_previous_digest != (previous["digest"] if previous else None):
                    raise AccessDenied("Source-only grant revision changed; preserve the reviewed request")
                revision = previous["revision"] + 1 if previous else 1
                if (
                    revision > 50
                    or len(state["requests"]) >= 5000
                    or binding["binding_key"] not in state["heads"]
                    and len(state["heads"]) >= 100
                ):
                    raise ValueError("Source-only grant retention limit reached")
                retained = dict(
                    version=self.grant_version,
                    event_id=uuid4().hex,
                    project_id=project,
                    repository_id=repository,
                    binding_key=binding["binding_key"],
                    native_user_id=binding["request"]["native_user_id"],
                    revision=revision,
                    previous_digest=request.expected_previous_digest,
                    prepared_at=now(),
                    administering_user_id=actor.id.hex,
                    admin_snapshot={
                        **ProjectAccess.actor_snapshot(actor),
                        "identity_digest": digest({"issuer": actor.issuer, "subject": actor.subject}),
                    },
                    request=incoming,
                    request_digest=digest(incoming),
                )
                retained["digest"] = digest(retained)
                self.receipt_type.model_validate(retained)
                envelope = {"kind": self.event_kind, "receipt": retained}
                total = await session.scalar(
                    select(
                        func.coalesce(func.sum(func.octet_length(cast(ForgeAccessEventEntity.data, Text))), 0)
                    ).where(
                        ForgeAccessEventEntity.data["kind"].as_string() == self.event_kind,
                        ForgeAccessEventEntity.data["receipt"]["repository_id"].as_string() == repository,
                    )
                )
                if (
                    len(json.dumps(envelope, ensure_ascii=False).encode()) > 65536
                    or int(total or 0) + len(json.dumps(envelope, ensure_ascii=False).encode()) > 16 * 1024 * 1024
                ):
                    raise ValueError("Source-only grant history exceeds its byte budget")
                self.store.reject_credentials(retained)
                await self.events.save(
                    ForgeAccessEventEntity(
                        id=identifier(retained["event_id"]), project_id=identifier(project), data=envelope
                    )
                )
                pointer = {"event_id": retained["event_id"], "digest": retained["digest"]}
                state["heads"][binding["binding_key"]] = pointer
                state["requests"][project + ":" + request.request_id] = pointer
                used = source_state(row.data)
                if retained["digest"] in used:
                    raise AccessDenied("The source grant identity was already reserved")
                used[retained["digest"]] = None
                row.data = {**row.data, self.state_key: state, USED_KEY: used}
                await self.settings.save(row)
        return await self._return(actor, retained, admin=True)

    async def _current_binding(self, project: str, repository: str, request_id: str) -> Data | None:
        async with self.store._unit():
            _, state = await self.polling._state(repository)
        pointer = state["requests"].get(project + ":" + request_id)
        return await self.polling._event(pointer, BINDING_KIND, project, repository) if pointer else None

    async def _binding(self, project: str, repository: str, request: SCMSourceEditGrantInputV1) -> Data:
        binding = await self._current_binding(project, repository, request.expected_binding_request_id)
        if binding is None:
            raise AccessDenied("Select an exact current API-observed polling binding")
        policy, state = await self.polling._policy(repository)
        await self.polling._scope(repository, policy)
        project_cut = await self.authority._project(project, repository, request.component_ids)
        observed = await self.polling._observation(project, repository, binding["request"]["observed_receipt_id"])
        run = await self.store.get_run(request.run_id)
        if (
            await self.polling._head(project, repository, binding["binding_key"]) != binding
            or not binding["request"]["active"]
            or policy["project_id"] != project
            or not state["cursor"]["ready"]
            or binding["digest"] != request.expected_binding_digest
            or not set(request.component_ids) <= set(binding["request"]["component_ids"])
            or digest(project_cut) != request.expected_project_binding_digest
            or binding["project_binding_digest"] != digest(project_cut)
            or observed.get("source_conflict")
            or observed["verification"]["issue_number"] != request.issue_number
            or any(binding["verified_reference"].get(k) != v for k, v in policy["scope"].items())
            or run.get("repository_id") != repository
            or run.get("project_id") != project
            or run.get("component_id") not in request.component_ids
        ):
            raise AccessDenied("The source-only binding, issue, run or project cut changed")
        return binding

    async def _preflight(self, context: Data) -> None:
        binding = context["binding"]
        delivery = context.get("delivery") or await self.polling._observation(
            binding["project_id"], binding["repository_id"], binding["request"]["observed_receipt_id"]
        )
        await self.polling._fresh(delivery, await self.store.repository(binding["repository_id"]))

    async def _context(self, delivery: Data, *, remote: bool = False) -> Data:
        command = SCMSourceEditCommandV1.model_validate(delivery.get("command"))
        from firefly_forge.collaboration import RepositoryCommand

        parsed = RepositoryCommand.parse(delivery["source_snapshot"]["body"])
        if parsed.model_dump(exclude_none=True) != command.model_dump(exclude_none=True):
            raise AccessDenied("The retained source-only command differs from its observed source")
        repository = delivery["repository_id"]
        run = await self.store.get_run(command.run_id)
        project = run.get("project_id")
        if not project:
            raise AccessDenied("The existing source-only run requires its native project")
        reference = GitHubPollReferenceV1.model_validate(delivery.get("verification")).model_dump(mode="json")
        if (
            await self.polling._observation(project, repository, delivery["id"]) != delivery
            or delivery.get("source_conflict")
            or delivery.get("actor") != reference["external_actor_id"]
            or delivery.get("digest") != reference["payload_digest"]
            or delivery.get("remote_id") != reference["remote_repository_id"]
            or delivery.get("issue") != {"kind": "issue", "number": reference["issue_number"]}
        ):
            raise AccessDenied("The API-observed source-only trigger or actor changed")
        grant = await self._grant(project, repository, key=poll_key(project, reference))
        if grant is None or grant["digest"] != command.expected_grant_digest:
            raise AccessDenied("A separate exact source-only administrator consent is required")
        request = self.request_type.model_validate(grant["request"])
        validate_expiration(grant["request"], admission=False)
        binding = await self._binding(project, repository, request)
        if (
            not request.active
            or command.run_id != request.run_id
            or reference["issue_number"] != request.issue_number
            or grant["native_user_id"] != binding["request"]["native_user_id"]
            or datetime.fromisoformat(reference["observed_at"]) < datetime.fromisoformat(grant["prepared_at"])
            or datetime.fromisoformat(delivery["created_at"]) < datetime.fromisoformat(grant["prepared_at"])
            or any(
                reference[k] != binding["verified_reference"][k]
                for k in (
                    "provider",
                    "api_origin",
                    "installation_id",
                    "remote_repository_id",
                    "external_actor_id",
                    "connection_revision",
                    "connection_scope_digest",
                    "transport",
                )
            )
        ):
            raise AccessDenied("The source-only consent does not bind this issue, run or account")
        from firefly_forge.source_edit_continuations import _assert_source_administrator

        await _assert_source_administrator(self.store, grant["admin_snapshot"])
        async with self.store._unit():
            user = await self.polling.users.find_by_id(
                identifier(grant["native_user_id"]), lock=LockMode.PESSIMISTIC_WRITE
            )
            memberships = await self.polling.memberships.for_user(identifier(grant["native_user_id"]))
            row, _ = await self.polling._state(repository)
            if (
                user is None
                or not user.enabled
                or not row.data.get("enabled")
                or delivery["actor"] not in row.data.get("trusted_actors", [])
                or not any(
                    m.project_id.hex == project and m.active and ProjectAccess.permits(m.role, "execute")
                    for m in memberships
                )
            ):
                raise AccessDenied("Current mapped native execution authority is required")
            identity = (user.id, user.issuer, user.subject)
        await self.store.assert_ownership()
        async with self.store._unit():
            user = await self.polling.users.find_by_id(
                identifier(grant["native_user_id"]), lock=LockMode.PESSIMISTIC_WRITE
            )
            memberships = await self.polling.memberships.for_user(identifier(grant["native_user_id"]))
            row, _ = await self.polling._state(repository)
            if (
                user is None
                or not user.enabled
                or (user.id, user.issuer, user.subject) != identity
                or not row.data.get("enabled")
                or delivery["actor"] not in row.data.get("trusted_actors", [])
                or not any(
                    m.project_id.hex == project and m.active and ProjectAccess.permits(m.role, "execute")
                    for m in memberships
                )
            ):
                raise AccessDenied("The mapped source identity or current execution authority changed during admission")
        context = dict(grant=grant, request=request, binding=binding, user=user, project=project, delivery=delivery)
        if remote:
            await self._preflight(context)
        return context

    async def process(self, receipt_id: str) -> Data:
        collaboration = self.polling.collaboration
        delivery = await collaboration.receipt(receipt_id)
        if delivery["status"] != "received":
            return copy.deepcopy(delivery)
        context = await self._context(delivery, remote=True)
        grant, request, user = context["grant"], context["request"], context["user"]
        async with self.polling._locked(context["project"], delivery["repository_id"], [user.id.hex]):
            current = await self._context(await collaboration.receipt(receipt_id))
            if current["grant"] != grant or current["binding"] != context["binding"]:
                raise AccessDenied("The reviewed source-only admission changed")
            row = await self.inbox.find_by_id(identifier(receipt_id), lock=LockMode.PESSIMISTIC_WRITE)
            assert row is not None
            if row.status != "received":
                return copy.deepcopy(row.data)
            settings, _ = await self.polling._state(delivery["repository_id"])
            used = source_state(settings.data)
            if grant["digest"] not in used:
                raise AccessDenied("The durable source-only reservation ledger is missing")
            if used[grant["digest"]] is not None:
                raise AccessDenied("The source-only trigger is already reserved")
            used[grant["digest"]] = receipt_id
            settings.data = {**settings.data, USED_KEY: used}
            await self.settings.save(settings)
            evidence = dict(
                version=1,
                transport=self.actor_transport,
                receipt_id=receipt_id,
                grant_digest=grant["digest"],
                binding_digest=context["binding"]["digest"],
                verified_reference_digest=digest(delivery["verification"]),
                native_user_id=user.id.hex,
            )
            await collaboration._write_row(
                row,
                status="needs_reconciliation",
                action_status="writing",
                reason="source_only_activation_pending",
                source_edit_authority=evidence,
            )
            admitted = copy.deepcopy(row.data)
        caller = Principal(
            user.id, user.issuer, user.subject, authentication_method="scm_mapping", scm_delegation=evidence
        )
        token = CURRENT_PRINCIPAL.set(caller)

        async def verify() -> None:
            if CURRENT_PRINCIPAL.get() != caller:
                raise AccessDenied("The source-only caller changed")
            remote = current_unit_of_work() is None
            if remote:
                await self._context(await collaboration.receipt(receipt_id), remote=True)
            async with self.store._unit():
                row = await self.inbox.find_by_id(identifier(receipt_id), lock=LockMode.PESSIMISTIC_WRITE)
                if row is None or row.status != admitted["status"] or row.data != admitted:
                    raise AccessDenied("The admitted source-only custody changed")
                latest = await self._context(copy.deepcopy(row.data))
                if (
                    latest["grant"] != grant
                    or latest["binding"] != context["binding"]
                    or (latest["user"].id, latest["user"].issuer, latest["user"].subject)
                    != (caller.id, caller.issuer, caller.subject)
                ):
                    raise AccessDenied("The source-only administrator consent was revoked or changed")
                settings, _ = await self.polling._state(delivery["repository_id"])
                if source_state(settings.data).get(grant["digest"]) != receipt_id:
                    raise AccessDenied("The durable source-only reservation changed")

        try:
            admission = SealedSourceEditAdmission(
                run_id=request.run_id,
                native_actor=copy.deepcopy(grant["admin_snapshot"]),
                caller=caller,
                request=request.activation,
                evidence=evidence,
                verify=verify,
            )
            result = await self.workspaces.activate_source_edit_from_scm(admission)
            async with self.polling._locked(context["project"], delivery["repository_id"], [user.id.hex]):
                await verify()
                row = await self.inbox.find_by_id(identifier(receipt_id), lock=LockMode.PESSIMISTIC_WRITE)
                assert row is not None
                if row.data == admitted and row.status == admitted["status"]:
                    await collaboration._write_row(
                        row,
                        status="processed",
                        action_status="completed",
                        reason=None,
                        run_id=request.run_id,
                        action_result=dict(
                            status="queued",
                            run_id=request.run_id,
                            request_id=request.activation.request_id,
                            native_intent_digest=result["intent_digest"],
                        ),
                        feedback_status="held",
                    )
        except BaseException:
            await self.store.assert_ownership()
            async with self.store._unit():
                row = await self.inbox.find_by_id(identifier(receipt_id), lock=LockMode.PESSIMISTIC_WRITE)
                assert row is not None
                if row.data == admitted and row.status == admitted["status"]:
                    await collaboration._write_row(
                        row,
                        action_status="needs_reconciliation",
                        reason="source_only_activation_unknown",
                        feedback_status="held",
                    )
            raise
        finally:
            CURRENT_PRINCIPAL.reset(token)
        return dict(await collaboration.receipt(receipt_id))
