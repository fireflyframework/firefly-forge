# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Actual verification service/Journal/Engine path with memory persistence and controlled workers."""

from contextlib import asynccontextmanager
from contextvars import ContextVar

import pytest
from test_automatic_verification_engine_host import (
    test_repair_zero_gets_exactly_one_native_verification_continuation as existing_engine_path,
)
from test_bridge_inflight_cancel import MemoryStore


@pytest.mark.parametrize("queued_drift", [None, "source"])
async def test_handoff_and_subsequent_provider_never_build_a_project_manifest(tmp_path, monkeypatch, queued_drift):
    from firefly_forge import assurance
    from firefly_forge import verification_source_preparation as preparation

    unit = ContextVar("synthetic_catalog_caller_unit", default=False)
    original = MemoryStore._unit

    @asynccontextmanager
    async def measured_unit(self):
        token = unit.set(True)
        try:
            async with original(self) as session:
                yield session
        finally:
            unit.reset(token)

    monkeypatch.setattr(MemoryStore, "_unit", measured_unit)
    monkeypatch.setattr(preparation, "current_unit_of_work", lambda: object() if unit.get() else None)

    def no_full_map(*args, **kwargs):
        raise AssertionError("The real handoff path still invokes the legacy full project manifest")

    monkeypatch.setattr(assurance, "_manifest", no_full_map)
    await existing_engine_path(tmp_path, monkeypatch, queued_drift)


async def test_approved_check_generated_cache_keeps_legacy_source_identity(tmp_path, monkeypatch):
    import test_automatic_verification_engine_host as fixture
    from test_bridge_inflight_cancel import RUN

    original = fixture.ProcessResult

    def completed_check(argv, code, output):
        cache = tmp_path / "workspaces" / RUN / ".pytest_cache"
        cache.mkdir(exist_ok=True)
        (cache / "result").write_text("synthetic generated cache")
        return original(argv, code, output)

    monkeypatch.setattr(fixture, "ProcessResult", completed_check)
    await existing_engine_path(tmp_path, monkeypatch, None)


async def test_cancelling_source_preparation_drains_and_releases_publication_lock(tmp_path, monkeypatch):
    import asyncio

    from firefly_forge import verification_handoffs

    entered, release = asyncio.Event(), asyncio.Event()
    services = []
    original_prepare = verification_handoffs.prepare_verification_source
    original_init = verification_handoffs.VerificationHandoffs.__init__

    def record_service(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        services.append(self)

    async def waiting_prepare(*args, **kwargs):
        authority = kwargs["assert_current"]

        async def waiting_authority():
            await authority()
            entered.set()
            await release.wait()

        return await original_prepare(*args, **{**kwargs, "assert_current": waiting_authority})

    monkeypatch.setattr(verification_handoffs.VerificationHandoffs, "__init__", record_service)
    monkeypatch.setattr(verification_handoffs, "prepare_verification_source", waiting_prepare)
    task = asyncio.create_task(existing_engine_path(tmp_path, monkeypatch, None))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)
    finally:
        release.set()
    assert len(services) == 1 and not services[0].engine.publication_lock.locked()
    journal = services[0].journal
    run = await services[0].store.get_run(services[0].store.run.data["id"])
    assert run["status"] == "paused" and run["worker_request"]["status"] == "pending"
    assert not await journal.sandbox_executions(run["id"])
    assert all(row["kind"] != "verification_handoff_intent" for row in await journal.entries(run["id"]))
