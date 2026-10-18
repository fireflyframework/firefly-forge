# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Optional scoped Flyquery observations, isolated from execution and required knowledge."""

from __future__ import annotations

import asyncio
import copy
import importlib
import json
from itertools import islice
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID, uuid4

import httpx
from pyfly.container import service
from pyfly.data.transaction import current_unit_of_work
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from firefly_forge.analytics_models import AnalyticsConfigurationInput
from firefly_forge.analytics_persistence import (
    ForgeAnalyticsBindingEntity,
    ForgeAnalyticsBindingRepository,
    ForgeAnalyticsConfigurationReceiptEntity,
)
from firefly_forge.configuration import ConfigurationStore
from firefly_forge.identity import Principal
from firefly_forge.identity_persistence import ForgeIdentityUserEntity
from firefly_forge.journal import digest
from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied, ProjectAccess
from firefly_forge.project_persistence import ForgeProjectEntity
from firefly_forge.store import Store, identifier, now

Data = dict[str, Any]


class _ReadTransport(httpx.AsyncBaseTransport):
    def __init__(self, inner: httpx.AsyncBaseTransport, paths: set[str]) -> None:
        self.inner, self.paths, self.count = inner, paths, 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.count += 1
        if request.method != "GET" or request.url.path not in self.paths or request.url.query or self.count > 8:
            raise ValueError("Analytics transport accepts only admitted bounded GET paths")
        request.headers.pop("cookie", None)
        request.extensions["timeout"] = {"connect": 3.0, "read": 10.0, "write": 10.0, "pool": 3.0}
        return await self.inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self.inner.aclose()


@service
class AnalyticsConnection:
    def __init__(self, private: ConfigurationStore, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.private, self.transport = private, transport

    def redaction_settings(self) -> dict[str, str]:
        with self.private.lock:
            paths = list(islice(self.private.revisions.glob("analytics-*.json"), 4097))
            if len(paths) > 4096:
                raise ValueError("Analytics credential history exceeds its retention bound")
            registry = {}
            for path in paths:
                token = self.private._read(path).get("agent_token")
                if not isinstance(token, str) or not token:
                    raise ValueError("Analytics credential history is invalid")
                registry["FORGE_ANALYTICS_AGENT_TOKEN_" + path.stem] = token
            return registry

    def prepare(self, token: str) -> Data:
        with self.private.lock:
            if sum(1 for _ in self.private.revisions.glob("analytics-*.json")) >= 4096:
                raise ValueError("Analytics credential history requires a reviewed retention checkpoint")
            revision = "analytics-" + uuid4().hex
            payload = {"agent_token": token}
            self.private._write(self.private.revisions / (revision + ".json"), payload)
        return {"secret_revision": revision, "secret_digest": digest(payload)}

    def load(self, binding: Data) -> str:
        revision = binding["secret_revision"]
        if not isinstance(revision, str) or len(revision) != 42 or not revision.startswith("analytics-"):
            raise ValueError("Analytics credential revision is unavailable")
        identifier(revision[10:])
        payload = self.private._read(self.private.revisions / (revision + ".json"))
        if digest(payload) != binding["secret_digest"] or set(payload) != {"agent_token"}:
            raise ValueError("Analytics credential revision changed")
        token = payload["agent_token"]
        if not isinstance(token, str):
            raise ValueError("Analytics credential revision is invalid")
        return token

    async def read(self, binding: Data, operation: str, table_id: str | None, object_id: str | None) -> Data:
        # Import only after local authority, scope and credential admission.
        try:
            sdk = importlib.import_module("flyquery_sdk.agent_read_client")
        except ImportError:
            raise ValueError("Optional Flyquery 26.10.4 read SDK is unavailable") from None
        paths = (
            {"/api/v1/agent/datasets/" + binding["dataset_id"] + "/tables"}
            if operation == "catalog"
            else {"/api/v1/agent/tables/" + str(table_id)}
        )
        if object_id:
            paths.add("/api/v1/agent/schema-objects/" + object_id)
        transport = _ReadTransport(self.transport or httpx.AsyncHTTPTransport(retries=0, trust_env=False), paths)
        try:
            async with (
                asyncio.timeout(10),
                sdk.AgentReadClient(
                    base_url=binding["endpoint"],
                    tenant_id=binding["tenant_id"],
                    workspace_id=binding["workspace_id"],
                    dataset_id=binding["dataset_id"],
                    agent_token=self.load(binding),
                    timeout=10,
                    max_response_bytes=262144,
                    max_items=50,
                    transport=transport,
                ) as client,
            ):
                if operation == "catalog":
                    value = await client.list_tables()
                elif operation == "table":
                    value = await client.get_table(table_id)
                else:
                    value = await client.get_object(table_id, object_id)
                data: Data = value.model_dump(mode="json")
                if operation == "catalog" and (
                    type(data.get("total")) is not int or data["total"] != len(data["items"]) or data.get("offset") != 0
                ):
                    raise ValueError("Analytics catalog completeness metadata is unavailable")
                if len(json.dumps(data).encode()) > 1048576:
                    raise ValueError("Analytics observation exceeds its response bound")
                return data
        except (sdk.AgentReadError, httpx.HTTPError, TimeoutError, ValueError):
            raise ValueError("Analytics read unavailable; no retry, query or write was attempted") from None


@service
class ProjectAnalytics:
    def __init__(
        self,
        store: Store,
        access: ProjectAccess,
        connection: AnalyticsConnection,
        records: ForgeAnalyticsBindingRepository,
    ) -> None:
        self.store, self.access, self.connection, self.records = store, access, connection, records
        self.active: set[tuple[str, UUID]] = set()

    @staticmethod
    def _outside(actor: Principal) -> None:
        if current_unit_of_work() is not None:
            raise ValueError("Analytics acquisition requires no ambient native unit")
        if CURRENT_PRINCIPAL.get() != actor:
            raise AccessDenied("Analytics caller context changed")

    @asynccontextmanager
    async def _authority(self, project_id: str, actor: Principal, admin: bool = False) -> AsyncIterator[AsyncSession]:
        async with self.store._unit() as session:
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"),
                {"key": self.store.schema + ":members:" + project_id},
            )
            user = await session.scalar(
                select(ForgeIdentityUserEntity).where(ForgeIdentityUserEntity.id == actor.id).with_for_update()
            )
            if user is None or not user.enabled or (user.issuer, user.subject) != (actor.issuer, actor.subject):
                raise AccessDenied("Analytics requires a matching enabled native identity")
            if admin and not await self.access.current_administrator(actor):
                raise AccessDenied("Current factory administrator authority is required")
            await self.access.require_project(actor, project_id, "read")
            project = await session.scalar(
                select(ForgeProjectEntity).where(ForgeProjectEntity.id == identifier(project_id)).with_for_update()
            )
            if project is None:
                raise KeyError("Analytics project not found")
            if CURRENT_PRINCIPAL.get() != actor:
                raise AccessDenied("Analytics caller context changed")
            yield session

    async def _binding(self, project_id: str, actor: Principal, *, admin: bool = False) -> Data | None:
        async with self._authority(project_id, actor, admin):
            head = await self.records.for_project(identifier(project_id))
            return {**copy.deepcopy(head.data), "revision": head.revision} if head else None

    @staticmethod
    def _public(project_id: str, binding: Data | None) -> Data:
        capabilities = {
            "catalog": True,
            "table": True,
            "exact_schema_object": True,
            "full_table_schema": False,
            "prepared_results": False,
            "query_execution": False,
        }
        return {
            "project_id": project_id,
            "revision": (binding or {}).get("revision", 0),
            "status": "configured" if binding and binding["enabled"] else "disabled",
            "agent_token_configured": bool(binding),
            "configuration": {
                k: v for k, v in (binding or {}).items() if k not in {"secret_revision", "secret_digest", "revision"}
            },
            "capabilities": capabilities,
        }

    async def configuration(self, project_id: str, actor: Principal) -> Data:
        self._outside(actor)
        project_id = identifier(project_id).hex
        binding = await self._binding(project_id, actor, admin=True)
        self._outside(actor)
        return self._public(project_id, binding)

    async def configure(self, project_id: str, request: AnalyticsConfigurationInput, actor: Principal) -> Data:
        self._outside(actor)
        project_id = identifier(project_id).hex
        binding = await self._binding(project_id, actor, admin=True)
        if (binding or {}).get("revision", 0) != request.expected_revision or request.expected_revision >= 32:
            raise ValueError("Analytics configuration revision changed or history bound reached")
        self._outside(actor)
        secret = self.connection.prepare(request.agent_token.get_secret_value())
        data = {**request.model_dump(exclude={"agent_token", "expected_revision"}), **secret}
        async with self._authority(project_id, actor, True) as session:
            head = await self.records.for_project(identifier(project_id))
            if CURRENT_PRINCIPAL.get() != actor:
                raise AccessDenied("Analytics caller context changed")
            if (head.revision if head else 0) != request.expected_revision:
                raise ValueError("Analytics configuration revision changed")
            revision = request.expected_revision + 1
            if head is None:
                head = ForgeAnalyticsBindingEntity(project_id=identifier(project_id), revision=revision, data=data)
                session.add(head)
            else:
                head.revision, head.data = revision, data
            session.add(
                ForgeAnalyticsConfigurationReceiptEntity(
                    project_id=identifier(project_id),
                    revision=revision,
                    data={"actor_id": actor.id.hex, "at": now(), "configuration": data, "digest": digest(data)},
                )
            )
        self._outside(actor)
        return self._public(project_id, {**data, "revision": revision})

    async def _read(
        self,
        project_id: str,
        actor: Principal,
        operation: str,
        table_id: str | None = None,
        object_id: str | None = None,
    ) -> Data:
        self._outside(actor)
        project_id = identifier(project_id).hex
        table_id = str(UUID(table_id)) if table_id else None
        object_id = str(UUID(object_id)) if object_id else None
        key = (project_id, actor.id)
        if key in self.active:
            raise ValueError("One analytics read per project and actor is already active")
        self.active.add(key)
        try:
            binding = await self._binding(project_id, actor)
            if not binding or not binding["enabled"]:
                raise ValueError("Optional analytics is not configured")
            self._outside(actor)
            token = self.connection.load(binding)
            data = await self.connection.read(binding, operation, table_id, object_id)
            current = await self._binding(project_id, actor)
            self._outside(actor)
            if current != binding or self.connection.load(binding) != token:
                raise ValueError("Analytics configuration or credentials changed during read")
            return {
                "project_id": project_id,
                "configuration_revision": binding["revision"],
                "tenant_id": binding["tenant_id"],
                "workspace_id": binding["workspace_id"],
                "dataset_id": binding["dataset_id"],
                "observed_at": now(),
                "response_digest": digest(data),
                "snapshot_assurance": "reported_identity_only",
                "data": data,
            }
        finally:
            self.active.discard(key)

    async def catalog(self, project_id: str, actor: Principal) -> Data:
        return await self._read(project_id, actor, "catalog")

    async def table(self, project_id: str, table_id: str, actor: Principal) -> Data:
        return await self._read(project_id, actor, "table", table_id)

    async def schema_object(self, project_id: str, table_id: str, object_id: str, actor: Principal) -> Data:
        return await self._read(project_id, actor, "object", table_id, object_id)

    async def prepared(self, project_id: str, query_id: str, actor: Principal) -> Data:
        self._outside(actor)
        UUID(query_id)
        await self._binding(identifier(project_id).hex, actor)
        raise ValueError(
            "Flyquery 26.10.4 prepared results require a scoped agent GET query-history/result API "
            "and read SDK; currently unavailable"
        )
