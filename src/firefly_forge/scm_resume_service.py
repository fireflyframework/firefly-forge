# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Explicit same-run SCM continuation, separate from V1 delegation authority."""

from __future__ import annotations

import copy
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
from firefly_forge.collaboration_persistence import ForgeCollaborationSettingsRepository, ForgeWebhookInboxRepository
from firefly_forge.identity_persistence import ForgeIdentityUserRepository
from firefly_forge.journal import Journal, digest
from firefly_forge.persistence import (
    ForgeEventEntity,
    ForgeJournalEntity,
    ForgePlanEntity,
    ForgeRunEntity,
    ForgeTaskEntity,
)
from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied, ProjectAccess
from firefly_forge.project_persistence import ForgeProjectRepository
from firefly_forge.sandbox_persistence import ForgeSandboxEntity
from firefly_forge.scm_actor_authority import SCMActorAuthority, binding_key
from firefly_forge.scm_authority_models import VerifiedSCMReferenceV1
from firefly_forge.scm_resume_cut import FROZEN_FIELDS, resume_cut
from firefly_forge.scm_resume_models import (
    SCMResumeCommandV2,
    SCMResumeGrantInputV2,
    SCMResumeGrantReceiptV2,
    SCMResumeStatusV2,
)
from firefly_forge.store import Store, identifier, now

Data = dict[str, Any]
STATE_KEY = "_scm_resume_authority_v2"
EVENT_KIND = "scm_resume_grant_revision"


def grant_state(data: Data) -> Data:
    state = copy.deepcopy(data.get(STATE_KEY, {"version": 2, "heads": {}, "requests": {}}))
    if (
        not isinstance(state, dict)
        or set(state) != {"version", "heads", "requests"}
        or type(state["version"]) is not int
        or state["version"] != 2
        or not isinstance(state["heads"], dict)
        or not isinstance(state["requests"], dict)
        or len(state["heads"]) > 100
        or len(state["requests"]) > 5000
        or len(json.dumps(state, ensure_ascii=False).encode()) > 16 * 1024 * 1024
    ):
        raise ValueError("The V2 resume grant index is invalid or exceeds its bounded retention")
    return state


class SCMResumeAuthority:
    """A Factory bean; all mutable admission checks are repeated under native locks."""

    def __init__(
        self,
        store: Store,
        authority: SCMActorAuthority,
        journal: Journal,
        settings: ForgeCollaborationSettingsRepository,
        inbox: ForgeWebhookInboxRepository,
        events: ForgeAccessEventRepository,
    ) -> None:
        self.store, self.authority, self.journal = store, authority, journal
        self.settings, self.inbox, self.events = settings, inbox, events

    async def _record(self, event_id: str, project: str, repository: str) -> Data:
        async with self.store._unit() as session:
            size = await session.scalar(
                select(func.octet_length(cast(ForgeAccessEventEntity.data, Text))).where(
                    ForgeAccessEventEntity.id == identifier(event_id)
                )
            )
            if size is None:
                raise KeyError("Resume grant not found")
            if size > 65536:
                raise ValueError("Resume grant exceeds 64 KiB")
            row = await self.events.find_by_id(identifier(event_id))
            if (
                row is None
                or row.project_id.hex != project
                or set(row.data) != {"kind", "receipt"}
                or row.data["kind"] != EVENT_KIND
            ):
                raise ValueError("Resume grant event scope is invalid")
            receipt = SCMResumeGrantReceiptV2.model_validate(row.data["receipt"]).model_dump(mode="json")
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
            raise ValueError("Resume grant envelope is invalid")
        request = SCMResumeGrantInputV2.model_validate(receipt["request"]).model_dump(mode="json")
        if (
            receipt["version"] != 2
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
            raise ValueError("Resume grant immutable binding is invalid")
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
                raise ValueError("Resume history has no consistent current head")
            return None
        if not isinstance(pointer, dict) or set(pointer) != {"event_id", "digest"}:
            raise ValueError("Resume grant pointer is invalid")
        retained = await self._record(pointer["event_id"], project, repository)
        if (
            retained["digest"] != pointer["digest"]
            or key
            and (latest is None or latest.hex != retained["event_id"] or retained["binding_key"] != key)
            or request_id
            and retained["request"]["request_id"] != request_id
        ):
            raise ValueError("Resume grant pointer does not bind immutable history")
        return retained

    async def _return(self, actor: Any, receipt: Data, *, admin: bool) -> Data:
        await self.store.admit_credentials(receipt)
        await self.authority._reader(actor, receipt["project_id"], receipt["repository_id"], admin=admin)
        self.store.reject_credentials(receipt)
        return copy.deepcopy(receipt)

    async def get(self, project: str, repository: str, request_id: str) -> Data:
        if current_unit_of_work() is not None:
            raise ValueError("Read resume grants outside a caller transaction")
        actor = CURRENT_PRINCIPAL.get()
        await self.authority._reader(actor, project, repository, admin=False)
        receipt = await self._grant(project, repository, request_id=request_id)
        if receipt is None:
            raise KeyError("Resume grant request not found; absence never permits another write")
        return await self._return(actor, receipt, admin=False)

    async def current(self, project: str, repository: str, binding_request: str) -> Data:
        if current_unit_of_work() is not None:
            raise ValueError("Read resume grants outside a caller transaction")
        actor = CURRENT_PRINCIPAL.get()
        await self.authority._reader(actor, project, repository, admin=False)
        binding = await self.authority._existing(project, repository, binding_request)
        if binding is None:
            raise KeyError("V1 binding not found")
        receipt = await self._grant(project, repository, key=binding["binding_key"])
        if receipt is None:
            raise KeyError("No separately consented V2 grant exists")
        return await self._return(actor, receipt, admin=False)

    async def _binding(self, project: str, repository: str, request: SCMResumeGrantInputV2) -> Data:
        binding = await self.authority._existing(project, repository, request.expected_binding_request_id)
        if binding is None:
            raise AccessDenied("Review an exact current V1 mapping first")
        head = await self.authority._head(project, repository, binding["binding_key"])
        project_cut = await self.authority._project(project, repository, request.component_ids)
        if (
            head != binding
            or binding["digest"] != request.expected_binding_digest
            or not binding["request"]["active"]
            or "respond" not in binding["request"]["allowed_actions"]
            or not set(request.component_ids) <= set(binding["request"]["component_ids"])
            or digest(project_cut) != request.expected_project_binding_digest
            or binding["project_binding_digest"] != digest(project_cut)
        ):
            raise AccessDenied("The reviewed V1 mapping or project scope changed")
        repository_cut = await self.store.repository(repository)
        reference = binding["verified_reference"]
        scope = self.authority.scm.delegation_scope(repository_cut)
        if scope != {k: reference[k] for k in scope} or not self.authority._key_current(repository, reference):
            raise AccessDenied("The V1 connection or verification key changed")
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

    async def configure(self, project: str, repository: str, request: SCMResumeGrantInputV2) -> Data:
        if current_unit_of_work() is not None:
            raise ValueError("Admit resume consent outside a caller transaction")
        request = SCMResumeGrantInputV2.model_validate(request.model_dump(mode="json"))
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
                    raise AccessDenied("Configure collaboration before consenting to resume")
                state = grant_state(row.data)
                previous = await self._grant(project, repository, key=binding["binding_key"])
                if request.expected_previous_digest != (previous["digest"] if previous else None):
                    raise AccessDenied("Resume grant revision changed; preserve the reviewed request")
                revision = previous["revision"] + 1 if previous else 1
                if (
                    revision > 50
                    or len(state["requests"]) >= 5000
                    or binding["binding_key"] not in state["heads"]
                    and len(state["heads"]) >= 100
                ):
                    raise ValueError("Resume grant retention limit reached")
                retained = dict(
                    version=2,
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
                SCMResumeGrantReceiptV2.model_validate(retained)
                envelope = {"kind": EVENT_KIND, "receipt": retained}
                total = await session.scalar(
                    select(
                        func.coalesce(func.sum(func.octet_length(cast(ForgeAccessEventEntity.data, Text))), 0)
                    ).where(
                        ForgeAccessEventEntity.data["kind"].as_string() == EVENT_KIND,
                        ForgeAccessEventEntity.data["receipt"]["repository_id"].as_string() == repository,
                    )
                )
                if (
                    len(json.dumps(envelope, ensure_ascii=False).encode()) > 65536
                    or int(total or 0) + len(json.dumps(envelope, ensure_ascii=False).encode()) > 16 * 1024 * 1024
                ):
                    raise ValueError("Resume grant history exceeds its byte budget")
                self.store.reject_credentials(retained)
                await self.events.save(
                    ForgeAccessEventEntity(
                        id=identifier(retained["event_id"]), project_id=identifier(project), data=envelope
                    )
                )
                pointer = {"event_id": retained["event_id"], "digest": retained["digest"]}
                state["heads"][binding["binding_key"]] = pointer
                state["requests"][project + ":" + request.request_id] = pointer
                row.data = {**row.data, STATE_KEY: state}
                await self.settings.save(row)
        return await self._return(actor, retained, admin=True)

    async def _run_data(self, run_id: str) -> Data:
        async with self.store._unit() as session:
            size = await session.scalar(
                select(func.octet_length(cast(ForgeRunEntity.data, Text))).where(
                    ForgeRunEntity.id == identifier(run_id)
                )
            )
            if not isinstance(size, int) or not 0 < size <= 1024 * 1024:
                raise AccessDenied("The original run is unavailable or exceeds its bounded admission snapshot")
            row = await self.store.run_records.find_by_id(identifier(run_id))
            if (
                row is None
                or row.status != row.data.get("status")
                or row.id.hex != row.data.get("id")
                or row.repository_id.hex != row.data.get("repository_id")
            ):
                raise AccessDenied("The original run scalar scope is inconsistent")
            return copy.deepcopy(row.data)

    async def _context(self, delivery: Data) -> Data:
        command = delivery["command"]
        run = await self._run_data(command["run_id"])
        project, repository, component = run.get("project_id"), run["repository_id"], run.get("component_id")
        if not project or not component or repository != delivery["repository_id"]:
            raise AccessDenied("Resume requires this run's original project, repository and component")
        reference = VerifiedSCMReferenceV1.model_validate(delivery.get("verification")).model_dump(mode="json")
        if (
            reference["payload_digest"] != delivery.get("digest")
            or reference["provider"] != delivery.get("provider")
            or reference["external_actor_id"] != delivery.get("actor")
            or reference["remote_repository_id"] != delivery.get("remote_id")
        ):
            raise AccessDenied("The retained signed delivery no longer binds the source actor")
        binding = await self.authority._head(project, repository, binding_key(project, reference))
        grant = await self._grant(project, repository, key=binding["binding_key"])
        if grant is None or grant["digest"] != command["expected_grant_digest"] or not grant["request"]["active"]:
            raise AccessDenied("A current separately consented V2 resume grant is required")
        consent = SCMResumeGrantInputV2.model_validate(grant["request"])
        current = await self._binding(project, repository, consent)
        if (
            current != binding
            or grant["native_user_id"] != binding["request"]["native_user_id"]
            or component not in consent.component_ids
        ):
            raise AccessDenied("The resume grant no longer binds this native user and component")
        if {k: v for k, v in reference.items() if k != "payload_digest"} != {
            k: v for k, v in binding["verified_reference"].items() if k != "payload_digest"
        }:
            raise AccessDenied("The signed source connection or account cut changed")
        async with self.store._unit():
            settings_row = await self.settings.find_for_repository(identifier(repository))
            settings = settings_row.data if settings_row else {}
            identity = await ForgeIdentityUserRepository().find_by_id(identifier(grant["native_user_id"]))
            memberships = await ForgeMembershipRepository().for_user(identifier(grant["native_user_id"]))
        permission = "read" if command["action"] == "status" else "execute"
        if (
            not settings.get("enabled")
            or delivery["actor"] not in settings.get("trusted_actors", [])
            or (
                command["action"] not in consent.allowed_actions
                and command["action"]
                not in [*settings.get("allowed_commands", []), *settings.get("channel_commands", [])]
            )
            or identity is None
            or not identity.enabled
            or not any(
                item.project_id.hex == project and ProjectAccess.permits(item.role, permission) for item in memberships
            )
        ):
            raise AccessDenied("Current collaboration, identity and project permission are required")
        if delivery.get("global_ingress"):
            channel = self.store.configuration._read(self.store.configuration.directory / "channel-bootstrap.json").get(
                reference["provider"], {}
            )
            if not channel.get("enabled") or delivery["actor"] not in channel.get("trusted_actors", []):
                raise AccessDenied("The signed global ingress actor is no longer enabled")
        return {"run": run, "grant": grant, "binding": binding, "user_id": identity.id.hex}

    async def _original(self, run: Data) -> None:
        if not run.get("execution_actor"):
            raise AccessDenied("Resume cannot manufacture an original execution actor")
        await ProjectAccess(self.store).authorize_execution(run)

    async def _cut(self, context: Data) -> Data:
        run = context["run"]
        async with self.store._unit() as session:
            await session.execute(text("SET LOCAL statement_timeout = '3s'"))
            for entity in (ForgeJournalEntity, ForgeTaskEntity, ForgeEventEntity):
                count, size = (
                    await session.execute(
                        select(
                            func.count(), func.coalesce(func.sum(func.octet_length(cast(entity.data, Text))), 0)
                        ).where(entity.run_id == identifier(run["id"]))
                    )
                ).one()
                if not isinstance(count, int) or not isinstance(size, int) or count > 2000 or size > 4 * 1024 * 1024:
                    raise ValueError("Resume evidence exceeds its bounded read")
            unknown = await session.scalar(
                select(ForgeSandboxEntity.id)
                .where(ForgeSandboxEntity.run_id == identifier(run["id"]), ForgeSandboxEntity.status != "removed")
                .limit(1)
            )
            if unknown is not None:
                raise AccessDenied("A sandbox effect is still retained for reconciliation")
            plan_size = await session.scalar(
                select(func.octet_length(cast(ForgePlanEntity.data, Text)))
                .where(ForgePlanEntity.run_id == identifier(run["id"]))
                .order_by(ForgePlanEntity.revision.desc())
                .limit(1)
            )
            if plan_size is None or plan_size > 1024 * 1024:
                raise ValueError("The current development plan is unavailable or exceeds 1 MiB")
            plan = await self.journal.plans.find_current(identifier(run["id"]))
            if plan is None or plan.revision != plan.data.get("revision") or plan.run_id.hex != run["id"]:
                raise ValueError("The original development plan is unavailable")
            tasks = await self.journal.tasks.find_for_run(identifier(run["id"]))
            entries = await self.journal.records.find_for_run(identifier(run["id"]))
            if any(
                item.run_id.hex != run["id"]
                or item.sequence != item.data.get("sequence")
                or item.digest != item.data.get("digest")
                or item.kind != item.data.get("kind")
                or item.idempotency_key != item.data.get("idempotency_key")
                or item.previous_hash != item.data.get("previous_hash")
                for item in entries
            ):
                raise ValueError("Journal scalar columns do not match their immutable envelopes")
            if any(item.plan_id != plan.id and item.status != "done" for item in tasks):
                raise ValueError("An active task belongs to another plan revision")
            return resume_cut(
                run=run,
                plan={**copy.deepcopy(plan.data), "digest": plan.digest},
                tasks=[
                    {
                        "task_id": item.task_id,
                        "status": item.status,
                        "attempt": item.attempt,
                        "data": copy.deepcopy(item.data),
                    }
                    for item in tasks
                ],
                entries=[copy.deepcopy(item.data) for item in entries],
                grant_digest=context["grant"]["digest"],
                binding_digest=context["binding"]["digest"],
                native_user_id=context["user_id"],
            )

    async def _delivery(self, receipt_id: str) -> Data:
        async with self.store._unit():
            row = await self.inbox.find_by_id(identifier(receipt_id))
            if row is None or row.data.get("id") != receipt_id or row.status != row.data.get("status"):
                raise AccessDenied("The retained SCM delivery is unavailable or inconsistent")
            if row.repository_id.hex != row.data.get("repository_id") or row.provider != row.data.get("provider"):
                raise AccessDenied("The SCM delivery scalar scope is inconsistent")
            return copy.deepcopy(row.data)

    @asynccontextmanager
    async def _locked(self, delivery: Data, context: Data) -> AsyncIterator[Any]:
        run = context["run"]
        async with self.store._unit() as session:
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": self.store.schema + ":members:" + run["project_id"]},
            )
            for user in sorted({context["user_id"], run["execution_actor"]["user_id"]}):
                await ForgeIdentityUserRepository().find_by_id(identifier(user), lock=LockMode.PESSIMISTIC_WRITE)
            await ForgeProjectRepository().find_by_id(identifier(run["project_id"]), lock=LockMode.PESSIMISTIC_WRITE)
            await self.store.repository_records.find_by_id(
                identifier(run["repository_id"]), lock=LockMode.PESSIMISTIC_WRITE
            )
            await self.store.assert_campaign_current(run)
            run_row = await self.store.run_records.find_by_id(identifier(run["id"]), lock=LockMode.PESSIMISTIC_WRITE)
            if (
                run_row is None
                or run_row.status != run_row.data.get("status")
                or run_row.repository_id.hex != run_row.data.get("repository_id")
            ):
                raise AccessDenied("The original run scalar scope is inconsistent")
            row = await self.inbox.find_by_id(identifier(delivery["id"]), lock=LockMode.PESSIMISTIC_WRITE)
            if (
                row is None
                or row.data.get("command") != delivery["command"]
                or row.data.get("verification") != delivery.get("verification")
                or row.data.get("digest") != delivery.get("digest")
            ):
                raise AccessDenied("The retained SCM delivery changed during admission")
            yield row

    async def _write(self, row: Any, **changes: Any) -> None:
        row.data = {**row.data, **changes, "updated_at": now()}
        row.status = row.data["status"]
        await self.inbox.save(row)

    async def _save_completion(self, result: Data) -> None:
        await self.journal._append(result["run_id"], "resume", result["receipt_id"], "scm_resume_receipt", result)

    async def _verify_completion(self, result: Data) -> None:
        cut = result["review"]
        sequences = [cut["journal_event_count"] + 1, cut["journal_event_count"] + 2]
        async with self.store._unit() as session:
            size = await session.scalar(
                select(func.coalesce(func.sum(func.octet_length(cast(ForgeJournalEntity.data, Text))), 0)).where(
                    ForgeJournalEntity.run_id == identifier(result["run_id"]),
                    ForgeJournalEntity.sequence.in_(sequences),
                )
            )
            if not isinstance(size, int) or not 0 < size <= 131072:
                raise AccessDenied("The exact resume completion is unavailable or oversized")
            rows = list(
                await session.scalars(
                    select(ForgeJournalEntity)
                    .where(
                        ForgeJournalEntity.run_id == identifier(result["run_id"]),
                        ForgeJournalEntity.sequence.in_(sequences),
                    )
                    .order_by(ForgeJournalEntity.sequence)
                )
            )
        if len(rows) != 2:
            raise AccessDenied("The exact native resume and completion records are unavailable")
        previous = cut["journal_head"]
        for row, sequence in zip(rows, sequences, strict=True):
            envelope = row.data
            if (
                row.run_id.hex != result["run_id"]
                or row.sequence != sequence
                or envelope.get("sequence") != sequence
                or envelope.get("run_id") != result["run_id"]
                or row.digest != envelope.get("digest")
                or row.previous_hash != previous
                or envelope.get("previous_hash") != previous
                or row.kind != envelope.get("kind")
                or row.idempotency_key != envelope.get("idempotency_key")
                or row.digest != digest({key: value for key, value in envelope.items() if key != "digest"})
            ):
                raise AccessDenied("The native resume completion chain is inconsistent")
            previous = row.digest
        native, completed = (row.data for row in rows)
        if (
            native["kind"] != "resume"
            or native["payload"].get("selection_mode") != "frozen"
            or native["payload"].get("automatic") is not False
            or completed["kind"] != "scm_resume_receipt"
            or completed["idempotency_key"] != result["receipt_id"]
            or completed["payload"] != result
        ):
            raise AccessDenied("The retained result does not match its native same-run completion")

    async def authorize_feedback(self, receipt_id: str) -> None:
        delivery = await self._delivery(receipt_id)
        context = await self._context(delivery)
        await self._preflight(context)
        await self.store.admit_credentials(delivery)
        await self._context(await self._delivery(receipt_id))
        if delivery.get("command", {}).get("action") == "resume" and delivery.get("action_status") == "completed":
            await self._verify_completion(delivery["action_result"])
        self.store.reject_credentials(delivery)

    async def process(self, receipt_id: str) -> Data:
        if current_unit_of_work() is not None:
            raise ValueError("Admit SCM resume outside a caller transaction")
        initial = await self._delivery(receipt_id)
        command = (
            SCMResumeStatusV2 if initial["command"].get("action") == "status" else SCMResumeCommandV2
        ).model_validate(initial["command"])
        context = await self._context(initial)
        if context["grant"]["digest"] != command.expected_grant_digest:
            raise AccessDenied("The reviewed resume grant changed")
        if isinstance(command, SCMResumeCommandV2):
            await self._original(context["run"])
        await self._preflight(context)
        await self.store.admit_credentials({"command": command.model_dump(mode="json"), "grant": context["grant"]})
        context = await self._context(await self._delivery(receipt_id))
        async with self._locked(initial, context) as row:
            context = await self._context(row.data)
            if row.data.get("action_status") == "completed" and row.data["status"] == "processed":
                result = row.data["action_result"]
                if isinstance(command, SCMResumeCommandV2):
                    await self._verify_completion(result)
                if (
                    result.get("request_digest") != digest(command.model_dump(mode="json"))
                    or result.get("receipt_id") != receipt_id
                    or result.get("grant_digest") != context["grant"]["digest"]
                    or digest({k: v for k, v in result.items() if k != "digest"}) != result.get("digest")
                ):
                    raise AccessDenied("The completed outcome does not bind this exact delivery")
            elif row.status != "received" or row.data.get("action_status") not in {None, "not_dispatched"}:
                raise AccessDenied("The original resume intent is unknown; reconcile it without replay")
            else:
                cut = await self._cut(context)
                if isinstance(command, SCMResumeCommandV2):
                    if (
                        cut["digest"] != command.expected_cut_digest
                        or cut["question_id"] != command.expected_question_id
                        or cut["response_digest"] != command.expected_response_digest
                    ):
                        raise AccessDenied("The answered clarification or frozen review cut changed")
                    await self._original(context["run"])
                intent = {
                    "version": 2,
                    "request_digest": digest(command.model_dump(mode="json")),
                    "cut": cut,
                    "native_user_id": context["user_id"],
                    "binding_digest": context["binding"]["digest"],
                }
                await self._write(row, status="needs_reconciliation", action_status="writing", resume_intent=intent)
                resumed = (
                    await self.journal.resume(command.run_id)
                    if isinstance(command, SCMResumeCommandV2)
                    else context["run"]
                )
                if digest({key: resumed.get(key) for key in sorted(FROZEN_FIELDS)}) != cut["frozen_run_digest"]:
                    raise AccessDenied("Resume changed the frozen execution contract")
                result = {
                    "version": 2,
                    "receipt_id": receipt_id,
                    "request_digest": intent["request_digest"],
                    "run_id": command.run_id,
                    "status": resumed["status"],
                    "question_id": cut["question_id"],
                    "response_digest": cut["response_digest"],
                    "resume_cut_digest": cut["digest"],
                    "grant_digest": context["grant"]["digest"],
                    "native_user_id": context["user_id"],
                    "review": cut,
                }
                result["digest"] = digest(result)
                if isinstance(command, SCMResumeCommandV2):
                    await self._save_completion(result)
                self.store.reject_credentials(result)
                await self._write(
                    row,
                    status="processed",
                    action_status="completed",
                    action_result=result,
                    run_id=command.run_id,
                    feedback_status="pending",
                    feedback_stage=resumed["status"],
                )
            output: Data = copy.deepcopy(row.data)
        await self.store.admit_credentials(output)
        await self._context(await self._delivery(receipt_id))
        self.store.reject_credentials(output)
        return output
