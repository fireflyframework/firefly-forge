# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Offline native transfer uses the existing topology ledger and exclusive factory fence."""

from __future__ import annotations

import copy
import hashlib
import ipaddress
import os
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

from pyfly.data.relational.sqlalchemy import LockMode
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from firefly_forge.journal import Journal, digest
from firefly_forge.native_location_files import NativeStorageIdentity, read_postgres_identity, read_storage_identity
from firefly_forge.native_location_models import (
    NativeLocationAcknowledgmentV1,
    NativeLocationCandidateV1,
    NativeLocationFilesV1,
    NativeLocationPlanV1,
)
from firefly_forge.native_location_runtime import (
    NativeLocationUnknown,
    location_binding,
    location_event,
    location_record_id,
    resolve_native_location,
)
from firefly_forge.runtime_role_persistence import ForgeRuntimeTopologyEntity, ForgeRuntimeTopologyRepository
from firefly_forge.runtime_roles import STORAGE_ATTESTATION_ID, TOPOLOGY_ID
from firefly_forge.source_catalog import SourceCatalogBudget, _json
from firefly_forge.store import Store

Data = dict[str, Any]


async def database_fingerprint(session: AsyncSession, schema: str, directory: Path) -> str:
    native = read_postgres_identity(directory)
    row = (
        (
            await session.execute(
                text(
                    "SELECT current_database() AS database, d.oid::bigint AS database_oid, "
                    "n.oid::bigint AS schema_oid, inet_server_port() AS port, "
                    "host(inet_server_addr()) AS address, "
                    "floor(extract(epoch FROM pg_postmaster_start_time()))::bigint AS started "
                    "FROM pg_database d CROSS JOIN pg_namespace n "
                    "WHERE d.datname=current_database() AND n.nspname=:schema"
                ),
                {"schema": schema},
            )
        )
        .mappings()
        .one()
    )
    if (
        row["port"] != native.port
        or row["started"] != native.started_at
        or not row["address"]
        or not ipaddress.ip_address(row["address"]).is_loopback
    ):
        raise ValueError("Offline maintenance requires the exact local PostgreSQL postmaster")
    return digest(
        {
            "system_identifier": native.system_identifier,
            "database": row["database"],
            "database_oid": row["database_oid"],
            "schema": schema,
            "schema_oid": row["schema_oid"],
        }
    )


def topology_base(row: ForgeRuntimeTopologyEntity, attestation: ForgeRuntimeTopologyEntity | None) -> Data:
    if row.id != TOPOLOGY_ID or row.mode != "combined" or row.data.get("mode") != "combined":
        raise ValueError("Offline relocation requires the unchanged combined topology")
    value = copy.deepcopy(row.data)
    if "storage_identity" not in value:
        if (
            attestation is None
            or attestation.id != STORAGE_ATTESTATION_ID
            or attestation.mode != row.mode
            or attestation.data.get("topology_id") != TOPOLOGY_ID.hex
            or any(attestation.data.get(key) != value.get(key) for key in ("mode", "owner_uid", "storage_binding"))
            or type(attestation.data.get("storage_identity")) is not str
        ):
            raise ValueError("Relocation requires the exact retained native storage attestation")
        value["storage_identity"] = attestation.data["storage_identity"]
        value["legacy_attestation_digest"] = digest(attestation.data)
    value["immutable_topology_digest"] = digest(row.data)
    return value


def native_location_rows(rows: list[ForgeRuntimeTopologyEntity]) -> list[Data]:
    result = []
    for row in rows:
        if row.mode != "combined" or row.id != location_record_id(row.data):
            raise ValueError("Native location history differs from its exact native record")
        result.append(copy.deepcopy(row.data))
    return result


async def preserved_native_digest(session: AsyncSession, schema: str, *, lock: bool = False) -> str:
    tables = list(
        await session.scalars(
            text(
                "SELECT tablename FROM pg_tables WHERE schemaname=:schema "
                "AND left(tablename,6)='forge_' AND tablename<>'forge_runtime_topology' ORDER BY tablename"
            ),
            {"schema": schema},
        )
    )
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", schema) or any(
        not re.fullmatch(r"forge_[a-z0-9_]+", name) for name in tables
    ):
        raise ValueError("Native relocation requires exact existing factory tables")
    value = hashlib.sha256()
    for name in tables:
        if lock:
            await session.execute(text(f'LOCK TABLE "{schema}"."{name}" IN SHARE MODE'))
        value.update(_json({"table": name}) + b"\n")
        rows = await session.stream(
            text(f'SELECT row_to_json(r)::text FROM "{schema}"."{name}" r ORDER BY row_to_json(r)::text')
        )
        row: str
        async for row in rows.scalars():
            raw: bytes = row.encode()
            value.update(len(raw).to_bytes(8, "big") + raw)
    return value.hexdigest()


@dataclass(frozen=True)
class PreparedNativeLocation:
    candidate: NativeLocationCandidateV1
    identity: NativeStorageIdentity
    assert_files_current: Callable[[], Awaitable[None]]


class NativeLocationMaintenance:
    def __init__(self, store: Store, records: ForgeRuntimeTopologyRepository | None = None) -> None:
        self.store = store
        self.records = records or ForgeRuntimeTopologyRepository()

    async def status(self, request_id: str) -> Data:
        if not re.fullmatch(r"[a-f0-9]{32}", request_id):
            raise ValueError("Native maintenance requires the exact original request identifier")
        identity = read_storage_identity(self.store.path.parent)
        async with self.store._unit() as session:
            row = await self.records.find_by_id(TOPOLOGY_ID)
            if row is None:
                raise ValueError("Offline maintenance cannot initialize a missing native topology")
            history = native_location_rows(await self.records.native_locations())
            base = topology_base(row, await self.records.find_by_id(STORAGE_ATTESTATION_ID))
            if not history:
                resolve_native_location(base, [], identity, "0" * 64)
                return {"status": "not_admitted", "request_id": request_id}
            fingerprint = await database_fingerprint(
                session, self.store.schema, Path(history[-1]["candidate"]["postgres_data_directory"])
            )
            try:
                resolve_native_location(base, history, identity, fingerprint)
            except NativeLocationUnknown as unknown:
                return {"status": "unknown", "request_id": unknown.request_id, "intent_digest": unknown.intent_digest}
            saved = next(
                (
                    event
                    for event in history
                    if event["kind"] == "native_location_receipt" and event["request"]["request_id"] == request_id
                ),
                None,
            )
            return (
                {"status": "completed", "request_id": request_id, "receipt": saved}
                if saved
                else {
                    "status": "not_admitted",
                    "request_id": request_id,
                }
            )

    async def prepare(
        self,
        plan: NativeLocationPlanV1,
        manifest: NativeLocationFilesV1,
        postgres_data: Path,
        scratch: Path,
        budget: SourceCatalogBudget,
    ) -> PreparedNativeLocation:
        from firefly_forge.checkpoint_work_duration import checkpoint_payload
        from firefly_forge.native_location_proof import prepare_location_files
        from firefly_forge.persistence import ForgeJournalEntity, ForgeRunEntity

        if str(self.store.path.parent) != plan.target_storage:
            raise ValueError("Offline maintenance must use the explicit physical target root")
        identity = read_storage_identity(Path(plan.target_storage))
        runs, workspaces = {}, {}
        async with self.store._unit() as session:
            await self._exclusive(session)
            row = await self.records.find_by_id(TOPOLOGY_ID, lock=LockMode.PESSIMISTIC_WRITE)
            if row is None:
                raise ValueError("Offline relocation requires its original existing topology")
            base = topology_base(row, await self.records.find_by_id(STORAGE_ATTESTATION_ID))
            fingerprint = await database_fingerprint(session, self.store.schema, postgres_data)
            history = native_location_rows(await self.records.native_locations())
            state = resolve_native_location(base, history, identity, fingerprint)
            if state.storage_binding != location_binding(plan.source_storage):
                raise ValueError("The requested source is not the current exclusive native location leaf")
            preserved = await preserved_native_digest(session, self.store.schema, lock=True)
            for selected in plan.workspaces:
                run = await session.get(ForgeRunEntity, UUID(hex=selected.run_id))
                if run is None or run.data.get("id") != selected.run_id:
                    raise ValueError("Selected relocation run is not an existing native run")
                rows = list(
                    await session.scalars(
                        select(ForgeJournalEntity)
                        .where(ForgeJournalEntity.run_id == run.id)
                        .order_by(ForgeJournalEntity.sequence)
                    )
                )
                entries = [{**copy.deepcopy(value.data), "digest": value.digest} for value in rows]
                if not Journal.verify_chain(entries) or any(
                    _json(
                        [
                            entry.get("sequence"),
                            entry.get("run_id"),
                            entry.get("kind"),
                            entry.get("idempotency_key"),
                            entry.get("task_id"),
                            entry.get("previous_hash"),
                            value.data.get("digest"),
                        ]
                    )
                    != _json(
                        [
                            value.sequence,
                            selected.run_id,
                            value.kind,
                            value.idempotency_key,
                            value.task_id,
                            value.previous_hash,
                            value.digest,
                        ]
                    )
                    for value, entry in zip(rows, entries, strict=True)
                ):
                    raise ValueError("Relocation requires the exact complete native journal chain")
                checkpoints = [entry for entry in entries if entry["kind"] == "agent_checkpoint"]
                if not checkpoints:
                    raise ValueError("Selected native workspace lacks an exact retained checkpoint source binding")
                runs[selected.run_id] = copy.deepcopy(run.data)
                workspaces[selected.run_id] = checkpoint_payload(checkpoints[-1]["payload"]).invocation.workspace
        files = await prepare_location_files(self.store, plan, manifest, runs, workspaces, scratch, budget)
        candidate = {
            "version": 1,
            "kind": "native_location_candidate",
            "plan": plan.model_dump(mode="json"),
            "postgres_data_directory": str(postgres_data),
            "owner_uid": os.getuid(),
            "base_topology_digest": digest(base),
            "database_fingerprint": fingerprint,
            "storage_identity_sha256": identity.sha256,
            "previous_receipt_digest": state.receipt_digest,
            "preserved_native_digest": preserved,
            "filesystem_proof_digest": files.digest,
            "execution_authorized": False,
        }
        candidate["digest"] = digest(candidate)
        identity.fence()
        return PreparedNativeLocation(
            NativeLocationCandidateV1.model_validate(candidate), identity, files.assert_current
        )

    async def _exclusive(self, session: AsyncSession) -> None:
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"),
            {"key": "forge:runtime-topology:" + self.store.schema},
        )
        if not await session.scalar(
            text("SELECT pg_try_advisory_xact_lock(hashtextextended(:key,0))"),
            {"key": "forge:execution:" + self.store.schema},
        ):
            raise ValueError("Stop the existing native execution owner before offline location maintenance")

    async def commit(self, prepared: PreparedNativeLocation, acknowledgment: NativeLocationAcknowledgmentV1) -> Data:
        candidate = NativeLocationCandidateV1.model_validate(prepared.candidate.model_dump(mode="json"))
        acknowledgment = NativeLocationAcknowledgmentV1.model_validate(acknowledgment.model_dump(mode="json"))
        if (
            acknowledgment.candidate_digest != candidate.digest
            or candidate.owner_uid != os.getuid()
            or str(self.store.path.parent) != candidate.plan.target_storage
            or prepared.identity.sha256 != candidate.storage_identity_sha256
        ):
            raise ValueError("Offline maintenance requires its exact reviewed candidate and local storage owner")
        async with self.store._unit() as session:
            await self._exclusive(session)
            row = await self.records.find_by_id(TOPOLOGY_ID, lock=LockMode.PESSIMISTIC_WRITE)
            if row is None:
                raise ValueError("Offline relocation cannot initialize a new topology")
            base = topology_base(row, await self.records.find_by_id(STORAGE_ATTESTATION_ID))
            history = native_location_rows(await self.records.native_locations())
            fingerprint = await database_fingerprint(
                session, self.store.schema, Path(candidate.postgres_data_directory)
            )
            state = resolve_native_location(base, history, prepared.identity, fingerprint)
            saved = [event for event in history if event["request"]["request_id"] == acknowledgment.request_id]
            if saved:
                if (
                    len(saved) != 2
                    or saved[-1]["kind"] != "native_location_receipt"
                    or _json(saved[-1]["candidate"]) != _json(candidate.model_dump(mode="json"))
                    or _json(saved[-1]["request"]) != _json(acknowledgment.model_dump(mode="json"))
                ):
                    raise ValueError("The original native maintenance request has another or unknown outcome")
                return saved[-1]
            if (
                digest(base) != candidate.base_topology_digest
                or fingerprint != candidate.database_fingerprint
                or state.receipt_digest != candidate.previous_receipt_digest
                or state.storage_binding != location_binding(candidate.plan.source_storage)
                or await preserved_native_digest(session, self.store.schema, lock=True)
                != candidate.preserved_native_digest
            ):
                raise ValueError("Native location candidate changed before its exclusive transfer")
            await prepared.assert_files_current()
            previous = history[-1]["digest"] if history else None
            intent = location_event(
                "native_location_intent",
                len(history) + 1,
                acknowledgment.model_dump(mode="json"),
                candidate.model_dump(mode="json"),
                previous,
                None,
                prepared.identity,
            )
            receipt = location_event(
                "native_location_receipt",
                len(history) + 2,
                acknowledgment.model_dump(mode="json"),
                candidate.model_dump(mode="json"),
                intent["digest"],
                intent["digest"],
                prepared.identity,
            )
            for event in (intent, receipt):
                await self.records.save(
                    ForgeRuntimeTopologyEntity(id=location_record_id(event), mode="combined", data=event)
                )
            await session.flush()
            await prepared.assert_files_current()
            prepared.identity.fence()
            if await preserved_native_digest(session, self.store.schema) != candidate.preserved_native_digest:
                raise ValueError("Native location maintenance cannot change retained factory history")
            return receipt
