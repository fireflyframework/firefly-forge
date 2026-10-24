# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Project defaults and immutable policy receipts for future Git deliveries."""

from __future__ import annotations

import copy
from contextlib import suppress
from typing import Any

from pyfly.data.relational.sqlalchemy import LockMode
from sqlalchemy import text

from firefly_forge.access_persistence import ForgeAccessEventEntity, ForgeAccessEventRepository
from firefly_forge.git_deliveries import delivery_digest
from firefly_forge.git_delivery_models import GitDeliveryPolicyInputV1, GitDeliveryPolicyV1, GitDeliveryPolicyViewV1
from firefly_forge.git_delivery_remote import draft_capability
from firefly_forge.identity_persistence import ForgeIdentityUserRepository
from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied, ProjectAccess
from firefly_forge.project_persistence import ForgeProjectRepository
from firefly_forge.store import identifier, now

Data = dict[str, Any]


class GitDeliveryPolicies:
    def __init__(self, store: Any, records: Any = None, events: Any = None) -> None:
        self.store = store
        self.records = records or ForgeProjectRepository()
        self.events = events or ForgeAccessEventRepository()

    async def _authority(self, project_id: str, *, configure: bool = False, remote: bool = False) -> Data:
        user = CURRENT_PRINCIPAL.get()
        if user is None:
            raise AccessDenied("Sign in to inspect or configure project Git deliveries")
        access = ProjectAccess(self.store)
        actor = access.actor_snapshot(user)
        if configure and (not await access.current_administrator(user) or user.authentication_method == "scm_mapping"):
            raise AccessDenied("A current factory administrator must configure Git delivery consent")
        await access.require_project(user, project_id, "members" if configure else "read")
        if remote:
            await access.require_project(user, project_id, "release")
        if CURRENT_PRINCIPAL.get() != user:
            raise AccessDenied("Git delivery configuration identity changed")
        return actor

    async def snapshot(self, project_id: str | None) -> Data | None:
        if not project_id:
            return None
        async with self.store._unit():
            row = await self.records.find_by_id(identifier(project_id))
            if row is None:
                raise KeyError("Project not found")
            saved = copy.deepcopy(row.data.get("git_delivery_policy"))
        return saved

    async def view(self, project_id: str) -> Data:
        actor = await self._authority(project_id)
        async with self.store._unit():
            row = await self.records.find_by_id(identifier(project_id))
            if row is None:
                raise KeyError("Project not found")
            project = copy.deepcopy(row.data)
        saved = project.get("git_delivery_policy") or {}
        policy = GitDeliveryPolicyV1.model_validate(saved.get("policy") or {})
        capabilities = []
        for repo_id in sorted({c["repository_id"] for c in project["definition"]["components"]}):
            repository = await self.store.repository(repo_id)
            supported = draft_capability(repository["provider"]) and bool(repository.get("connection_provider"))
            capabilities.append(
                {
                    "repository_id": repo_id,
                    "provider": repository["provider"],
                    "draft_updates": supported,
                    "reason": None
                    if supported
                    else (
                        "Connect this repository before enabling remote draft deliveries."
                        if draft_capability(repository["provider"])
                        else "This provider supports local checkpoints; its draft lifecycle is not implemented."
                    ),
                }
            )
        access = ProjectAccess(self.store)
        configure = False
        with suppress(AccessDenied):
            configure = bool(await access.authorize_actor(actor, project_id, "members"))
        # Project ownership alone does not authorize changing factory execution consent.
        principal = CURRENT_PRINCIPAL.get()
        configure = bool(configure and principal is not None and access.is_admin(principal))
        publish = False
        try:
            await access.authorize_actor(actor, project_id, "release")
            publish = True
        except AccessDenied:
            pass
        await self._authority(project_id)
        return GitDeliveryPolicyViewV1.model_validate(
            {
                "version": 1,
                "project_id": project_id,
                "policy": policy.model_dump(),
                "digest": saved.get("digest"),
                "request_id": saved.get("request_id"),
                "permissions": {"configure": configure, "publish": publish},
                "remote_capabilities": capabilities,
            }
        ).model_dump(mode="json")

    async def configure(self, project_id: str, body: GitDeliveryPolicyInputV1) -> Data:
        actor = await self._authority(
            project_id, configure=True, remote=body.policy.enabled and body.policy.remote == "draft_pr"
        )
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
                raise AccessDenied("The Git delivery configuration identity is unavailable")
            row = await self.records.find_by_id(identifier(project_id), lock=LockMode.PESSIMISTIC_WRITE)
            if row is None:
                raise KeyError("Project not found")
            previous = row.data.get("git_delivery_policy") or {}
            existing = await self.events.find_by_id(identifier(body.request_id))
            if existing is not None:
                if (
                    existing.project_id.hex != project_id
                    or existing.data.get("input") != incoming
                    or existing.data.get("actor") != actor
                ):
                    raise ValueError("Policy request identity belongs to another project, actor or policy")
                receipt = copy.deepcopy(existing.data)
            else:
                if body.expected_previous_digest != previous.get("digest"):
                    raise ValueError("Git delivery policy changed; review the current policy before saving")
                if body.policy.enabled and body.policy.remote == "draft_pr":
                    for component in row.data["definition"]["components"]:
                        repository = await self.store.repository(component["repository_id"])
                        if not draft_capability(repository["provider"]) or not repository.get("connection_provider"):
                            raise ValueError(
                                "Remote checkpoints require a connected provider with an explicit draft lifecycle"
                            )
                await self._authority(
                    project_id, configure=True, remote=body.policy.enabled and body.policy.remote == "draft_pr"
                )
                receipt = {
                    "version": 1,
                    "project_id": project_id,
                    "request_id": body.request_id,
                    "previous_digest": previous.get("digest"),
                    "policy": body.policy.model_dump(mode="json"),
                    "actor": actor,
                    "created_at": now(),
                }
                receipt["digest"] = delivery_digest(receipt)
                await self.events.save(
                    ForgeAccessEventEntity(
                        id=identifier(body.request_id),
                        project_id=identifier(project_id),
                        data={"kind": "git_delivery_policy", "input": incoming, **receipt},
                    )
                )
                row.data = {**row.data, "git_delivery_policy": receipt}
                await self.records.save(row)
        view = await self.view(project_id)
        return {**view, "policy": receipt["policy"], "digest": receipt["digest"], "request_id": receipt["request_id"]}
