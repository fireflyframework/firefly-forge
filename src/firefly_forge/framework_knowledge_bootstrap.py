# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Private managed-installation grant; never a fabricated native user identity."""

from __future__ import annotations

import asyncio
import hashlib
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from firefly_forge.framework_knowledge_models import OFFICIAL_REPOSITORIES
from firefly_forge.managed_knowledge import DEFAULT_ENDPOINT
from firefly_forge.product_provenance import content_digest as digest
from firefly_forge.project_access import AccessDenied

if TYPE_CHECKING:
    from firefly_forge.framework_knowledge import FrameworkKnowledge


@dataclass(frozen=True)
class PreparedManagedFrameworkGrant:
    manager: FrameworkKnowledge
    task: object
    settings_generation: object
    body: dict[str, Any]
    actor: dict[str, Any]
    require_owner: bool


CURRENT_MANAGED_FRAMEWORK_GRANT: ContextVar[PreparedManagedFrameworkGrant | None] = ContextVar(
    "managed_framework_grant", default=None
)


def managed_grant_body(manager: FrameworkKnowledge) -> dict[str, Any] | None:
    enabled = os.environ.get("FORGE_FRAMEWORK_KNOWLEDGE_AUTO_REFRESH", "true").lower()
    if enabled not in {"true", "false"}:
        raise ValueError("Automatic official framework refresh must be explicitly true or false")
    if enabled == "false":
        return None
    private = manager.store.configuration
    marker = private._read(private.directory / "managed-knowledge.json")
    if not marker:
        return None
    settings = manager.knowledge.settings()
    files = marker.get("files", {})
    expected = {"ca.crt", "server.crt", "server.key", "postgres-password", "service.json"}
    if (
        marker.get("version") != 1
        or set(files) != expected
        or any(
            not isinstance(v, str) or len(v) != 64 or any(c not in "0123456789abcdef" for c in v)
            for v in files.values()
        )
        or not settings["enabled"]
        or not settings["api_key"]
        or settings["endpoint"] != marker.get("endpoint", DEFAULT_ENDPOINT)
        or settings["tenant_id"] != "forge"
        or settings["framework_workspace"] != "frameworks"
        or hashlib.sha256(settings["ca_certificate"].encode()).hexdigest() != files["ca.crt"]
    ):
        raise AccessDenied("Managed official framework refresh requires its unchanged native installation grant")
    return {
        "version": 1,
        "scope": "official_framework_knowledge",
        "repositories": OFFICIAL_REPOSITORIES,
        "managed_manifest_digest": digest(marker),
        "connection_digest": digest(settings),
        "collector_version": 1,
    }


@asynccontextmanager
async def managed_framework_scope(
    manager: FrameworkKnowledge, *, require_owner: bool = True
) -> AsyncIterator[dict[str, Any] | None]:
    body = managed_grant_body(manager)
    if body is None:
        yield None
        return
    if require_owner:
        await manager.store.assert_ownership()
    actor = {"authentication_method": "managed_framework_knowledge", "grant_digest": digest(body)}
    async with manager.store._unit():
        row = await manager.records.by_key("bootstrap", actor["grant_digest"], lock=True)
        if row is None and require_owner:
            await manager._put("bootstrap", actor["grant_digest"], "pyfly", {"grant": body})
        elif row is None or row.data.get("grant") != body:
            raise AccessDenied("Managed official framework grant changed")
    prepared = PreparedManagedFrameworkGrant(
        manager, asyncio.current_task(), manager.store.configuration.settings_generation, body, actor, require_owner
    )
    token = CURRENT_MANAGED_FRAMEWORK_GRANT.set(prepared)
    try:
        yield actor
    finally:
        CURRENT_MANAGED_FRAMEWORK_GRANT.reset(token)


async def authorize_managed_framework(manager: FrameworkKnowledge, actor: dict[str, Any]) -> dict[str, Any]:
    prepared = CURRENT_MANAGED_FRAMEWORK_GRANT.get()
    if (
        prepared is None
        or prepared.manager is not manager
        or prepared.task is not asyncio.current_task()
        or prepared.actor != actor
        or prepared.settings_generation is not manager.store.configuration.settings_generation
    ):
        raise AccessDenied("Managed framework authority requires this native owner and prepared installation grant")
    async with manager.store._unit():
        row = await manager.records.by_key("bootstrap", actor["grant_digest"], lock=True)
        if row is None or row.data.get("grant") != prepared.body:
            raise AccessDenied("Managed official framework authority is unavailable")
        if prepared.require_owner:
            await manager.store.assert_ownership()
    return dict(actor)
