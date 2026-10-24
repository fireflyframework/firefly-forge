# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""One atomic admission per original identity; exact reads never dispatch work."""

from __future__ import annotations

import copy
import json
import re
from typing import Any

from pyfly.container import service
from pyfly.data.relational.sqlalchemy import LockMode
from pyfly.data.transaction import current_unit_of_work
from sqlalchemy import text

from firefly_forge.checkpoint_test_sources import CheckpointTestSources
from firefly_forge.identity import Principal
from firefly_forge.identity_persistence import ForgeIdentityUserRepository
from firefly_forge.journal import digest
from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied, ProjectAccess
from firefly_forge.project_persistence import ForgeProjectRepository
from firefly_forge.run_creation_control import RunCreationControls
from firefly_forge.run_creation_models import (
    MAX_ENVELOPE_BYTES,
    CreationActor,
    RunCreationCancelInput,
    RunCreationInput,
    RunCreationReceipt,
)
from firefly_forge.run_creation_persistence import (
    ForgeRunCreationCancellationRepository,
    ForgeRunCreationRequestEntity,
    ForgeRunCreationRequestRepository,
    ForgeRunCreationReservationRepository,
)
from firefly_forge.store import Store, identifier, prepared_credential_rejection

Data = dict[str, Any]


def _outside() -> None:
    if current_unit_of_work() is not None:
        raise ValueError("Prepare run creation outside the caller transaction")


def _request_id(value: str) -> None:
    if not isinstance(value, str) or re.fullmatch(r"[a-f0-9]{32}", value) is None:
        raise ValueError("Run creation requires a lowercase 32-character request identity")


def _bounded(value: Data) -> None:
    if len(json.dumps(value, ensure_ascii=False).encode()) > MAX_ENVELOPE_BYTES:
        raise ValueError("Run creation envelope exceeds 256 KiB")


def _context(actor: Principal) -> None:
    if CURRENT_PRINCIPAL.get() != actor:
        raise AccessDenied("The current run creation identity changed")


@service
class RunCreationRequests:
    def __init__(
        self,
        store: Store,
        records: ForgeRunCreationRequestRepository | None = None,
        reservations: ForgeRunCreationReservationRepository | None = None,
        cancellations: ForgeRunCreationCancellationRepository | None = None,
        checkpoint_sources: CheckpointTestSources | None = None,
    ) -> None:
        self.store = store
        if checkpoint_sources is not None:
            if checkpoint_sources.store is not store:
                raise ValueError("Checkpoint source admission belongs to another native store")
            self.store.checkpoint_sources = checkpoint_sources
        self.records = records or ForgeRunCreationRequestRepository()
        self.controls = RunCreationControls(
            self,
            reservations or ForgeRunCreationReservationRepository(),
            cancellations or ForgeRunCreationCancellationRepository(),
        )

    @staticmethod
    def _actor() -> Principal:
        actor = CURRENT_PRINCIPAL.get()
        if actor is None or actor.authentication_method not in {"oidc_session", "oidc_bearer", "local_bootstrap"}:
            raise AccessDenied("Run creation requires a current native authenticated user")
        return actor

    async def _lock_actor(self, actor: Principal, project: str | None, request_id: str | None = None) -> None:
        if current_unit_of_work() is None:
            raise ValueError("Run creation fences require a native transaction")
        _context(actor)
        if project:
            async with self.store._unit() as session:
                await session.execute(
                    text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                    {"key": self.store.schema + ":members:" + project},
                )
            _context(actor)
        user = await ForgeIdentityUserRepository().find_by_id(actor.id, lock=LockMode.PESSIMISTIC_WRITE)
        _context(actor)
        if user is None or not user.enabled or (user.issuer, user.subject) != (actor.issuer, actor.subject):
            raise AccessDenied("The native run creation identity is unavailable")
        if request_id is not None:
            async with self.store._unit() as session:
                await session.execute(
                    text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                    {"key": self.store.schema + ":run-creation:" + request_id},
                )
            _context(actor)

    async def _scope(self, actor: Principal, value: Data, *, write: bool) -> None:
        _context(actor)
        access = ProjectAccess(self.store)
        project = value.get("project_id")
        administrator = await access.current_administrator(actor)
        _context(actor)
        if not administrator:
            if not project:
                raise AccessDenied("Unassigned run creation requires a factory administrator")
            await access.require_project(actor, project, "read")
            _context(actor)
            if write:
                await access.require_project(actor, project, "execute")
                _context(actor)
                if value.get("publish"):
                    await access.require_project(actor, project, "release")
                    _context(actor)
        await access.require_repository(actor, value["repository_id"], "read")
        _context(actor)
        if write:
            await access.require_repository(actor, value["repository_id"], "execute")
            _context(actor)
            if value.get("publish"):
                await access.require_repository(actor, value["repository_id"], "release")
                _context(actor)

    async def _load(self, request_id: str) -> Data | None:
        size = await self.records.envelope_bytes(identifier(request_id))
        if size is None:
            return None
        if size > MAX_ENVELOPE_BYTES:
            raise ValueError("Stored run creation envelope exceeds its bounded read")
        row = await self.records.find_by_id(identifier(request_id))
        if row is None:
            raise ValueError("Run creation identity changed during its bounded read")
        value = RunCreationReceipt.model_validate(copy.deepcopy(row.data)).model_dump(mode="json")
        _bounded(value)
        normalized = RunCreationInput.model_validate({"version": 1, "input": value["input"]})
        original_input = normalized.input.model_dump(mode="json", exclude_unset=True)
        if (
            row.id.hex != request_id
            or value["request_id"] != request_id
            or row.actor_id.hex != value["original_actor"]["user_id"]
            or row.run_id.hex != value["run_id"]
            or row.repository_id.hex != value["repository_id"]
            or (row.project_id.hex if row.project_id else None) != value["project_id"]
            or row.request_digest != value["request_digest"]
            or row.receipt_digest != value["digest"]
            or original_input != value["input"]
            or original_input["repository_id"] != value["repository_id"]
            or original_input.get("project_id") != value["project_id"]
            or original_input.get("component_id") != value["component_id"]
            or digest({"version": 1, "input": original_input}) != value["request_digest"]
            or digest({key: item for key, item in value.items() if key != "digest"}) != value["digest"]
        ):
            raise ValueError("Run creation receipt integrity failed")
        return value

    @staticmethod
    def _owner(actor: Principal, receipt: Data, request_digest: str | None = None) -> None:
        if receipt["original_actor"]["user_id"] != actor.id.hex:
            raise AccessDenied("The run creation request belongs to another user")
        if request_digest is not None and (
            receipt["request_digest"] != request_digest
            or receipt["original_actor"]["authentication_method"] != actor.authentication_method
        ):
            raise ValueError("Run creation identity already has another actor or different input")

    async def _return(self, actor: Principal, receipt: Data, *, write: bool = False) -> Data:
        _context(actor)
        self._owner(actor, receipt)
        await self.store.admit_credentials(receipt)
        _context(actor)
        with prepared_credential_rejection(self.store):
            async with self.store._unit():
                await self._lock_actor(actor, receipt["project_id"])
                if receipt["project_id"]:
                    await ForgeProjectRepository().find_by_id(
                        identifier(receipt["project_id"]), lock=LockMode.PESSIMISTIC_WRITE
                    )
                await self.store.repository_records.find_by_id(
                    identifier(receipt["repository_id"]), lock=LockMode.PESSIMISTIC_WRITE
                )
                run = await self.store.run_records.find_by_id(
                    identifier(receipt["run_id"]), lock=LockMode.PESSIMISTIC_WRITE
                )
                if run is None or any(
                    run.data.get(key) != receipt[key] for key in ("repository_id", "project_id", "component_id")
                ):
                    raise ValueError("Original run scope differs from the retained creation receipt")
                if receipt.get("source_checkpoint") is not None:
                    from firefly_forge.checkpoint_test_models import frozen_checkpoint_source

                    if run.data.get("source_checkpoint_snapshot") != receipt["source_checkpoint"]:
                        raise ValueError("Original run source differs from its retained checkpoint creation receipt")
                    frozen_checkpoint_source(run.data, {"id": receipt["repository_id"]})
                await self.store.admit_credentials(receipt)
                await self._scope(actor, receipt["input"], write=write)
                _context(actor)
                self.store.reject_credentials(receipt)
        self.store.reject_credentials(receipt)
        _context(actor)
        return copy.deepcopy(receipt)

    async def get(self, request_id: str) -> Data:
        _outside()
        _request_id(request_id)
        actor = self._actor()
        async with self.store._unit():
            value = await self._load(request_id)
        _context(actor)
        if value is None:
            raise KeyError("Run creation request not found")
        return await self._return(actor, value)

    async def admit(self, request_id: str, request: RunCreationInput) -> tuple[Data, bool]:
        _outside()
        _request_id(request_id)
        actor = self._actor()
        raw = request.model_dump(mode="json", exclude_unset=True, warnings=False)
        _bounded(raw)
        request = RunCreationInput.model_validate(raw)
        original = request.input.model_dump(mode="json", exclude_unset=True)
        request_digest = digest({"version": 1, "input": original})
        binding = {**original, "request_digest": request_digest}
        previous = await self.controls.reserve(actor, request_id, binding)
        _context(actor)
        if previous is not None:
            self._owner(actor, previous, request_digest)
            return await self._return(actor, previous, write=True), False
        async with self.store._unit():
            await self._lock_actor(actor, request.input.project_id)
            await self._scope(actor, original, write=True)
        prepared = await self.store.prepare_run(request.input)
        _context(actor)
        created = False
        with prepared_credential_rejection(self.store):
            async with self.store._unit():
                await self._lock_actor(actor, request.input.project_id, request_id)
                await self.controls.eligible(actor, request_id, binding)
                await self.store.fence_prepared_runs([prepared])
                await self._scope(actor, original, write=True)
                previous = await self._load(request_id)
                if previous is not None:
                    self._owner(actor, previous, request_digest)
                    receipt = previous
                else:
                    run = await self.store.insert_prepared_run_locked(prepared)
                    admission = {key: value for key, value in run.items() if key != "events"}
                    receipt = {
                        "version": 1,
                        "request_id": request_id,
                        "original_actor": CreationActor.model_validate(
                            {"user_id": actor.id.hex, "authentication_method": actor.authentication_method}
                        ).model_dump(),
                        "request_digest": request_digest,
                        "input": original,
                        "repository_id": run["repository_id"],
                        "project_id": run.get("project_id"),
                        "component_id": run.get("component_id"),
                        "run_id": run["id"],
                        "admitted_at": run["created_at"],
                        "admission_digest": digest(admission),
                    }
                    if run.get("source_checkpoint_snapshot") is not None:
                        receipt["source_checkpoint"] = copy.deepcopy(run["source_checkpoint_snapshot"])
                    receipt["digest"] = digest(receipt)
                    receipt = RunCreationReceipt.model_validate(receipt).model_dump(mode="json")
                    _bounded(receipt)
                    await self.store.admit_credentials(receipt)
                    _context(actor)
                    await self.records.save(
                        ForgeRunCreationRequestEntity(
                            id=identifier(request_id),
                            actor_id=actor.id,
                            repository_id=identifier(receipt["repository_id"]),
                            project_id=identifier(receipt["project_id"]) if receipt["project_id"] else None,
                            run_id=identifier(run["id"]),
                            request_digest=request_digest,
                            receipt_digest=receipt["digest"],
                            data=receipt,
                        )
                    )
                    created = True
                await self.store.admit_credentials(receipt)
                await self._scope(actor, original, write=True)
                _context(actor)
                self.store.assert_prepared_run_settings(prepared)
                self.store.reject_credentials(receipt)
        return await self._return(actor, receipt, write=True), created

    async def cancel(self, request_id: str, body: RunCreationCancelInput) -> tuple[Data, bool]:
        _outside()
        _request_id(request_id)
        return await self.controls.cancel(self._actor(), request_id, body)

    async def decision(self, request_id: str) -> Data:
        _outside()
        _request_id(request_id)
        return await self.controls.decision(self._actor(), request_id)
