# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Immutable ownership reservations and terminal cancellation of original requests."""

from __future__ import annotations

import copy
from typing import TYPE_CHECKING, Any

from pyfly.data.relational.sqlalchemy import LockMode

from firefly_forge.identity import Principal
from firefly_forge.journal import digest
from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied
from firefly_forge.project_persistence import ForgeProjectRepository
from firefly_forge.run_creation_models import (
    MAX_CONTROL_BYTES,
    MAX_PENDING_RESERVATIONS,
    RunCreationAdmittedDecision,
    RunCreationCancelInput,
    RunCreationCancellation,
    RunCreationCancelledDecision,
    RunCreationReservation,
)
from firefly_forge.run_creation_persistence import (
    ForgeRunCreationCancellationEntity,
    ForgeRunCreationCancellationRepository,
    ForgeRunCreationReservationEntity,
    ForgeRunCreationReservationRepository,
)
from firefly_forge.store import identifier, now, prepared_credential_rejection

if TYPE_CHECKING:
    from firefly_forge.run_creation_requests import RunCreationRequests

Data = dict[str, Any]
SCOPE = ("repository_id", "project_id", "component_id")


def current(actor: Principal) -> None:
    if CURRENT_PRINCIPAL.get() != actor:
        raise AccessDenied("The current run creation identity changed")


class RunCreationControls:
    def __init__(
        self,
        owner: RunCreationRequests,
        reservations: ForgeRunCreationReservationRepository,
        cancellations: ForgeRunCreationCancellationRepository,
    ) -> None:
        self.owner, self.store = owner, owner.store
        self.reservations, self.cancellations = reservations, cancellations

    async def _load(self, actor: Principal, request_id: str, *, cancelled: bool = False) -> Data | None:
        records = self.cancellations if cancelled else self.reservations
        size = await records.envelope_bytes(identifier(request_id))
        current(actor)
        if size is None:
            return None
        if size > MAX_CONTROL_BYTES:
            raise ValueError("Stored run creation control exceeds its bounded read")
        row = await records.find_by_id(identifier(request_id))
        current(actor)
        if row is None:
            raise ValueError("Run creation control changed during its bounded read")
        model = RunCreationCancellation if cancelled else RunCreationReservation
        value = model.model_validate(copy.deepcopy(row.data)).model_dump(mode="json")
        if (
            row.id.hex != request_id
            or value["request_id"] != request_id
            or row.actor_id.hex != value["original_actor"]["user_id"]
            or row.repository_id.hex != value["repository_id"]
            or (row.project_id.hex if row.project_id else None) != value["project_id"]
            or row.component_id != value["component_id"]
            or row.authentication_method != value["original_actor"]["authentication_method"]
            or row.request_digest != value["request_digest"]
            or row.receipt_digest != value["digest"]
            or digest({key: item for key, item in value.items() if key != "digest"}) != value["digest"]
        ):
            raise ValueError("Run creation control integrity failed")
        if cancelled and (
            not isinstance(row, ForgeRunCreationCancellationEntity)
            or row.cancelled_authentication_method != value["cancelled_by"]["authentication_method"]
        ):
            raise ValueError("Run creation cancelling actor integrity failed")
        return value

    async def _records(self, actor: Principal, request_id: str) -> tuple[Data | None, Data | None, Data | None]:
        admitted = await self.owner._load(request_id)
        current(actor)
        reservation = await self._load(actor, request_id)
        cancelled = await self._load(actor, request_id, cancelled=True)
        if cancelled is not None and (
            admitted is not None
            or reservation is None
            or any(cancelled[key] != reservation[key] for key in (*SCOPE, "original_actor", "request_digest"))
        ):
            raise ValueError("Run creation terminal decision integrity failed")
        if (
            admitted is not None
            and reservation is not None
            and any(admitted[key] != reservation[key] for key in (*SCOPE, "original_actor", "request_digest"))
        ):
            raise ValueError("Run creation admission reservation integrity failed")
        return admitted, reservation, cancelled

    def _binding(self, actor: Principal, value: Data, incoming: Data, *, admission: bool = False) -> None:
        current(actor)
        self.owner._owner(actor, value)
        if value["request_digest"] != incoming["request_digest"] or any(
            value[key] != incoming.get(key) for key in SCOPE
        ):
            raise ValueError("Run creation identity already has different input or scope")
        if admission and value["original_actor"]["authentication_method"] != actor.authentication_method:
            raise ValueError("Run creation identity already has another authentication method")

    async def _reserve(self, actor: Principal, request_id: str, incoming: Data, *, terminal: bool = False) -> Data:
        if not terminal and await self.reservations.pending_for_actor(actor.id) >= MAX_PENDING_RESERVATIONS:
            raise ValueError("Resolve an original run request before starting more work")
        current(actor)
        value = {
            "version": 1,
            "request_id": request_id,
            "original_actor": {"user_id": actor.id.hex, "authentication_method": actor.authentication_method},
            "request_digest": incoming["request_digest"],
            **{key: incoming.get(key) for key in SCOPE},
            "reserved_at": now(),
        }
        value["digest"] = digest(value)
        value = RunCreationReservation.model_validate(value).model_dump(mode="json")
        self.store.reject_credentials(value)
        await self.reservations.save(
            ForgeRunCreationReservationEntity(
                id=identifier(request_id),
                actor_id=actor.id,
                repository_id=identifier(value["repository_id"]),
                project_id=identifier(value["project_id"]) if value["project_id"] else None,
                component_id=value["component_id"],
                authentication_method=actor.authentication_method,
                request_digest=value["request_digest"],
                receipt_digest=value["digest"],
                data=value,
            )
        )
        current(actor)
        return value

    async def reserve(self, actor: Principal, request_id: str, incoming: Data) -> Data | None:
        with prepared_credential_rejection(self.store):
            async with self.store._unit():
                await self.owner._lock_actor(actor, incoming.get("project_id"), request_id)
                await self.owner._scope(actor, incoming, write=False)
                admitted, reservation, cancelled = await self._records(actor, request_id)
                if admitted is not None:
                    self._binding(actor, admitted, incoming, admission=True)
                    return admitted
                if reservation is None:
                    reservation = await self._reserve(actor, request_id, incoming)
                self._binding(actor, reservation, incoming, admission=True)
                if cancelled is not None:
                    raise ValueError("This original run request was cancelled")
        return None

    async def eligible(self, actor: Principal, request_id: str, incoming: Data) -> None:
        admitted, reservation, cancelled = await self._records(actor, request_id)
        if reservation is None:
            raise ValueError("The original run creation reservation is unavailable")
        self._binding(actor, reservation, incoming, admission=True)
        if cancelled is not None:
            raise ValueError("This original run request was cancelled")
        if admitted is not None:
            self._binding(actor, admitted, incoming, admission=True)

    async def _return(self, actor: Principal, value: Data, *, admitted: bool) -> Data:
        if admitted:
            receipt = await self.owner._return(actor, value)
            return RunCreationAdmittedDecision.model_validate(
                {"version": 1, "outcome": "admitted", "receipt": receipt}
            ).model_dump(mode="json")
        self.owner._owner(actor, value)
        await self.store.admit_credentials(value)
        current(actor)
        with prepared_credential_rejection(self.store):
            async with self.store._unit():
                await self.owner._lock_actor(actor, value["project_id"])
                if value["project_id"]:
                    await ForgeProjectRepository().find_by_id(
                        identifier(value["project_id"]), lock=LockMode.PESSIMISTIC_WRITE
                    )
                repository = await self.store.repository_records.find_by_id(
                    identifier(value["repository_id"]), lock=LockMode.PESSIMISTIC_WRITE
                )
                current(actor)
                if repository is None:
                    raise AccessDenied("The original run request source is unavailable")
                await self.owner._scope(actor, value, write=False)
                self.store.reject_credentials(value)
        current(actor)
        return RunCreationCancelledDecision.model_validate(
            {"version": 1, "outcome": "cancelled", "cancellation": value}
        ).model_dump(mode="json")

    async def decision(self, actor: Principal, request_id: str) -> Data:
        async with self.store._unit():
            admitted, reservation, cancelled = await self._records(actor, request_id)
        if admitted is not None:
            return await self._return(actor, admitted, admitted=True)
        if cancelled is not None:
            return await self._return(actor, cancelled, admitted=False)
        if reservation is not None:
            self.owner._owner(actor, reservation)
            async with self.store._unit():
                await self.owner._lock_actor(actor, reservation["project_id"])
                await self.owner._scope(actor, reservation, write=False)
        raise KeyError("No terminal decision is recorded for this original run request")

    async def cancel(self, actor: Principal, request_id: str, body: RunCreationCancelInput) -> tuple[Data, bool]:
        incoming = RunCreationCancelInput.model_validate(body.model_dump(mode="json")).model_dump(mode="json")
        await self.store.admit_credentials(incoming)
        current(actor)
        created = False
        with prepared_credential_rejection(self.store):
            async with self.store._unit():
                await self.owner._lock_actor(actor, incoming["project_id"], request_id)
                await self.owner._scope(actor, incoming, write=False)
                admitted, reservation, cancelled = await self._records(actor, request_id)
                if admitted is not None:
                    self._binding(actor, admitted, incoming)
                    value = admitted
                else:
                    if reservation is None:
                        reservation = await self._reserve(actor, request_id, incoming, terminal=True)
                    self._binding(actor, reservation, incoming)
                    if cancelled is None:
                        value = {
                            "version": 1,
                            "request_id": request_id,
                            "original_actor": reservation["original_actor"],
                            "request_digest": reservation["request_digest"],
                            **{key: reservation[key] for key in SCOPE},
                            "cancelled_by": {
                                "user_id": actor.id.hex,
                                "authentication_method": actor.authentication_method,
                            },
                            "cancelled_at": now(),
                        }
                        value["digest"] = digest(value)
                        value = RunCreationCancellation.model_validate(value).model_dump(mode="json")
                        self.store.reject_credentials(value)
                        await self.cancellations.save(
                            ForgeRunCreationCancellationEntity(
                                id=identifier(request_id),
                                actor_id=actor.id,
                                repository_id=identifier(value["repository_id"]),
                                project_id=identifier(value["project_id"]) if value["project_id"] else None,
                                component_id=value["component_id"],
                                authentication_method=value["original_actor"]["authentication_method"],
                                cancelled_authentication_method=actor.authentication_method,
                                request_digest=value["request_digest"],
                                receipt_digest=value["digest"],
                                data=value,
                            )
                        )
                        current(actor)
                        created = True
                    else:
                        value = cancelled
                await self.owner._scope(actor, incoming, write=False)
                self.store.reject_credentials(value)
        return await self._return(actor, value, admitted=admitted is not None), created
