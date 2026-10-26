# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Current native configuration receipts for finite project campaign windows."""

from __future__ import annotations

import copy
from contextlib import suppress
from typing import Any

from pyfly.data.relational.sqlalchemy import LockMode
from sqlalchemy import text

from firefly_forge.access_persistence import ForgeAccessEventEntity, ForgeAccessEventRepository
from firefly_forge.campaign_execution_budget import CampaignExecutionPolicyInputV1, CampaignExecutionPolicyV1
from firefly_forge.identity_persistence import ForgeIdentityUserRepository
from firefly_forge.journal import digest
from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied, ProjectAccess
from firefly_forge.project_persistence import ForgeProjectRepository
from firefly_forge.store import identifier, now


class CampaignExecutionPolicies:
    def __init__(self, store: Any, *, records: Any = None, events: Any = None) -> None:
        self.store = store
        self.records = records or ForgeProjectRepository()
        self.events = events or ForgeAccessEventRepository()

    async def _authority(self, project_id: str, *, configure: bool = False) -> dict[str, Any]:
        user = CURRENT_PRINCIPAL.get()
        if user is None:
            raise AccessDenied("Sign in to inspect or configure campaign execution policy")
        access = ProjectAccess(self.store)
        actor = access.actor_snapshot(user)
        if configure and (user.authentication_method == "scm_mapping" or not await access.current_administrator(user)):
            raise AccessDenied("A current native factory administrator must configure campaign execution policy")
        await access.require_project(user, project_id, "members" if configure else "read")
        if CURRENT_PRINCIPAL.get() != user or access.actor_snapshot(user) != actor:
            raise AccessDenied("Campaign execution policy identity changed")
        return actor

    @staticmethod
    def _snapshot(project: dict[str, Any]) -> dict[str, Any]:
        saved = project.get("campaign_execution_policy")
        if saved and saved.get("digest") != digest({key: value for key, value in saved.items() if key != "digest"}):
            raise ValueError("The retained campaign execution policy does not verify")
        policy = CampaignExecutionPolicyV1.model_validate(saved["policy"] if saved else {})
        return {
            "policy": policy.model_dump(),
            "digest": saved["digest"] if saved else None,
            "effective_digest": saved["digest"] if saved else digest({"version": 1, "policy": policy.model_dump()}),
            "request_id": saved["request_id"] if saved else None,
        }

    async def snapshot(self, project_id: str) -> dict[str, Any]:
        async with self.store._unit():
            project = await self.records.find_by_id(identifier(project_id))
            if project is None:
                raise KeyError("Project not found")
            return self._snapshot(project.data)

    async def view(self, project_id: str) -> dict[str, Any]:
        await self._authority(project_id)
        result = await self.snapshot(project_id)
        configure = False
        with suppress(AccessDenied):
            await self._authority(project_id, configure=True)
            configure = True
        await self.store.admit_credentials(result)
        await self._authority(project_id)
        return {"version": 1, "project_id": project_id, **result, "permissions": {"configure": configure}}

    async def configure(self, project_id: str, body: CampaignExecutionPolicyInputV1) -> dict[str, Any]:
        actor = await self._authority(project_id, configure=True)
        incoming = body.model_dump(mode="json")
        await self.store.admit_credentials(incoming)
        async with self.store._unit() as session:
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"),
                {"key": self.store.schema + ":members:" + project_id},
            )
            identity = await ForgeIdentityUserRepository().find_by_id(
                identifier(actor["user_id"]), lock=LockMode.PESSIMISTIC_WRITE
            )
            if identity is None or not identity.enabled:
                raise AccessDenied("The campaign execution policy identity is unavailable")
            project = await self.records.find_by_id(identifier(project_id), lock=LockMode.PESSIMISTIC_WRITE)
            if project is None:
                raise KeyError("Project not found")
            current = project.data.get("campaign_execution_policy") or {}
            existing = await self.events.find_by_id(identifier(body.request_id))
            if existing is not None:
                if (
                    existing.project_id.hex != project_id
                    or existing.data.get("kind") != "campaign_execution_policy"
                    or existing.data.get("input") != incoming
                    or existing.data.get("actor") != actor
                ):
                    raise ValueError("Campaign policy request identity belongs to another actor, project or definition")
                receipt = copy.deepcopy(existing.data)
            else:
                if current.get("digest") != body.expected_previous_digest:
                    raise ValueError("Campaign execution policy changed; review it before saving")
                receipt = {
                    "version": 1,
                    "project_id": project_id,
                    "request_id": body.request_id,
                    "previous_digest": current.get("digest"),
                    "policy": body.policy.model_dump(),
                    "actor": actor,
                    "created_at": now(),
                }
                receipt["digest"] = digest(receipt)
                if await self._authority(project_id, configure=True) != actor:
                    raise AccessDenied("Campaign execution policy identity changed")
                await self.events.save(
                    ForgeAccessEventEntity(
                        id=identifier(body.request_id),
                        project_id=identifier(project_id),
                        data={"kind": "campaign_execution_policy", "input": incoming, **receipt},
                    )
                )
                project.data = {**project.data, "campaign_execution_policy": receipt}
                await self.records.save(project)
            if await self._authority(project_id, configure=True) != actor:
                raise AccessDenied("Campaign execution policy identity changed")
        return {
            **await self.view(project_id),
            "policy": receipt["policy"],
            "digest": receipt["digest"],
            "effective_digest": receipt["digest"],
            "request_id": receipt["request_id"],
        }
