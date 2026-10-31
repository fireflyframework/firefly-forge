# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Best-effort observations of locally owned Engine stages, separate from admission."""

from __future__ import annotations

import asyncio
import logging
from contextvars import Context
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pyfly.container import service
from sqlalchemy import column, select, table

from firefly_forge.engine_activity_persistence import SCOPE_FIELDS, ForgeEngineActivityRepository
from firefly_forge.persistence import ForgeJournalEntity, ForgePlanEntity, ForgeTaskEntity
from firefly_forge.store import Store, identifier

log = logging.getLogger(__name__)


class EngineActivitySettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    heartbeat_seconds: float = Field(default=5, gt=0)
    stale_seconds: float = Field(default=20, gt=0)

    @model_validator(mode="after")
    def observation_window(self) -> EngineActivitySettings:
        if self.stale_seconds <= self.heartbeat_seconds:
            raise ValueError("Observation expiry must exceed the heartbeat interval")
        return self


@dataclass(frozen=True)
class LocalStage:
    row_id: UUID
    run_id: str
    key: str
    task: asyncio.Task[Any]
    ownership: object


@service
class EngineActivity:
    def __init__(
        self,
        store: Store,
        records: ForgeEngineActivityRepository,
        settings: EngineActivitySettings | None = None,
    ) -> None:
        self.store, self.records = store, records
        self.settings = settings or EngineActivitySettings()
        self.epoch = uuid4().hex
        self.scopes: dict[tuple[str, str], LocalStage] = {}
        self.poller: asyncio.Task[None] | None = None
        self.lock = asyncio.Lock()

    async def begin(self, run_id: str, task_id: str, key: str) -> None:
        try:
            task = asyncio.current_task()
            if task is None:
                return
            async with asyncio.timeout(1), self.lock:
                ownership = self.store.execution_ownership_token
                await self.store.assert_ownership()
                async with self.store._unit() as session:
                    run = await self.store.get_run(run_id)
                    actor = run.get("execution_actor") or {}
                    # Delegated SCM actors need their own current delegation projection.
                    if actor.get("authentication_method") not in {"local_bootstrap", "oidc_session", "oidc_bearer"}:
                        return
                    stage = await session.scalar(
                        select(ForgeTaskEntity).where(
                            ForgeTaskEntity.run_id == identifier(run_id),
                            ForgeTaskEntity.task_id == task_id,
                        )
                    )
                    if stage is None or stage.data.get("idempotency_key") != key:
                        return
                    intent = await session.scalar(
                        select(ForgeJournalEntity).where(
                            ForgeJournalEntity.run_id == identifier(run_id),
                            ForgeJournalEntity.task_id == task_id,
                            ForgeJournalEntity.idempotency_key == key,
                            ForgeJournalEntity.kind == "intent",
                        )
                    )
                    if intent is None:
                        return
                    plan = await session.scalar(
                        select(ForgePlanEntity)
                        .where(
                            ForgePlanEntity.run_id == identifier(run_id),
                        )
                        .order_by(ForgePlanEntity.revision.desc())
                        .limit(1)
                    )
                    if plan is None:
                        return
                    backend = table("pg_stat_activity", column("pid"), column("backend_start"), schema="pg_catalog")
                    started = await session.scalar(
                        select(backend.c.backend_start).where(
                            backend.c.pid == self.store._ownership_pid,
                        )
                    )
                    if started is None or ownership is not self.store.execution_ownership_token:
                        return
                    row = await self.records.begin(
                        {
                            "run_id": identifier(run_id),
                            "actor_id": identifier(actor["user_id"]),
                            "project_id": identifier(run["project_id"]) if run.get("project_id") else None,
                            "plan_id": stage.plan_id,
                            "current_plan_id": plan.id,
                            "plan_digest": plan.digest,
                            "task_id": task_id,
                            "attempt": stage.attempt,
                            "invocation_key": key,
                            "intent_digest": intent.digest,
                            "owner_epoch": self.epoch,
                            "owner_pid": self.store._ownership_pid,
                            "owner_started_at": started,
                            "scope": {
                                "repository_id": identifier(run["repository_id"]).hex,
                                **{name: run.get(name) for name in SCOPE_FIELDS},
                            },
                        },
                        self.settings.stale_seconds,
                        self.store.schema,
                    )
                    if row is not None:
                        self.scopes[(run_id, key)] = LocalStage(row, run_id, key, task, ownership)
                if row is not None and (self.poller is None or self.poller.done()):
                    self.poller = asyncio.create_task(self._poll(), context=Context())
        except Exception:
            log.warning("Engine stage observation unavailable; recorded task state remains unchanged")

    async def end(self, run_id: str, key: str | None = None) -> None:
        async with self.lock:
            selected = [
                value for value in self.scopes.values() if value.run_id == run_id and (key is None or value.key == key)
            ]
            for value in selected:
                self.scopes.pop((value.run_id, value.key), None)
            await self._retire(selected)

    async def _retire(self, scopes: list[LocalStage]) -> None:
        if not scopes:
            return
        try:
            # End only our exact rows; SQL fence/CAS prevents retiring a replacement.
            async with asyncio.timeout(1), self.store._unit():
                await self.records.end([value.row_id for value in scopes], self.epoch, self.store.schema)
        except Exception:
            log.warning("Engine stage observation could not end; its SQL-clock expiry remains in force")

    async def pulse(self) -> None:
        async with self.lock:
            ended = [value for value in self.scopes.values() if value.task.done() or value.task.cancelling()]
            for value in ended:
                self.scopes.pop((value.run_id, value.key), None)
            await self._retire(ended)
            if not self.scopes:
                return
            try:
                async with asyncio.timeout(1):
                    ownership = self.store.execution_ownership_token
                    await self.store.assert_ownership()
                    selected = [value for value in self.scopes.values() if value.ownership is ownership]
                    async with self.store._unit():
                        renewed = await self.records.renew(
                            [value.row_id for value in selected],
                            self.epoch,
                            self.settings.stale_seconds,
                            self.store.schema,
                        )
                kept = set(renewed)
                self.scopes = {key: value for key, value in self.scopes.items() if value.row_id in kept}
            except Exception:
                # A failed sample cannot be used to restart or extend expired custody.
                self.scopes.clear()
                log.warning("Engine stage observation renewal unavailable; current activity becomes unknown")

    async def _poll(self) -> None:
        while self.scopes:
            await asyncio.sleep(self.settings.heartbeat_seconds)
            await self.pulse()

    async def close(self) -> None:
        if self.poller is not None:
            self.poller.cancel()
            await asyncio.gather(self.poller, return_exceptions=True)
            self.poller = None
        async with self.lock:
            scopes = list(self.scopes.values())
            self.scopes.clear()
            await self._retire(scopes)
