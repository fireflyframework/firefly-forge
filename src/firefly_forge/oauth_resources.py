# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Native grant custody for exact, currently authorized resource requests."""

from __future__ import annotations

import hmac
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal
from uuid import UUID

import httpx2
from pydantic import BaseModel, ConfigDict, Field
from pyfly.container import service

from firefly_forge.connection_oauth import ConnectionOAuth, Data
from firefly_forge.identity import Principal
from firefly_forge.journal import digest
from firefly_forge.project_access import ProjectAccess
from firefly_forge.project_persistence import ForgeProjectEntity


class ResourceOwner(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    user_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    authentication_method: Literal["oidc_session", "oidc_bearer", "local_bootstrap"]
    global_admin: bool
    api_audience: str | None = Field(max_length=200)


class ResourceBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    version: Literal[1]
    provider: str = Field(pattern=r"^[a-z][a-z0-9-]{0,63}$")
    connection_name: str = Field(pattern=r"^[a-z][a-z0-9-]{0,63}$")
    connection_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    registration_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    registration_revision: int = Field(ge=1, le=2_147_483_647)
    policy_revision: int = Field(ge=0, le=2_147_483_647)
    policy_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    project_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    issuer: str = Field(min_length=1, max_length=2048)
    resource: str = Field(min_length=1, max_length=2048)
    scopes: list[str] = Field(min_length=1, max_length=64)
    grant_family: str = Field(pattern=r"^[a-f0-9]{32}$")
    minimum_grant_revision: int = Field(ge=1, le=2_147_483_647)
    owner_actor: ResourceOwner
    signature: str = Field(pattern=r"^[a-f0-9]{64}$")


@dataclass(frozen=True)
class _ResourceCredential:
    bearer: str = field(repr=False)
    observation: Data


@service
class OAuthResources:
    def __init__(self, connections: ConnectionOAuth, access: ProjectAccess) -> None:
        self.connections = connections
        self.store = connections.store
        self.access = access

    def _signature(self, packet: Data) -> str:
        raw = json.dumps(packet, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        if len(raw) > 16_000:
            raise ValueError("OAuth resource binding exceeds its bound")
        return hmac.digest(self.connections.matcher.key, b"resource-binding:v1\0" + raw, "sha256").hex()

    def _checked(self, packet: Data) -> Data:
        result = ResourceBinding.model_validate(packet).model_dump()
        expected = self._signature({key: value for key, value in result.items() if key != "signature"})
        if not hmac.compare_digest(result["signature"], expected):
            raise ValueError("OAuth resource binding context is invalid")
        return result

    async def bind(self, name: str, actor: Principal, *, project_id: str, resource: str) -> Data:
        if not re.fullmatch(r"[a-f0-9]{32}", project_id):
            raise ValueError("Select an exact resource project")
        async with self.connections._locked(name) as session:
            await self.connections._authority(session, actor)
            row = await self.connections._required(name)
            registration = await self.connections._registration(session, row)
            policy = registration.data["policy"]
            if registration.data.get("project_id") != project_id:
                raise ValueError("OAuth resource requires its exact configured project")
            if policy["resource"] != resource:
                raise ValueError("OAuth resource differs from its admitted endpoint")
            grant = await self.connections._grant(session, row)
            if grant is None or grant.actor_id != actor.id:
                raise ValueError("The current grant owner must admit this resource binding")
            packet: Data = {
                "version": 1,
                "provider": registration.data["provider"],
                "connection_name": name,
                "connection_id": row.id.hex,
                "registration_id": registration.id.hex,
                "registration_revision": row.revision,
                "policy_revision": registration.data.get("policy_revision", 0),
                "policy_digest": registration.data.get("policy_digest", digest(policy)),
                "project_id": project_id,
                "issuer": policy["issuer"],
                "resource": resource,
                "scopes": policy["scopes"],
                "grant_family": grant.family,
                "minimum_grant_revision": grant.revision,
                "owner_actor": ProjectAccess.actor_snapshot(actor),
            }
            binding = self._checked({**packet, "signature": self._signature(packet)})
            await self._admission(binding, actor=actor)
            await self.store.admit_credentials(binding)
            return binding

    async def _admission(
        self,
        binding: Data,
        *,
        run_id: str | None = None,
        actor: Principal | None = None,
        repository_id: str | None = None,
    ) -> _ResourceCredential:
        binding = self._checked(binding)
        async with self.connections._locked(binding["connection_name"]) as session:
            # Policy writers lock current administrator authority before the policy row.
            if actor is not None:
                await self.connections._authority(session, actor)
            row = await self.connections._required(binding["connection_name"])
            registration = await self.connections._registration(session, row)
            policy = registration.data["policy"]
            if (
                registration.data["provider"] != binding["provider"]
                or row.id.hex != binding["connection_id"]
                or registration.id.hex != binding["registration_id"]
                or row.revision != binding["registration_revision"]
                or registration.data.get("project_id") != binding["project_id"]
                or registration.data.get("policy_revision", 0) != binding["policy_revision"]
                or registration.data.get("policy_digest", digest(policy)) != binding["policy_digest"]
                or policy["resource"] != binding["resource"]
                or policy["issuer"] != binding["issuer"]
                or policy["scopes"] != binding["scopes"]
            ):
                raise ValueError("OAuth resource registration or policy context changed")
            if row.status != "authorized_unverified" or self.connections._effect_block(
                row, await self.connections._effect_packets(session, row)
            ):
                raise ValueError("OAuth resource requires an authorized grant without unresolved effects")
            grant = await self.connections._grant(session, row)
            if (
                grant is None
                or grant.family != binding["grant_family"]
                or grant.revision < binding["minimum_grant_revision"]
                or grant.actor_id != UUID(binding["owner_actor"]["user_id"])
                or grant.expires_at <= await self.connections._time(session)
                or grant.data["issuer"] != binding["issuer"]
                or grant.data["resource"] != binding["resource"]
                or set(grant.data["scopes"]) != set(binding["scopes"])
            ):
                raise ValueError("OAuth resource grant is unavailable, expired or changed")
            await self.access.authorize_actor(binding["owner_actor"], binding["project_id"], "execute")
            if actor is not None:
                if actor.id != grant.actor_id:
                    raise ValueError("The current grant owner must authorize resource discovery")
            else:
                if not run_id or not re.fullmatch(r"[a-f0-9]{32}", run_id):
                    raise ValueError("OAuth resource dispatch requires an exact recorded run")
                run = await self.store.get_run(run_id)
                if not run.get("execution_actor") or run.get("project_id") != binding["project_id"]:
                    raise ValueError("OAuth resource requires the run's captured actor and project")
                project = await session.get(ForgeProjectEntity, UUID(binding["project_id"]))
                if (
                    project is None
                    or run["repository_id"]
                    not in {item["repository_id"] for item in project.data["definition"]["components"]}
                    or repository_id is not None
                    and run["repository_id"] != repository_id
                ):
                    raise ValueError("OAuth resource requires the run's current project repository binding")
                await self.access.authorize_execution(run)
            await self.store.admit_credentials(binding)
            payload = self.connections._open(grant.encrypted_data, self.connections._grant_context(row, grant))
            token = payload.get("access_token")
            if not isinstance(token, str) or len(token) > 8192 or not re.fullmatch(r"[A-Za-z0-9._~+/-]+={0,2}", token):
                raise ValueError("OAuth resource bearer contract is unavailable")
            return _ResourceCredential(
                token,
                {
                    "connection_id": row.id.hex,
                    "registration_id": registration.id.hex,
                    "registration_revision": row.revision,
                    "policy_revision": binding["policy_revision"],
                    "policy_digest": binding["policy_digest"],
                    "project_id": binding["project_id"],
                    "grant_family": grant.family,
                    "grant_revision": grant.revision,
                    "observed_at": (await self.connections._time(session)).isoformat(),
                },
            )

    async def authorize(self, binding: Data, *, run_id: str, repository_id: str) -> None:
        """Recheck current native resource authority without dispatch or a new observation."""
        await self._admission(binding, run_id=run_id, repository_id=repository_id)

    def transport(
        self,
        binding: Data,
        *,
        run_id: str | None = None,
        actor: Principal | None = None,
        inner: httpx2.AsyncBaseTransport | None = None,
        authority: Callable[[], None] | None = None,
        repository_id: str | None = None,
    ) -> _ResourceTransport:
        return _ResourceTransport(
            self,
            self._checked(binding),
            run_id,
            actor,
            inner or httpx2.AsyncHTTPTransport(retries=0),
            authority,
            repository_id,
        )


class _ResourceTransport(httpx2.AsyncBaseTransport):
    def __init__(
        self,
        resources: OAuthResources,
        binding: Data,
        run_id: str | None,
        actor: Principal | None,
        inner: httpx2.AsyncBaseTransport,
        authority: Callable[[], None] | None,
        repository_id: str | None,
    ) -> None:
        self.observations: list[Data] = []
        self.repository_id = repository_id
        self.authority = authority
        self.resources, self.binding, self.run_id, self.actor, self.inner = resources, binding, run_id, actor, inner

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        provider = self.resources.store.session_provider
        if provider is None or provider.current() is not None:
            raise ValueError("OAuth resource HTTP cannot dispatch inside an ambient native transaction")
        if str(request.url) != self.binding["resource"]:
            raise ValueError("OAuth resource request is outside its exact admitted endpoint")
        if len(self.observations) >= 12:
            raise ValueError("OAuth resource request observations exceed their bound")
        credential = await self.resources._admission(
            self.binding, run_id=self.run_id, actor=self.actor, repository_id=self.repository_id
        )
        if self.authority:
            self.authority()
        request.headers["Authorization"] = "Bearer " + credential.bearer
        self.observations.append(credential.observation)
        response = await self.inner.handle_async_request(request)
        try:
            await self.resources._admission(
                self.binding, run_id=self.run_id, actor=self.actor, repository_id=self.repository_id
            )
            if self.authority:
                self.authority()
        except BaseException:
            await response.aclose()
            raise
        return response

    async def aclose(self) -> None:
        await self.inner.aclose()
