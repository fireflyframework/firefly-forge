# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Dedicated non-superuser PG and controlled OIDC/ASGI; no worker or cloud."""

from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text
from test_run_creation_scopes_native import nonadministrator

from firefly_forge.api import create_application
from firefly_forge.identity import IdentityService
from firefly_forge.object_storage import ObjectWriteUncertain
from firefly_forge.persistence import ForgeRunEntity
from firefly_forge.project_access import CURRENT_PRINCIPAL, ProjectAccess
from firefly_forge.source_archive_service import SourceArchiveManager
from firefly_forge.source_explorer import SourceExplorer


async def native(store, tmp_path, monkeypatch, actor, repository, project):
    for name, value in {
        "FORGE_OBJECT_STORAGE_PROVIDER": "local",
        "FORGE_OBJECT_STORAGE_MODE": "test",
        "FORGE_OBJECT_STORAGE_STORAGE_ID": UUID(int=8).hex,
        "FORGE_OBJECT_STORAGE_TENANT_ID": UUID(int=1).hex,
        "FORGE_OBJECT_STORAGE_LOCAL_DIRECTORY": str(tmp_path / "objects"),
    }.items():
        monkeypatch.setenv(name, value)
    async with store._unit() as session:
        assert await session.scalar(text("SELECT rolsuper FROM pg_roles WHERE rolname=current_user")) is False
        value = dict(
            id=uuid4().hex,
            project_id=project["id"],
            repository_id=repository["id"],
            attempt=1,
            status="paused",
            branch="recorded/source-test",
            agent="api",
            sandbox_snapshot={"mode": "docker"},
            execution_actor=ProjectAccess.actor_snapshot(actor),
        )
        await store.run_records.save(
            ForgeRunEntity(id=UUID(value["id"]), repository_id=UUID(repository["id"]), status="paused", data=value)
        )
    await store.acquire_ownership()
    manager = SourceArchiveManager(store)
    await manager.start()
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    (workspace / "app.py").write_text('print("native archived source")\n')
    (workspace / "binary.dat").write_bytes(b"\xff\x00retained")
    return manager, workspace, await store.get_run(value["id"])


async def test_native_lifecycle_asgi_paged_cold_reads_current_project_scope_and_revocation(
    store, tmp_path, monkeypatch
):
    async with nonadministrator(store, tmp_path) as c:
        identity, _, credential, actor, access, repositories, projects, _ = c
        manager, workspace, run = await native(store, tmp_path, monkeypatch, actor, repositories[0], projects[0])
        for number in range(129):
            (workspace / f"z-{number:03d}.py").write_bytes(b"")
        assert CURRENT_PRINCIPAL.get() is None

        async def current():
            await store.assert_ownership()

        await store.release_ownership()
        for key, value in {
            "FORGE_AUTH_MODE": "oidc",
            "FORGE_API_TOKEN": "",
            "FORGE_OIDC_ISSUER": identity.config.issuer,
            "FORGE_OIDC_CLIENT_ID": identity.config.client_id,
            "FORGE_OIDC_CLIENT_SECRET": identity.config.client_secret.get_secret_value(),
            "FORGE_OIDC_API_AUDIENCE": identity.config.api_audience,
            "FORGE_OIDC_REDIRECT_URI": identity.config.redirect_uri,
            "FORGE_IDENTITY_ENCRYPTION_KEY": identity.config.encryption_key.get_secret_value(),
        }.items():
            monkeypatch.setenv(key, value)
        # Capture the configured native registry after infrastructure initialization,
        # then reopen an independent API for cold reads with the same retained keys.
        boot = create_application(store.path.parent, role="api")
        async with boot.router.lifespan_context(boot):
            store.credential_registry = boot.state.store.credential_registry
            await store.acquire_ownership()
            ref = await manager.publish(run, workspace, "a" * 40, "b" * 40, assert_current=current)
            await store.release_ownership()
        assert ref.catalog.file_count == 131 and ref.catalog.page_count == 2
        (workspace / "app.py").unlink()
        app = create_application(store.path.parent, role="api")
        async with app.router.lifespan_context(app):
            native_identity = app.state.runtime.context.get_bean(IdentityService)
            await native_identity.client.aclose()
            native_identity.client = identity.client
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app),
                base_url="https://testserver",
                headers={"Authorization": "Bearer " + credential},
            ) as client:
                prefix = f"/api/v1/runs/{run['id']}/source/revisions"
                revisions = await client.get(prefix)
                assert revisions.status_code == 200 and revisions.json()["recording_enabled"] is True
                assert revisions.json()["items"][0]["reference"]["snapshot_id"] == ref.snapshot_id
                params = {"expected_manifest": ref.catalog.manifest_sha256}
                page = await client.get(prefix + f"/{ref.snapshot_id}/catalog", params={**params, "page": 1})
                assert page.status_code == 200 and len(page.json()["catalog"]["entries"]) == 3
                content = await client.get(
                    prefix + f"/{ref.snapshot_id}/files/0", params={**params, "page": 0, "limit": 8}
                )
                assert content.status_code == 200 and content.json()["text"] == 'print("n'
                assert content.headers["Cache-Control"] == "private, no-store"
                explorer = app.state.runtime.context.get_bean(SourceExplorer)
                assert explorer.manager.publisher is not manager.publisher
                saved = explorer.manager.tenant
                explorer.manager.tenant = UUID(int=9)
                assert (await client.get(prefix + f"/{ref.snapshot_id}/catalog", params=params)).status_code == 409
                explorer.manager.tenant = saved
                await access.set_membership(projects[0]["id"], actor, None)
                assert (await client.get(prefix + f"/{ref.snapshot_id}/catalog", params=params)).status_code == 403
        async with store._unit() as session:
            assert await session.scalar(text("SELECT count(*) FROM forge_execution_requests")) == 0


async def test_native_lost_source_write_is_retained_unavailable_after_restart_without_retry(
    store, tmp_path, monkeypatch
):
    async with nonadministrator(store, tmp_path) as c:
        _, _, _, actor, _, repositories, projects, _ = c
        manager, workspace, run = await native(store, tmp_path, monkeypatch, actor, repositories[0], projects[0])
        adapter = manager.publisher.content.helper.objects.adapter
        writes = 0

        class LostReply:
            async def upload(self, *args):
                nonlocal writes
                writes += 1
                await adapter.upload(*args)
                raise TimeoutError("controlled reply loss")

            async def download(self, *args):
                return await adapter.download(*args)

        manager.publisher.content.helper.objects.adapter = LostReply()

        async def current():
            await store.assert_ownership()

        with pytest.raises(ObjectWriteUncertain):
            await manager.publish(run, workspace, "a" * 40, "b" * 40, assert_current=current)
        reopened = SourceArchiveManager(store)
        await reopened.start()
        rows = await reopened.publisher.index.revisions(run["id"], None)
        assert len(rows) == 1 and rows[0]["status"] == "failed"
        with pytest.raises(ValueError, match="unavailable"):
            await reopened.publisher.index.get(rows[0]["reference"]["snapshot_id"])
        with pytest.raises(RuntimeError, match="reconciliation"):
            await reopened.publish(run, workspace, "a" * 40, "b" * 40, assert_current=current)
        assert writes == 1
