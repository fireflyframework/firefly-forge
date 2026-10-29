# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Real factory execution path with recorded transport and bundle fixtures, no SQL or Docker."""

import asyncio
import hashlib
from collections import defaultdict
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID

import pytest
from test_runner_pools import IMAGE, profile

from firefly_forge import runner_pool_worker as module
from firefly_forge.runner_bundle_models import ProjectBundleIdentity
from firefly_forge.runner_factory_models import FactoryPoolSelection, capture_factory_pool
from firefly_forge.runner_pool_models import FactoryFence
from firefly_forge.work_duration_models import WorkDurationPolicyV2


@pytest.mark.parametrize("tagged", [False, True])
@pytest.mark.parametrize("kind", ["authority", "request", "receipt", "submission", "cancel_unavailable"])
async def test_tagged_post_submit_failure_requests_only_exact_owned_cancellation(tmp_path, monkeypatch, kind, tagged):
    duration = WorkDurationPolicyV2(timeout_seconds=12)
    pool = profile(**({"work_duration": duration} if tagged else {}))
    snapshot = capture_factory_pool(
        FactoryPoolSelection(profile=pool, image=IMAGE, work_duration=duration if tagged else None)
    )
    run_id, execution_id = "a" * 32, "b" * 32
    workspace = tmp_path / run_id
    workspace.mkdir()
    fence = FactoryFence(
        factory_schema="fixture_factory", pid=123, backend_started_at=datetime(2026, 10, 28, tzinfo=UTC)
    )
    run = {"id": run_id, "repository_id": UUID("c" * 32), "sandbox_snapshot": snapshot}
    scope = {"run": run, "attempt": 1, "fence": fence}
    submitted, cancelled, intents = [], [], []

    async def authorize(*args):
        if submitted and kind == "authority":
            raise ValueError("controlled run authority revoked")
        return scope

    async def exclusive():
        return fence

    async def submit(invocation, selected_fence, archive):
        submitted.append((invocation, selected_fence))
        if kind == "submission":
            raise OSError("controlled uncertain submission")

    async def observe(invocation):
        return {
            "request": {} if kind == "request" else invocation.model_dump(mode="json"),
            "status": "finished",
            "receipt": {"outcome": "completed", "drained": False},
        }

    async def cancel(invocation, selected_fence):
        cancelled.append((invocation, selected_fence))
        if kind == "cancel_unavailable":
            raise OSError("controlled cancellation unknown")

    async def intent(run_id, task, attempt, selected_snapshot, prepare, **kwargs):
        payload = prepare(1, execution_id, None)
        intents.append(payload)
        return {"fresh": True, "execution": {"execution_id": execution_id, "request": payload}}

    async def effect(*args, **kwargs):
        return {"disposition": "execute"}

    async def metadata(*args):
        return b"git-fixture"

    async def probe(*args):
        return {"available": True}

    source, archive = b"source-fixture", b"framed-fixture"

    def digest(raw):
        return hashlib.sha256(raw).hexdigest()

    bundle = ProjectBundleIdentity(
        archive_digest=digest(archive),
        archive_bytes=len(archive),
        source_digest="d" * 64,
        source_archive_digest=digest(source),
        source_archive_bytes=len(source),
        git_archive_bytes=11,
    )
    monkeypatch.setattr(module, "pack_workspace", lambda *args, **kwargs: (source, "fixture"))
    monkeypatch.setattr(module, "frame_project_archives", lambda *args: (archive, bundle))
    worker = object.__new__(module.RunnerPoolWorker)
    worker.workspaces, worker.locks, worker.waiting = tmp_path, defaultdict(asyncio.Lock), {}
    worker.authority = SimpleNamespace(authorize=authorize, exclusive_fence=exclusive)
    worker.client = SimpleNamespace(submit=submit, observe=observe, cancel=cancel)
    worker.journal = SimpleNamespace(
        sandbox_command_intent=intent, sandbox_effect_receipt=effect, sandbox_effect_intent=effect
    )
    worker.artifacts = SimpleNamespace(directory=tmp_path, credential_registry=lambda: {}, retain=lambda *args: None)
    worker.git_snapshot, worker.probe = SimpleNamespace(build=metadata), probe
    with pytest.raises(RuntimeError, match="reconciliation"):
        await worker.execute(run_id, workspace, workspace, ["fixture"], 2, "verify")
    assert len(submitted) == len(intents) == 1
    assert len(cancelled) == (1 if tagged else 0)
    if tagged:
        assert cancelled[0] == submitted[0]
        assert cancelled[0][0].execution_id == execution_id
    assert worker.waiting == {}
