# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Explicit factory policy renews ownership time, never an execution allowance."""

from __future__ import annotations

import uuid
from typing import Any, Literal

from pydantic import Field
from pyfly.container import service
from sqlalchemy import text

from firefly_forge.bridge_completion_models import StrictRecord
from firefly_forge.configuration import ConfigurationStore
from firefly_forge.journal import digest
from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied, ProjectAccess
from firefly_forge.store import Store


class ContinuityLeasePolicyV1(StrictRecord):
    version: Literal[1]
    enabled: bool = False
    max_window_seconds: int = Field(default=60, ge=1, le=7200)
    max_tickets_per_run: int = Field(default=50, ge=1)
    max_pending_tickets: int = Field(default=500, ge=1)


def policy_snapshot(private: ConfigurationStore) -> dict[str, Any]:
    with private.lock:
        record = private._read(private.directory / "invalid-read-continuity-policy.json")
    value = {
        "version": 1,
        "revision": record.get("revision"),
        "settings": ContinuityLeasePolicyV1.model_validate(record.get("settings", {"version": 1})).model_dump(),
    }
    return {**value, "digest": digest(value)}


def write_policy(private: ConfigurationStore, policy: ContinuityLeasePolicyV1) -> dict[str, Any]:
    policy = ContinuityLeasePolicyV1.model_validate(policy.model_dump())
    with private.lock:
        private._write(
            private.directory / "invalid-read-continuity-policy.json",
            {"revision": uuid.uuid4().hex, "settings": policy.model_dump()},
        )
    return policy_snapshot(private)


def lease_window(
    policy: ContinuityLeasePolicyV1,
    *,
    frozen_timeout_seconds: int,
    valid_for_seconds: int,
    now_ms: int,
    original_deadline_ms: int,
    active_deadline_ms: int | None = None,
) -> dict[str, Any]:
    if (
        not policy.enabled
        or type(valid_for_seconds) is not int
        or type(frozen_timeout_seconds) is not int
        or not 1 <= valid_for_seconds <= min(policy.max_window_seconds, frozen_timeout_seconds)
        or not 1 <= frozen_timeout_seconds <= 7200
        or type(now_ms) is not int
        or now_ms < 1
        or type(original_deadline_ms) is not int
        or original_deadline_ms < 1
    ):
        raise ValueError("The new ownership lease exceeds its explicit current policy or frozen timeout")
    if active_deadline_ms is not None and (type(active_deadline_ms) is not int or active_deadline_ms <= now_ms):
        raise ValueError("The current active execution time budget is exhausted")
    expires = (
        min(now_ms + valid_for_seconds * 1000, active_deadline_ms)
        if active_deadline_ms is not None
        else now_ms + valid_for_seconds * 1000
    )
    return {
        "original_deadline_ms": original_deadline_ms,
        "issued_at_ms": now_ms,
        "expires_at_ms": expires,
        "window_ms": expires - now_ms,
        "active_deadline_ms": active_deadline_ms,
    }


@service
class ContinuityLeasePolicies:
    def __init__(self, store: Store) -> None:
        self.store = store

    async def _actor(self) -> Any:
        actor = CURRENT_PRINCIPAL.get()
        if actor is None or not await ProjectAccess(self.store).current_administrator(actor):
            raise AccessDenied("Current factory administrator authority is required for continuity lease policy")
        return actor

    async def get(self) -> dict[str, Any]:
        await self._actor()
        return policy_snapshot(self.store.configuration)

    async def configure(self, policy: ContinuityLeasePolicyV1) -> dict[str, Any]:
        actor = await self._actor()
        async with self.store._unit() as session:
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"),
                {"key": "forge:execution-request-admission:" + self.store.schema},
            )
            if await self._actor() != actor:
                raise AccessDenied("Continuity lease policy administrator changed")
            return write_policy(self.store.configuration, policy)
