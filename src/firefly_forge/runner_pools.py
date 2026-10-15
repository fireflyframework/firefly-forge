# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Durable local runner admission. Expiry never grants replay of possible effects."""

from __future__ import annotations

import copy
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from pyfly.container import service
from pyfly.data.relational.sqlalchemy import SessionProvider
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from firefly_forge.runner_pool_models import (
    FactoryFence,
    RunnerInvocation,
    RunnerPoolProfile,
    parse_invocation,
    pool_digest,
)
from firefly_forge.runner_pool_persistence import (
    ForgeRunnerDaemonEntity,
    ForgeRunnerDaemonRepository,
    ForgeRunnerEventEntity,
    ForgeRunnerEventRepository,
    ForgeRunnerInvocationEntity,
    ForgeRunnerInvocationRepository,
    ForgeRunnerPoolEntity,
    ForgeRunnerPoolRepository,
)

HELD = {"leased", "creating", "created", "starting", "running", "draining", "quarantined"}


@service
class RunnerPools:
    def __init__(
        self,
        sessions: SessionProvider,
        schema: str,
        *,
        factory_schema: str | None = None,
        daemons: ForgeRunnerDaemonRepository | None = None,
        pools: ForgeRunnerPoolRepository | None = None,
        invocations: ForgeRunnerInvocationRepository | None = None,
        events: ForgeRunnerEventRepository | None = None,
    ) -> None:
        for value in (schema, factory_schema or schema):
            if not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", value):
                raise ValueError("Runner schemas must be lowercase PostgreSQL identifiers")
        self.sessions, self.schema, self.factory_schema = sessions, schema, factory_schema or schema
        self.daemons = daemons or ForgeRunnerDaemonRepository()
        self.pools = pools or ForgeRunnerPoolRepository()
        self.invocations = invocations or ForgeRunnerInvocationRepository()
        self.events = events or ForgeRunnerEventRepository()

    @asynccontextmanager
    async def _unit(self) -> AsyncIterator[AsyncSession]:
        async with self.sessions.unit() as session:
            await session.execute(text(f'SET LOCAL search_path TO "{self.schema}"'))
            yield session

    async def initialize(self) -> None:
        async with self.sessions.unit() as session:
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"),
                {"key": "forge:runner-migration:" + self.schema},
            )
            await session.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{self.schema}"'))
            await session.execute(text(f'SET LOCAL search_path TO "{self.schema}"'))
            await session.execute(
                text(
                    "CREATE TABLE IF NOT EXISTS forge_runner_schema_migrations "
                    "(revision INTEGER PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
                )
            )
            revision = await session.scalar(text("SELECT max(revision) FROM forge_runner_schema_migrations"))
            if revision is not None and revision != 1:
                raise RuntimeError("Runner schema requires a different controller release")
            if revision is None:
                connection = await session.connection()
                tables = [
                    ForgeRunnerPoolEntity.metadata.tables[entity.__tablename__]
                    for entity in (
                        ForgeRunnerDaemonEntity,
                        ForgeRunnerPoolEntity,
                        ForgeRunnerInvocationEntity,
                        ForgeRunnerEventEntity,
                    )
                ]
                await connection.run_sync(lambda sync: ForgeRunnerPoolEntity.metadata.create_all(sync, tables=tables))
                await session.execute(text("INSERT INTO forge_runner_schema_migrations(revision) VALUES(1)"))

    async def _daemon(self, profile: RunnerPoolProfile, *, admission: bool) -> ForgeRunnerDaemonEntity:
        session = self.daemons._require_session()
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"),
            {"key": "forge:runner-daemon:" + self.schema + ":" + profile.daemon_id},
        )
        daemon = await self.daemons.identity(profile.daemon_id)
        fields = (
            "slots",
            "project_slots",
            "cpu_budget",
            "memory_budget_mb",
            "project_cpu_budget",
            "project_memory_budget_mb",
            "waiting_limit",
        )
        limits = {field: profile.model_dump(mode="json")[field] for field in fields}
        if daemon is None:
            daemon = ForgeRunnerDaemonEntity(
                id=uuid5(NAMESPACE_URL, "forge:runner-daemon:" + profile.daemon_id),
                daemon_id=profile.daemon_id,
                profile_id=profile.profile_id,
                data={"limits": limits, "last_project": None},
            )
            await self.daemons.save(daemon)
            await session.flush()
        if daemon.profile_id != profile.profile_id:
            raise RuntimeError("Only one administrator profile ID is supported per physical daemon")
        if admission and daemon.data["limits"] != limits:
            if await self.invocations.daemon_rows(daemon.id):
                raise RuntimeError(
                    "Administrator profile capacity envelope cannot change until all daemon requests drain"
                )
            daemon.data = {**daemon.data, "limits": limits}
        return daemon

    async def _pool(self, profile: RunnerPoolProfile, *, admission: bool = False) -> ForgeRunnerPoolEntity:
        daemon = await self._daemon(profile, admission=admission)
        row = await self.pools.revision(profile.revision)
        if row is None:
            row = ForgeRunnerPoolEntity(
                id=uuid5(NAMESPACE_URL, "forge:runner-pool:" + profile.revision),
                daemon_id=daemon.id,
                profile_id=profile.profile_id,
                revision=profile.revision,
                data={"profile": profile.model_dump(mode="json")},
            )
            await self.pools.save(row)
            await self.pools._require_session().flush()
        if row.data["profile"] != profile.model_dump(mode="json"):
            raise RuntimeError("Immutable administrator profile was changed")
        return row

    async def _event(self, row: ForgeRunnerInvocationEntity, kind: str, payload: dict[str, Any]) -> None:
        previous = await self.events.events(row.id)
        preceding = previous[-1].data["hash"] if previous else "0" * 64
        value = {
            "sequence": len(previous) + 1,
            "previous_hash": preceding,
            "kind": kind,
            "epoch": row.epoch,
            "database_time": (await self.invocations.database_time()).isoformat(),
            "payload": copy.deepcopy(payload),
        }
        value["hash"] = pool_digest(value)
        await self.events.save(ForgeRunnerEventEntity(invocation_id=row.id, sequence=value["sequence"], data=value))
        await self.events._require_session().flush()

    def _public(self, row: ForgeRunnerInvocationEntity) -> dict[str, Any]:
        return {
            "id": row.id.hex,
            "status": row.status,
            "owner_id": row.owner_id,
            "epoch": row.epoch,
            "request": copy.deepcopy(row.data["request"]),
            "receipt": copy.deepcopy(row.data.get("receipt")),
        }

    async def _owned(self, fence: FactoryFence) -> None:
        if fence.factory_schema != self.factory_schema or not await self.invocations.factory_owned(fence):
            raise RuntimeError("Exact factory database execution ownership was lost")

    async def submit(
        self, profile: RunnerPoolProfile, request: RunnerInvocation, *, fence: FactoryFence | None = None
    ) -> dict[str, Any]:
        profile.validate_invocation(request)
        async with self._unit():
            if fence is not None:
                await self._owned(fence)
            document = request.model_dump(mode="json")
            prior = await self.invocations.execution(request.execution_id)
            if prior is not None:
                if prior.data["request"] != document:
                    raise ValueError("Runner invocation request is immutable across every profile and daemon")
                return self._public(prior)
            pool = await self._pool(profile, admission=True)
            rows = await self.invocations.daemon_rows(pool.daemon_id)
            if sum(row.status == "queued" for row in rows) >= profile.waiting_limit:
                raise RuntimeError("Runner waiting queue is full")
            now = await self.invocations.database_time()
            row = ForgeRunnerInvocationEntity(
                pool_id=pool.id,
                execution_id=request.execution_id,
                status="queued",
                epoch=0,
                admission_expires_at=now + timedelta(seconds=profile.admission_seconds),
                data={"request": document, "submission_fence": fence.model_dump(mode="json") if fence else None},
            )
            await self.invocations.save(row)
            await self.invocations._require_session().flush()
            await self._event(row, "submitted", {"request_digest": pool_digest(document)})
            return self._public(row)

    async def claim(self, profile: RunnerPoolProfile, owner: str, fence: FactoryFence) -> dict[str, Any] | None:
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", owner):
            raise ValueError("Invalid controller owner identity")
        async with self._unit():
            pool = await self._pool(profile, admission=True)
            await self._owned(fence)
            rows = await self.invocations.daemon_rows(pool.daemon_id)
            continued = await self.events.continued_invocations([row.id for row in rows])
            reserved = continued | {row.id for row in rows if "continuation" in row.data}
            now = await self.invocations.database_time()
            for row in rows:
                if row.id in reserved:
                    continue
                if (
                    row.status in HELD
                    and row.status != "quarantined"
                    and (row.lease_expires_at is None or row.lease_expires_at < now)
                ):
                    row.status = "queued" if row.status == "leased" else "quarantined"
                    row.owner_id = None
                    await self._event(row, "lease_expired", {"status": row.status})
                if row.status == "queued" and row.admission_expires_at < now:
                    row.status = "cancelled"
                    await self._event(row, "admission_expired", {})
            for row in rows:
                bound = row.data.get("submission_fence")
                if row.status == "queued" and bound is not None and bound != fence.model_dump(mode="json"):
                    row.status = "cancelled"
                    await self._event(row, "submission_owner_lost", {})
            active = [row for row in rows if row.status in HELD]
            if len(active) >= profile.slots:
                return None
            requests = [parse_invocation(row.data["request"]) for row in active]
            daemon = await self.daemons.identity(profile.daemon_id)
            assert daemon is not None
            queue = [
                row for row in rows if row.status == "queued" and row.pool_id == pool.id and row.id not in reserved
            ]
            queue.sort(key=lambda row: parse_invocation(row.data["request"]).project_key == daemon.data["last_project"])
            for row in queue:
                value = parse_invocation(row.data["request"])
                same = [other for other in requests if other.project_key == value.project_key]
                if (
                    len(same) >= profile.project_slots
                    or any(other.repository_id == value.repository_id for other in requests)
                    or sum(other.cpus for other in requests) + value.cpus > profile.cpu_budget
                    or sum(other.memory_mb for other in requests) + value.memory_mb > profile.memory_budget_mb
                    or sum(other.cpus for other in same) + value.cpus > profile.project_cpu_budget
                    or sum(other.memory_mb for other in same) + value.memory_mb > profile.project_memory_budget_mb
                ):
                    continue
                row.owner_id, row.epoch, row.status = owner, row.epoch + 1, "leased"
                row.lease_expires_at = now + timedelta(seconds=profile.lease_seconds)
                row.data = {**row.data, "fence": fence.model_dump(mode="json")}
                daemon.data = {**daemon.data, "last_project": value.project_key}
                await self._event(row, "claimed", {"owner": owner, "fence": fence.model_dump(mode="json")})
                return self._public(row)
            return None

    async def claim_continuation(
        self,
        profile: RunnerPoolProfile,
        owner: str,
        fence: FactoryFence,
        execution_id: str,
        request_id: str,
        request_digest: str,
    ) -> dict[str, Any]:
        from firefly_forge.runner_continuation_authority import RunnerContinuationAuthority

        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", owner):
            raise ValueError("Invalid controller owner identity")
        if not re.fullmatch(r"[a-f0-9]{32}", execution_id) or not re.fullmatch(r"[a-f0-9]{32}", request_id):
            raise ValueError("Exact continuation identities are required")
        async with self._unit():
            pool = await self._pool(profile, admission=True)
            await self._owned(fence)
            row = await self.invocations.execution(execution_id)
            if row is None or row.pool_id != pool.id:
                raise KeyError("Original invocation not found in this profile")
            invocation = parse_invocation(row.data["request"])
            authority = RunnerContinuationAuthority(self)
            await authority.authorize(invocation, fence, request_id, request_digest)
            original = row.data.get("continuation")
            if original:
                if original["request_id"] != request_id or original["request_digest"] != request_digest:
                    raise ValueError("Invocation continuation identity is immutable")
                return copy.deepcopy(original)
            now = await self.invocations.database_time()
            events = await self.history(profile, execution_id)
            if (
                row.data.get("submission_fence") != fence.model_dump(mode="json")
                or row.admission_expires_at <= now
                or row.status not in {"queued", "leased"}
                or (row.status == "leased" and (row.lease_expires_at is None or row.lease_expires_at > now))
                or any(event["kind"].endswith("_intent") for event in events)
            ):
                raise ValueError("Original invocation cannot establish a pre-effect continuation")
            rows = await self.invocations.daemon_rows(pool.daemon_id)
            active = [other for other in rows if other.id != row.id and other.status in HELD]
            requests = [parse_invocation(other.data["request"]) for other in active]
            same = [other for other in requests if other.project_key == invocation.project_key]
            busy = (
                len(active) >= profile.slots
                or len(same) >= profile.project_slots
                or any(other.repository_id == invocation.repository_id for other in requests)
                or sum(other.cpus for other in requests) + invocation.cpus > profile.cpu_budget
                or sum(other.memory_mb for other in requests) + invocation.memory_mb > profile.memory_budget_mb
                or sum(other.cpus for other in same) + invocation.cpus > profile.project_cpu_budget
                or sum(other.memory_mb for other in same) + invocation.memory_mb > profile.project_memory_budget_mb
            )
            decision = {
                "execution_id": execution_id,
                "request_id": request_id,
                "request_digest": request_digest,
                "disposition": "refused" if busy else "accepted",
                "epoch": row.epoch if busy else row.epoch + 1,
            }
            row.data = {**row.data, "continuation": decision}
            if not busy:
                row.owner_id, row.epoch, row.status = owner, row.epoch + 1, "leased"
                row.lease_expires_at = now + timedelta(seconds=profile.lease_seconds)
                row.data = {**row.data, "fence": fence.model_dump(mode="json")}
            await self._event(
                row, "explicit_continuation", {**decision, "owner": owner, "fence": fence.model_dump(mode="json")}
            )
            await self.invocations.save(row)
            return {**copy.deepcopy(decision), **({"claim": self._public(row)} if not busy else {})}

    async def _claimed(
        self, profile: RunnerPoolProfile, row_id: str, owner: str, epoch: int, fence: FactoryFence
    ) -> ForgeRunnerInvocationEntity:
        pool = await self._pool(profile)
        rows = await self.invocations.rows(pool.id, row_id=UUID(row_id))
        row = next((row for row in rows if row.id == UUID(row_id)), None)
        await self._owned(fence)
        if (
            row is None
            or row.status not in HELD
            or row.status == "quarantined"
            or row.owner_id != owner
            or row.epoch != epoch
            or row.lease_expires_at is None
            or row.lease_expires_at <= await self.invocations.database_time()
            or row.data["fence"] != fence.model_dump(mode="json")
        ):
            raise RuntimeError("Runner lease is stale or expired")
        return row

    async def renew(self, profile: RunnerPoolProfile, row_id: str, owner: str, epoch: int) -> bool:
        async with self._unit():
            pool = await self._pool(profile)
            rows = await self.invocations.rows(pool.id, row_id=UUID(row_id))
            row = next((row for row in rows if row.id == UUID(row_id)), None)
            if (
                row is None
                or not row.data.get("fence")
                or not await self.invocations.factory_owned(FactoryFence.model_validate(row.data["fence"]))
            ):
                return False
            return await self.invocations.renew(row.id, owner, epoch, profile.lease_seconds)

    async def authorize(
        self, profile: RunnerPoolProfile, row_id: str, owner: str, epoch: int, fence: FactoryFence
    ) -> None:
        async with self._unit():
            await self._claimed(profile, row_id, owner, epoch, fence)

    async def effect_intent(
        self,
        profile: RunnerPoolProfile,
        row_id: str,
        owner: str,
        epoch: int,
        fence: FactoryFence,
        effect: str,
        *,
        payload: dict[str, Any] | None = None,
    ) -> None:
        transitions = {
            "create": ("leased", "creating"),
            "start": ("created", "starting"),
            "cleanup": (("creating", "created", "starting", "running", "draining"), "draining"),
        }
        if effect not in transitions:
            raise ValueError("Unsupported runner effect")
        async with self._unit():
            row = await self._claimed(profile, row_id, owner, epoch, fence)
            before, after = transitions[effect]
            if row.status not in ((before,) if isinstance(before, str) else before):
                raise RuntimeError("Runner effect may not be replayed")
            row.status = after
            await self._event(row, effect + "_intent", payload or {})

    async def record_effect(
        self,
        profile: RunnerPoolProfile,
        row_id: str,
        owner: str,
        epoch: int,
        fence: FactoryFence,
        kind: str,
        payload: dict[str, Any],
    ) -> None:
        if kind not in {"transfer_observed", "runner_create_intent", "artifacts_retained"}:
            raise ValueError("Unsupported runner lifecycle observation")
        async with self._unit():
            row = await self._claimed(profile, row_id, owner, epoch, fence)
            await self._event(row, kind, payload)

    async def finish(
        self,
        profile: RunnerPoolProfile,
        row_id: str,
        owner: str,
        epoch: int,
        fence: FactoryFence,
        receipt: dict[str, Any],
    ) -> None:
        if receipt.get("drained") is not True:
            raise ValueError("Capacity release requires an observed drained resource")
        async with self._unit():
            row = await self._claimed(profile, row_id, owner, epoch, fence)
            row.status = "finished"
            row.data = {**row.data, "receipt": copy.deepcopy(receipt)}
            await self._event(row, "drained", receipt)

    async def observe(self, profile: RunnerPoolProfile, execution_id: str) -> dict[str, Any]:
        async with self._unit():
            pool = await self._pool(profile)
            rows = await self.invocations.rows(pool.id, execution_id=execution_id)
            row = next((row for row in rows if row.execution_id == execution_id), None)
            if row is None:
                raise KeyError("Runner invocation not found")
            return self._public(row)

    async def effect_receipt(
        self,
        profile: RunnerPoolProfile,
        row_id: str,
        owner: str,
        epoch: int,
        fence: FactoryFence,
        effect: str,
        receipt: dict[str, Any],
    ) -> None:
        transitions = {"create": ("creating", "created"), "start": ("starting", "running")}
        if effect not in transitions:
            raise ValueError("Unsupported runner effect receipt")
        async with self._unit():
            row = await self._claimed(profile, row_id, owner, epoch, fence)
            before, after = transitions[effect]
            if row.status != before:
                raise RuntimeError("Runner effect receipt cannot be replayed")
            row.status = after
            row.data = {**row.data, effect + "_receipt": copy.deepcopy(receipt)}
            await self._event(row, effect + "_receipt", receipt)

    async def cancel_waiting(self, profile: RunnerPoolProfile, execution_id: str, fence: FactoryFence) -> None:
        async with self._unit():
            pool = await self._pool(profile)
            await self._owned(fence)
            rows = await self.invocations.rows(pool.id, execution_id=execution_id)
            if not rows:
                raise KeyError("Runner invocation not found")
            row = rows[0]
            if row.status not in {"queued", "cancelled"}:
                raise RuntimeError("Active runner cancellation requires backend drain")
            if row.status == "queued":
                row.status = "cancelled"
                await self._event(row, "waiting_cancelled", {})

    async def quarantine(
        self, profile: RunnerPoolProfile, row_id: str, owner: str, epoch: int, diagnostic: dict[str, Any]
    ) -> None:
        # A lost factory fence may retain diagnostics, but cannot release capacity.
        async with self._unit():
            pool = await self._pool(profile)
            rows = await self.invocations.rows(pool.id, row_id=UUID(row_id))
            if not rows or rows[0].owner_id != owner or rows[0].epoch != epoch or rows[0].status not in HELD:
                raise RuntimeError("Runner lease changed before diagnostic retention")
            row = rows[0]
            row.status = "quarantined"
            await self._event(row, "quarantined", diagnostic)

    async def reconciliation_observation(
        self, profile: RunnerPoolProfile, execution_id: str, fence: FactoryFence, receipt: dict[str, Any]
    ) -> None:
        async with self._unit():
            pool = await self._pool(profile)
            await self._owned(fence)
            rows = await self.invocations.rows(pool.id, execution_id=execution_id)
            if not rows or rows[0].status != "quarantined":
                raise RuntimeError("Only quarantined invocations retain reconciliation observations")
            await self._event(rows[0], "reconciliation_observation", receipt)

    async def reconcile(
        self, profile: RunnerPoolProfile, execution_id: str, fence: FactoryFence, receipt: dict[str, Any]
    ) -> None:
        if receipt.get("drained") is not True:
            raise ValueError("Reconciliation requires an observed drained resource")
        async with self._unit():
            pool = await self._pool(profile)
            await self._owned(fence)
            rows = await self.invocations.rows(pool.id, execution_id=execution_id)
            if not rows or rows[0].status != "quarantined":
                raise RuntimeError("Only quarantined invocations can be reconciled")
            row = rows[0]
            row.status = "finished"
            row.data = {**row.data, "receipt": copy.deepcopy(receipt)}
            await self._event(row, "reconciled", receipt)

    async def history(self, profile: RunnerPoolProfile, execution_id: str) -> list[dict[str, Any]]:
        async with self._unit():
            pool = await self._pool(profile)
            rows = await self.invocations.rows(pool.id, execution_id=execution_id)
            if not rows:
                raise KeyError("Runner invocation not found")
            events = await self.events.events(rows[0].id)
            result = []
            previous = "0" * 64
            for number, event in enumerate(events, 1):
                value = copy.deepcopy(event.data)
                digest = value.pop("hash")
                if value["sequence"] != number or value["previous_hash"] != previous or pool_digest(value) != digest:
                    raise RuntimeError("Runner event chain was changed")
                value["hash"] = digest
                result.append(value)
                previous = digest
            return result

    async def check_factory_fence(self, fence: FactoryFence) -> None:
        async with self._unit():
            await self._owned(fence)
