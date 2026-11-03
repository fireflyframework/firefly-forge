# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Dedicated PostgreSQL artifact publication; no Docker, models or cloud calls."""

import asyncio
import hashlib
import os
from dataclasses import replace

import httpx
import pytest
from sqlalchemy import text
from test_runner_controller import controller_schema as controller_schema
from test_runner_controller import settings as controller_settings
from test_runner_object_artifacts_host import fixture, stream

from firefly_forge.object_storage import ObjectWriteUncertain
from firefly_forge.runner_artifact_publications import ForgeRunnerArtifactRepository, RunnerArtifactPublications
from firefly_forge.runner_object_artifacts import RunnerObjectArtifacts
from firefly_forge.runner_pool_models import FactoryFence
from firefly_forge.runner_pools import RunnerPools


async def native_fixture(store, tmp_path):
    service, profile, invocation, _ = fixture(tmp_path)
    await store.acquire_ownership()
    async with store._unit() as session:
        assert await session.scalar(text("SELECT rolsuper FROM pg_roles WHERE rolname=current_user")) is False
        started = await session.scalar(
            text("SELECT backend_start FROM pg_stat_activity WHERE pid=:pid"), {"pid": store._ownership_pid}
        )
    fence = FactoryFence(factory_schema=store.schema, pid=store._ownership_pid, backend_started_at=started)
    pools = RunnerPools(store.session_provider, store.schema, object_artifacts=True)
    await pools.initialize()
    service.ledger = RunnerArtifactPublications(pools, ForgeRunnerArtifactRepository())

    async def current():
        await pools.check_factory_fence(fence)

    return service, profile, invocation, pools, fence, current


async def test_native_publication_cas_and_reopened_reader_restore_without_local_bin(
    store, tmp_path, controller_schema, monkeypatch
):
    service, profile, invocation, pools, fence, current = await native_fixture(store, tmp_path)
    data = b"durable artifact" * 40000
    receipt = {"kind": "result", "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
    outcomes = await asyncio.gather(
        *(
            service.upload(profile, invocation, "result", receipt, stream(data), fence, authorize=current)
            for _ in range(2)
        ),
        return_exceptions=True,
    )
    assert any(outcome == receipt for outcome in outcomes)
    assert all(outcome == receipt or isinstance(outcome, RuntimeError) for outcome in outcomes)
    reopened = RunnerObjectArtifacts(
        service.tenant_id,
        service.objects,
        RunnerArtifactPublications(pools, ForgeRunnerArtifactRepository()),
        service.scratch,
    )
    target = await reopened.restore(
        profile, invocation, "result", receipt, service.scratch, "restored.bin", authorize=current
    )
    assert target.read_bytes() == data and not list(service.scratch.glob("*/result.bin"))
    async with pools._unit() as session:
        assert (
            await session.scalar(
                text("SELECT count(*) FROM forge_runner_artifact_publications WHERE status='published'")
            )
            == 1
        )
        assert await session.scalar(text("SELECT count(*) FROM forge_runner_invocations")) == 0

    from firefly_forge.runner_controller import PoolObjectUpload, RunnerControllerService, create_runner_application
    from firefly_forge.runner_pool_dispatch import DockerPoolDispatcher

    async def controlled_images(_self):
        return None

    # Real PyFly DI and PostgreSQL, with no Docker socket or container launch.
    monkeypatch.setattr(DockerPoolDispatcher, "verify_images", controlled_images)
    monkeypatch.setenv("FORGE_RUNNER_DATABASE_URL", os.environ["FORGE_DATABASE_URL"])
    cfg = controller_settings(tmp_path, store.schema)
    cfg = replace(
        cfg,
        authority=replace(cfg.authority, profile=profile, state_directory=service.scratch),
        active=True,
        object_storage=service.objects.settings,
        object_tenant_id=service.tenant_id,
    )
    app = create_runner_application(cfg)
    metadata = PoolObjectUpload(
        invocation=invocation, fence=fence, kind="source", bytes=6, sha256=invocation.archive_digest
    )
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="https://testserver") as client:
            response = await client.post(
                "/runner/v1/object-artifacts/upload",
                content=stream(b"source"),
                headers={
                    "Authorization": "Bearer " + cfg.token.get_secret_value(),
                    "X-Forge-Artifact-Request": metadata.model_dump_json(),
                },
            )
        assert response.status_code == 200 and response.json()["execution_granted"] is False
        configured = app.state.runtime.context.get_bean(RunnerControllerService)
        async with configured.pools._unit() as session:
            assert (
                await session.scalar(
                    text("SELECT count(*) FROM forge_runner_artifact_publications WHERE status='published'")
                )
                == 1
            )
            assert await session.scalar(text("SELECT count(*) FROM forge_runner_invocations")) == 0
    await store.release_ownership()
    with pytest.raises(RuntimeError):
        await reopened.upload(profile, invocation, "result", receipt, stream(data), fence, authorize=current)


async def test_native_unknown_upload_reopens_and_reconciles_without_reupload_or_execution(store, tmp_path):
    service, profile, invocation, pools, fence, current = await native_fixture(store, tmp_path)
    native = service.objects.adapter
    writes = 0

    class LostReply:
        name = "controlled-lost-native-reply"

        async def upload(self, document, data):
            nonlocal writes
            writes += 1
            await native.upload(document, data)
            raise TimeoutError("controlled reply loss")

        async def download(self, document, version=None):
            return await native.download(document, version)

    service.objects.adapter = LostReply()
    receipt = {"kind": "output", "bytes": 4, "sha256": hashlib.sha256(b"done").hexdigest()}
    with pytest.raises(ObjectWriteUncertain):
        await service.upload(profile, invocation, "output", receipt, stream(b"done"), fence, authorize=current)
    reopened = RunnerObjectArtifacts(
        service.tenant_id,
        service.objects,
        RunnerArtifactPublications(pools, ForgeRunnerArtifactRepository()),
        service.scratch,
    )
    with pytest.raises(RuntimeError, match="replay"):
        await reopened.upload(profile, invocation, "output", receipt, stream(b"done"), fence, authorize=current)
    await reopened.reconcile(profile, invocation, "output", receipt, fence, authorize=current)
    assert writes == 1
    assert (
        b"".join([part async for part in reopened.read(profile, invocation, "output", receipt, authorize=current)])
        == b"done"
    )
    async with pools._unit() as session:
        assert await session.scalar(text("SELECT count(*) FROM forge_runner_invocations")) == 0
