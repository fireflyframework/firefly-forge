# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Native factory publication of immutable source, separate from browser reads."""

from __future__ import annotations

import copy
import hashlib
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field
from pyfly.container import service
from sqlalchemy import text

from firefly_forge.object_storage import (
    ArtifactObjectStorage,
    Authorize,
    ObjectStorageSettings,
    native_document_storage,
)
from firefly_forge.project_access import AccessDenied, ProjectAccess
from firefly_forge.runner_artifact_publications import ForgeRunnerArtifactRepository, RunnerArtifactPublications
from firefly_forge.runner_pool_models import FactoryFence, pool_digest
from firefly_forge.runner_pools import RunnerPools
from firefly_forge.secrets import contains_known_credential
from firefly_forge.source_archive import SourceArchiveContent, catalog_fence, public_entry
from firefly_forge.source_archive_index import (
    ForgeSourceArchiveEntity,
    ForgeSourceArchivePageEntity,
    ForgeSourceArchivePageRepository,
    ForgeSourceArchiveRepository,
    SourceArchiveIndex,
    SourceArchiveRefV1,
)
from firefly_forge.source_catalog import SourceCatalogBudget, _directory, read_source_catalog_page
from firefly_forge.store import Store
from firefly_forge.verification_source_preparation import prepare_verification_source


class SourceArchivePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    enabled: bool = True
    excluded_projects: frozenset[UUID] = frozenset()
    retention: str = Field(default="retain_until_explicit_admin_gc", pattern=r"^retain_until_explicit_admin_gc$")

    @classmethod
    def from_environment(cls, values: Mapping[str, str]) -> SourceArchivePolicy:
        raw = values.get("FORGE_SOURCE_ARCHIVE_POLICY", "{}")
        return cls.model_validate_json(raw)


class SourceArchivePublisher:
    def __init__(
        self, content: SourceArchiveContent, index: SourceArchiveIndex, catalogs: Path, budget: SourceCatalogBudget
    ) -> None:
        self.content, self.index, self.catalogs = content, index, catalogs
        self.budget = budget.model_copy(update={"page_entries": min(128, budget.page_entries)})

    async def publish(
        self, run: dict[str, Any], workspace: Path, head: str, tree: str, fence: FactoryFence, *, authorize: Authorize
    ) -> SourceArchiveRefV1:
        authorize = self.content.helper._current(authorize)
        await authorize()
        root = self.catalogs / UUID(run["id"]).hex
        root.mkdir(mode=0o700, exist_ok=True)
        os.close(_directory(root, private=True))
        prepared = await prepare_verification_source(workspace, root, budget=self.budget, assert_current=authorize)
        value = {
            "tenant_id": self.content.helper.tenant_id.hex,
            "project_id": UUID(run["project_id"]).hex,
            "repository_id": UUID(run["repository_id"]).hex,
            "run_id": UUID(run["id"]).hex,
            "attempt": run["attempt"],
            "head": head,
            "tree": tree,
            "branch": run.get("branch"),
            "source_digest": prepared.source_digest,
            "storage_binding": self.content.helper.objects.settings.binding,
            "credential_scope": self.content.credential_fingerprint(),
        }
        snapshot_id = uuid5(NAMESPACE_URL, "forge-source-archive-v1:" + pool_digest(value)).hex
        ref = SourceArchiveRefV1.model_validate(
            {k: v for k, v in value.items() if k not in {"source_digest", "credential_scope"}}
            | {"snapshot_id": snapshot_id, "catalog": prepared.reference.model_dump(mode="json")}
        )
        await prepared.revalidate(assert_current=authorize)
        prior = await self.index.begin(ref, fence)
        if prior is not None:
            catalog_fence(prepared)
            await authorize()
            return prior
        try:
            registry = self.content.helper.credential_registry()
            if contains_known_credential(ref.model_dump(mode="json"), registry):
                raise ValueError("Source publication metadata contains configured credentials")
            for number in range(ref.catalog.page_count):
                await authorize()
                metadata = read_source_catalog_page(root, prepared.reference, number)
                if contains_known_credential(metadata.model_dump(mode="json"), registry):
                    raise ValueError("Source catalog metadata contains configured credentials")
            for page_number in range(ref.catalog.page_count):
                await authorize()
                page = read_source_catalog_page(root, prepared.reference, page_number)
                content: list[dict[str, Any] | None] = []
                for entry in page.entries:
                    public_entry(entry)
                    content.append(
                        await self.content.publish(ref.content_binding(), prepared, entry, fence, authorize=authorize)
                        if entry.kind == "file"
                        else None
                    )
                catalog_fence(prepared)
                await self.index.put(ref, page, content, fence)
            await prepared.revalidate(assert_current=authorize)
            await authorize()
            await self.index.seal(ref, fence, lambda: catalog_fence(prepared))
            await authorize()
            catalog_fence(prepared)
            return ref
        except BaseException:
            # A failed or ambiguous publication stays visible as unavailable. No retry is dispatched.
            await self.index.fail(snapshot_id, fence)
            raise


@service
class SourceArchiveManager:
    phase = 10

    def __init__(self, store: Store) -> None:
        self.store = store
        self.settings = ObjectStorageSettings.from_environment(os.environ)
        self.policy = SourceArchivePolicy.from_environment(os.environ)
        self.tenant = (
            UUID(os.environ["FORGE_OBJECT_STORAGE_TENANT_ID"]) if self.settings.provider != "disabled" else None
        )
        self.publisher: SourceArchivePublisher | None = None

    async def start(self) -> None:
        if self.settings.provider == "disabled":
            return
        assert self.tenant is not None
        root = self.store.path.parent / "source-archives"
        for path in (root, root / "scratch", root / "catalogs"):
            path.mkdir(mode=0o700, exist_ok=True)
            os.close(_directory(path, private=True))
        schema = "forge_source_" + hashlib.sha256(self.store.schema.encode()).hexdigest()[:32]
        if self.store.session_provider is None:
            raise RuntimeError("Source storage requires initialized native PostgreSQL persistence")
        pools = RunnerPools(
            self.store.session_provider, schema, factory_schema=self.store.schema, object_artifacts=True
        )
        await pools.initialize()
        async with pools._unit() as session:
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"),
                {"key": "forge:source-index-migration:" + schema},
            )
            connection = await session.connection()
            tables = [
                ForgeSourceArchiveEntity.metadata.tables[entity.__tablename__]
                for entity in (ForgeSourceArchiveEntity, ForgeSourceArchivePageEntity)
            ]
            await connection.run_sync(lambda sync: ForgeSourceArchiveEntity.metadata.create_all(sync, tables=tables))
        ledger = RunnerArtifactPublications(pools, ForgeRunnerArtifactRepository())
        content = SourceArchiveContent(
            self.tenant,
            ArtifactObjectStorage(self.settings, native_document_storage(self.settings)),
            ledger,
            root / "scratch",
        )
        content.helper.credential_registry = lambda: {
            **self.store.credential_registry(),
            **self.settings.credential_registry(),
        }
        self.publisher = SourceArchivePublisher(
            content,
            SourceArchiveIndex(pools, ForgeSourceArchiveRepository(), ForgeSourceArchivePageRepository()),
            root / "catalogs",
            SourceCatalogBudget(),
        )

    async def stop(self) -> None:
        pass

    async def publish(
        self, run: dict[str, Any], workspace: Path, head: str, tree: str, *, assert_current: Authorize
    ) -> SourceArchiveRefV1 | None:
        if (
            self.settings.provider == "disabled"
            or not self.policy.enabled
            or UUID(run["project_id"]) in self.policy.excluded_projects
        ):
            return None
        if self.publisher is None:
            raise RuntimeError("Configured immutable source storage is not initialized")
        owner = self.store.execution_ownership_token
        scope = {
            key: copy.deepcopy(run.get(key))
            for key in ("id", "repository_id", "project_id", "execution_actor", "attempt", "branch")
        }

        async def current() -> None:
            await assert_current()
            await self.store.assert_ownership()
            present = await self.store.get_run(run["id"])
            if (
                owner is None
                or self.store.execution_ownership_token is not owner
                or any(present.get(k) != v for k, v in scope.items())
            ):
                raise AccessDenied("Source publication owner or native scope changed")
            if not present.get("execution_actor"):
                raise AccessDenied("Source publication requires a recorded canonical actor")
            await ProjectAccess(self.store).authorize_execution(present)
            await self.store.assert_campaign_current(present)
            if self.store.execution_ownership_token is not owner:
                raise AccessDenied("Source publication native owner changed")

        await current()
        async with self.store._unit() as session:
            started = await session.scalar(
                text("SELECT backend_start FROM pg_stat_activity WHERE pid=:pid"), {"pid": self.store._ownership_pid}
            )
        pid = self.store._ownership_pid
        if pid is None:
            raise AccessDenied("Source publication requires the current native database owner")
        fence = FactoryFence(factory_schema=self.store.schema, pid=pid, backend_started_at=started)
        return await self.publisher.publish(run, workspace, head, tree, fence, authorize=current)
