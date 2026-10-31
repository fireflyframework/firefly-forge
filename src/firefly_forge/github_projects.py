# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Native project/repository binding and retained board configuration admission."""

from __future__ import annotations

import copy
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import uuid4

from pyfly.container import service
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from firefly_forge.execution_request_persistence import (
    ForgeExecutionRequestEntity,
    ForgeExecutionRequestEventRepository,
)
from firefly_forge.github_projects_graphql import ENDPOINT, GitHubProjectsGraphQL
from firefly_forge.github_projects_models import (
    BoardBindingV1,
    BoardConnectionInputV1,
    BoardConnectionViewV1,
    BoardCreateInputV1,
    BoardCreationReceiptV1,
    BoardDiscoveryInputV1,
    BoardDiscoveryV1,
)
from firefly_forge.github_projects_persistence import (
    ForgeGitHubProjectsBindingEntity,
    ForgeGitHubProjectsBindingRepository,
    ForgeGitHubProjectsConfigurationEntity,
    ForgeGitHubProjectsCreationEntity,
    ForgeGitHubProjectsCreationReceiptEntity,
)
from firefly_forge.identity import Principal
from firefly_forge.journal import digest
from firefly_forge.oauth_resources import OAuthResources
from firefly_forge.persistence import ForgeRunEntity
from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied, Permission, ProjectAccess
from firefly_forge.project_persistence import ForgeProjectEntity
from firefly_forge.store import Store, identifier, now

Data = dict[str, Any]
KIND = "github_board"


@service
class GitHubProjects:
    def __init__(
        self,
        store: Store,
        access: ProjectAccess,
        resources: OAuthResources,
        records: ForgeGitHubProjectsBindingRepository,
    ) -> None:
        self.store, self.access, self.resources, self.records = store, access, resources, records
        self.remote = GitHubProjectsGraphQL(resources)

    async def authority(
        self, project_id: str, repository_id: str, actor: Principal, *, configure: bool = False
    ) -> None:
        if CURRENT_PRINCIPAL.get() != actor:
            raise AccessDenied("The authenticated board caller changed")
        permission: Permission = "release" if configure else "read"
        await self.access.require_project(actor, project_id, permission)
        await self.access.require_repository(actor, repository_id, permission)
        async with self.store._unit() as session:
            project = await session.get(ForgeProjectEntity, identifier(project_id))
            if project is None or repository_id not in {
                component["repository_id"] for component in project.data["definition"]["components"]
            }:
                raise ValueError("Select a repository currently registered in the original project")

    @asynccontextmanager
    async def locked(self, project_id: str, repository_id: str) -> AsyncIterator[AsyncSession]:
        async with self.store._unit() as session:
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"),
                {"key": "forge:execution-request-admission:" + self.store.schema},
            )
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"),
                {"key": self.store.schema + ":github-projects:" + project_id + ":" + repository_id},
            )
            yield session

    async def binding(self, project_id: str, repository_id: str) -> Data | None:
        async with self.store._unit() as session:
            row = await self.records.scoped(identifier(project_id), identifier(repository_id))
            if row is None:
                return None
            receipt = await session.scalar(
                select(ForgeGitHubProjectsConfigurationEntity).where(
                    ForgeGitHubProjectsConfigurationEntity.project_id == row.project_id,
                    ForgeGitHubProjectsConfigurationEntity.repository_id == row.repository_id,
                    ForgeGitHubProjectsConfigurationEntity.revision == row.revision,
                )
            )
            packet = copy.deepcopy(row.data)
            if (
                receipt is None
                or receipt.data != packet
                or digest({key: value for key, value in packet.items() if key != "digest"}) != packet.get("digest")
            ):
                raise ValueError("Current board configuration differs from its retained receipt")
            return packet

    async def discover(
        self, project_id: str, repository_id: str, body: BoardDiscoveryInputV1, actor: Principal
    ) -> BoardDiscoveryV1:
        await self.authority(project_id, repository_id, actor, configure=True)
        resource = await self.resources.bind(body.connection_name, actor, project_id=project_id, resource=ENDPOINT)
        result = await self.remote.discover(resource, actor, body, await self.store.repository(repository_id))
        await self.authority(project_id, repository_id, actor, configure=True)
        await self.store.admit_credentials(result.model_dump(mode="json"))
        await self.authority(project_id, repository_id, actor, configure=True)
        return result

    async def creation_result(
        self, project_id: str, repository_id: str, request_id: str, actor: Principal
    ) -> BoardCreationReceiptV1:
        await self.authority(project_id, repository_id, actor)
        async with self.locked(project_id, repository_id) as session:
            intent = await session.get(ForgeGitHubProjectsCreationEntity, identifier(request_id))
            if intent is None or intent.project_id.hex != project_id or intent.repository_id.hex != repository_id:
                raise KeyError("Board creation not found in this project/repository")
            receipt = await session.get(ForgeGitHubProjectsCreationReceiptEntity, identifier(request_id))
            if digest(intent.data["request"]) != intent.data["request_digest"] or (
                receipt is not None
                and (
                    receipt.creation_id != intent.id or receipt.data["request_digest"] != intent.data["request_digest"]
                )
            ):
                raise ValueError("Board creation differs from its immutable admission")
            result = BoardCreationReceiptV1(
                id=request_id,
                project_id=project_id,
                repository_id=repository_id,
                request_digest=intent.data["request_digest"],
                status="completed" if receipt else "unknown",
                board=receipt.data["board"] if receipt else None,
                created_at=intent.data["created_at"],
            )
        await self.store.admit_credentials(result.model_dump(mode="json"))
        await self.authority(project_id, repository_id, actor)
        return result

    async def create(
        self, project_id: str, repository_id: str, body: BoardCreateInputV1, actor: Principal
    ) -> BoardCreationReceiptV1:
        await self.authority(project_id, repository_id, actor, configure=True)
        incoming = {
            "project_id": project_id,
            "repository_id": repository_id,
            "input": body.model_dump(mode="json"),
            "actor": self.access.actor_snapshot(actor),
        }
        await self.store.admit_credentials(incoming)
        async with self.locked(project_id, repository_id) as session:
            previous = await session.get(ForgeGitHubProjectsCreationEntity, identifier(body.request_id))
            if previous is not None and previous.data["request"] != incoming:
                raise ValueError("Creation identity belongs to another body, project, repository or actor")
        if previous is not None:
            return await self.creation_result(project_id, repository_id, body.request_id, actor)
        resource = await self.resources.bind(body.connection_name, actor, project_id=project_id, resource=ENDPOINT)
        repository = await self.store.repository(repository_id)
        owner = await self.remote.create_owner(resource, actor, body, repository)
        scope_digest = digest(
            {
                "project_id": project_id,
                "repository_id": repository_id,
                "owner_id": owner["owner_id"],
                "title": body.title,
            }
        )
        async with self.locked(project_id, repository_id) as session:
            await self.resources._admission(resource, actor=actor)
            await self.authority(project_id, repository_id, actor, configure=True)
            if await self.store.repository(repository_id) != repository:
                raise ValueError("Registered repository changed during board creation admission")
            previous = await session.get(ForgeGitHubProjectsCreationEntity, identifier(body.request_id))
            if previous is not None:
                if previous.data["request"] != incoming:
                    raise ValueError("Creation identity belongs to another admission")
            else:
                unresolved = await session.scalar(
                    select(ForgeGitHubProjectsCreationEntity)
                    .outerjoin(
                        ForgeGitHubProjectsCreationReceiptEntity,
                        ForgeGitHubProjectsCreationReceiptEntity.creation_id == ForgeGitHubProjectsCreationEntity.id,
                    )
                    .where(
                        ForgeGitHubProjectsCreationEntity.scope_digest == scope_digest,
                        ForgeGitHubProjectsCreationReceiptEntity.id.is_(None),
                    )
                    .limit(1)
                )
                if unresolved is not None:
                    raise ValueError("A prior creation has an unknown outcome; read back before any new request")
                session.add(
                    ForgeGitHubProjectsCreationEntity(
                        id=identifier(body.request_id),
                        project_id=identifier(project_id),
                        repository_id=identifier(repository_id),
                        scope_digest=scope_digest,
                        data={
                            "request": incoming,
                            "request_digest": digest(incoming),
                            "resource": resource,
                            "repository": repository,
                            "owner": owner,
                            "created_at": now(),
                        },
                    )
                )
        if previous is not None:
            return await self.creation_result(project_id, repository_id, body.request_id, actor)
        await self.resources._admission(resource, actor=actor)
        await self.authority(project_id, repository_id, actor, configure=True)
        board = await self.remote.create_private(resource, actor, body, repository, owner)
        async with self.locked(project_id, repository_id) as session:
            await self.resources._admission(resource, actor=actor)
            await self.authority(project_id, repository_id, actor, configure=True)
            if await self.store.repository(repository_id) != repository:
                raise ValueError("Registered repository changed after board creation")
            session.add(
                ForgeGitHubProjectsCreationReceiptEntity(
                    id=identifier(body.request_id),
                    creation_id=identifier(body.request_id),
                    data={"request_digest": digest(incoming), "board": board},
                )
            )
        return await self.creation_result(project_id, repository_id, body.request_id, actor)

    async def view(self, project_id: str, repository_id: str, actor: Principal) -> BoardConnectionViewV1:
        await self.authority(project_id, repository_id, actor)
        binding = await self.binding(project_id, repository_id)
        can_configure = True
        try:
            await self.authority(project_id, repository_id, actor, configure=True)
        except AccessDenied:
            can_configure = False
        can_sync = can_configure
        can_configure = can_configure and await self.access.current_administrator(actor)
        latest = None
        async with self.store._unit() as session:
            row = await session.scalar(
                select(ForgeExecutionRequestEntity)
                .where(
                    ForgeExecutionRequestEntity.kind == KIND,
                    ForgeExecutionRequestEntity.data["request"]["project_id"].astext == project_id,
                    ForgeExecutionRequestEntity.data["request"]["repository_id"].astext == repository_id,
                )
                .order_by(ForgeExecutionRequestEntity.created_at.desc())
                .limit(1)
            )
            if row:
                from firefly_forge.github_projects_requests import checked_board_request

                if (
                    row.id.hex != row.data.get("id")
                    or row.kind != row.data.get("kind")
                    or row.run_id.hex != row.data.get("run_id")
                    or row.status != row.data.get("status")
                ):
                    raise ValueError("Latest board request scalar identity changed")
                checked_board_request(
                    row.data, [item.data for item in await ForgeExecutionRequestEventRepository().events(row.id)]
                )
                projection = row.data["request"]["projection"]
                latest = {
                    "id": row.id.hex,
                    "status": row.status,
                    **{
                        key: projection[key]
                        for key in ("operation", "delivery_scope", "source_checkpoint", "product_completion_verified")
                    },
                }
        ready = await self.resources.ready(binding["resource"]) if binding else False
        result = BoardConnectionViewV1.model_validate(
            {
                "version": 1,
                "project_id": project_id,
                "repository_id": repository_id,
                "binding": binding["public"] if binding else None,
                "permissions": {"configure": can_configure, "sync": can_sync},
                "status": "not_configured"
                if not binding
                else "authorization_required"
                if not ready
                else "unknown"
                if latest and latest["status"] == "unknown"
                else "conflict"
                if latest and latest["status"] == "conflict"
                else "ready",
                "latest_request": latest,
            }
        )
        await self.store.admit_credentials(result.model_dump(mode="json"))
        await self.authority(project_id, repository_id, actor)
        return result

    async def configure(
        self, project_id: str, repository_id: str, body: BoardConnectionInputV1, actor: Principal
    ) -> BoardConnectionViewV1:
        await self.authority(project_id, repository_id, actor, configure=True)
        incoming = body.model_dump(mode="json")
        async with self.store._unit() as session:
            old = await session.scalar(
                select(ForgeGitHubProjectsConfigurationEntity).where(
                    ForgeGitHubProjectsConfigurationEntity.project_id == identifier(project_id),
                    ForgeGitHubProjectsConfigurationEntity.repository_id == identifier(repository_id),
                    ForgeGitHubProjectsConfigurationEntity.request_id == identifier(body.request_id),
                )
            )
            if old is not None:
                if old.data["input"] != incoming or old.data["actor"] != self.access.actor_snapshot(actor):
                    raise ValueError("Configuration identity belongs to another body or actor")
                return await self.configuration_result(old.data, actor)
        resource = await self.resources.bind(body.connection_name, actor, project_id=project_id, resource=ENDPOINT)
        discovery_request = BoardDiscoveryInputV1.model_validate(
            {key: incoming[key] for key in ("version", "connection_name", "owner_kind", "owner", "number")}
        )
        repository = await self.store.repository(repository_id)
        discovery = await self.remote.discover(resource, actor, discovery_request, repository)
        fields = {field.id: field for field in discovery.fields}
        if (
            discovery.board.id != body.board_id
            or body.field_id not in fields
            or not set(body.state_options.values()) <= {option.id for option in fields[body.field_id].options}
        ):
            raise ValueError("Refresh discovery and map exact current board, field and option identifiers")
        packet = await self._save(project_id, repository_id, body, actor, resource, discovery, repository)
        return await self.configuration_result(packet, actor)

    async def configuration_result(self, packet: Data, actor: Principal) -> BoardConnectionViewV1:
        result = BoardConnectionViewV1.model_validate(
            {
                "version": 1,
                "project_id": packet["project_id"],
                "repository_id": packet["repository_id"],
                "binding": packet["public"],
                "permissions": {"configure": True, "sync": True},
                "status": "ready",
                "latest_request": None,
            }
        )
        await self.store.admit_credentials(result.model_dump(mode="json"))
        await self.authority(packet["project_id"], packet["repository_id"], actor, configure=True)
        return result

    async def _save(
        self,
        project_id: str,
        repository_id: str,
        body: BoardConnectionInputV1,
        actor: Principal,
        resource: Data,
        discovery: BoardDiscoveryV1,
        repository: Data,
    ) -> Data:
        async with self.locked(project_id, repository_id) as session:
            await self.authority(project_id, repository_id, actor, configure=True)
            old = await session.scalar(
                select(ForgeGitHubProjectsConfigurationEntity).where(
                    ForgeGitHubProjectsConfigurationEntity.project_id == identifier(project_id),
                    ForgeGitHubProjectsConfigurationEntity.repository_id == identifier(repository_id),
                    ForgeGitHubProjectsConfigurationEntity.request_id == identifier(body.request_id),
                )
            )
            if old is not None:
                if old.data["input"] != body.model_dump(mode="json") or old.data["actor"] != self.access.actor_snapshot(
                    actor
                ):
                    raise ValueError("Configuration identity belongs to another body or actor")
                return copy.deepcopy(old.data)
            await self.resources._admission(resource, actor=actor)
            if await self.store.repository(repository_id) != repository:
                raise ValueError("The registered repository changed during board discovery")
            previous = await self.binding(project_id, repository_id)
            if (previous or {}).get("public", {}).get("digest") != body.expected_digest:
                raise ValueError("Board configuration changed; review its current revision")
            revision = (previous or {}).get("public", {}).get("revision", 0) + 1
            incoming = body.model_dump(mode="json")
            public = BoardBindingV1.model_validate(
                {
                    **{
                        key: incoming[key]
                        for key in (
                            "enabled",
                            "connection_name",
                            "owner_kind",
                            "owner",
                            "number",
                            "board_id",
                            "field_id",
                            "state_options",
                            "include_existing",
                        )
                    },
                    "revision": revision,
                    "board_url": discovery.board.url,
                    "digest": "0" * 64,
                }
            ).model_dump(mode="json")
            packet = {
                "input": incoming,
                "actor": self.access.actor_snapshot(actor),
                "resource": resource,
                "project_id": project_id,
                "repository_id": repository_id,
                "public": public,
                "repository_snapshot": repository,
                "discovery": discovery.model_dump(mode="json"),
                "created_at": now(),
                "cut_sequence": previous["cut_sequence"]
                if previous
                else int(await session.scalar(select(func.max(ForgeRunEntity.sequence))) or 0),
            }
            public["digest"] = digest(
                {**packet, "public": {key: value for key, value in public.items() if key != "digest"}}
            )
            packet["digest"] = digest(packet)
            await self.store.admit_credentials(packet)
            await self.authority(project_id, repository_id, actor, configure=True)
            row = await self.records.scoped(identifier(project_id), identifier(repository_id))
            if row is None:
                row = ForgeGitHubProjectsBindingEntity(
                    id=uuid4(),
                    project_id=identifier(project_id),
                    repository_id=identifier(repository_id),
                    revision=revision,
                    data=packet,
                )
            else:
                row.revision, row.data = revision, packet
            await self.records.save(row)
            session.add(
                ForgeGitHubProjectsConfigurationEntity(
                    id=uuid4(),
                    project_id=row.project_id,
                    repository_id=row.repository_id,
                    revision=revision,
                    request_id=identifier(body.request_id),
                    data=packet,
                )
            )
            return packet
