# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Continuation must use the same real Engine phase and original task as dispatch."""

from datetime import UTC, datetime
from uuid import NAMESPACE_URL, uuid4, uuid5

import pytest

from firefly_forge.runner_continuations import continuation_binding
from firefly_forge.runner_pool_bundle import pack_project_bundle
from firefly_forge.runner_pool_models import FactoryFence, RunnerProjectInvocation, pool_digest


def original(tmp_path, *, task="verify", legacy=False):
    source, metadata = tmp_path / "source", tmp_path / "git"
    source.mkdir()
    metadata.mkdir()
    (source / "main.py").write_text("print(1)\n")
    (metadata / "HEAD").write_text("ref: refs/heads/forge-snapshot\n")
    data, bundle = pack_project_bundle(source, metadata, 1_048_576)
    run_id, repository_id = uuid4(), uuid4()
    invocation = RunnerProjectInvocation(
        execution_id=uuid4().hex,
        profile_id="owned",
        profile_revision="a" * 64,
        repository_id=repository_id,
        run_id=run_id,
        image="example/worker@sha256:" + "b" * 64,
        argv=("git", "diff"),
        source_digest=bundle.source_digest,
        archive_digest=bundle.archive_digest,
        source_bytes=len(data),
        bundle=bundle,
    )
    configuration = {} if legacy else {"work_duration": {"protocol": "forge-work-duration-v2", "timeout_seconds": None}}
    run = {
        "id": run_id.hex,
        "repository_id": repository_id.hex,
        "status": {"implement": "developing", "verify": "validating"}[task],
        "active_task": task,
        "sandbox_snapshot": {"digest": "c" * 64, "configuration": configuration},
    }
    stage = {
        "id": task,
        "status": "running",
        "attempt": 2,
        "idempotency_key": uuid5(NAMESPACE_URL, f"forge:{run_id.hex}:{task}:2").hex,
    }
    wal = {
        "execution_id": invocation.execution_id,
        "task_id": task,
        "parent_attempt": 2,
        "status": "running",
        "request": {
            "request": invocation.model_dump(mode="json"),
            "factory_fence": FactoryFence(
                factory_schema="owned", pid=123, backend_started_at=datetime(2026, 10, 29, tzinfo=UTC)
            ).model_dump(mode="json"),
            "parent_attempt": 2,
            "snapshot_digest": run["sandbox_snapshot"]["digest"],
            "request_digest": pool_digest(invocation.model_dump(mode="json")),
        },
        "effects": {
            "start": {"status": "intent"},
            "create": {"status": "done", "evidence": {"scope": "factory_request_prepared"}},
        },
    }
    return run, wal, stage


@pytest.mark.parametrize("task", ["implement", "verify"])
@pytest.mark.parametrize("legacy", [False, True])
def test_continuation_accepts_the_actual_engine_phase_without_rewriting_state(tmp_path, task, legacy):
    run, wal, stage = original(tmp_path, task=task, legacy=legacy)
    expected = pool_digest([run, wal, stage])
    result = continuation_binding(run, wal, stage)
    assert result["task_id"] == task and result["attempt"] == 2
    assert pool_digest([run, wal, stage]) == expected


def test_legacy_running_compatibility_still_requires_the_exact_active_stage(tmp_path):
    run, wal, stage = original(tmp_path, legacy=True)
    run["status"] = "running"
    assert continuation_binding(run, wal, stage)["attempt"] == 2
    stage["idempotency_key"] = "f" * 32
    with pytest.raises(ValueError, match="exact unfinished"):
        continuation_binding(run, wal, stage)


@pytest.mark.parametrize("change", ["phase", "active", "task", "key", "attempt", "cancel", "generic", "paused"])
def test_continuation_never_broadens_a_replaced_cancelled_or_inactive_stage(tmp_path, change):
    run, wal, stage = original(tmp_path)
    if change == "phase":
        run["status"] = "developing"
    elif change == "active":
        run["active_task"] = "implement"
    elif change == "task":
        stage["id"] = "implement"
    elif change == "key":
        stage["idempotency_key"] = "f" * 32
    elif change == "attempt":
        stage["attempt"] = True
    elif change == "cancel":
        run["cancel_request"] = {"status": "requested"}
    elif change == "generic":
        run["status"] = "running"
    else:
        run["status"] = "paused"
    with pytest.raises(ValueError, match="exact unfinished"):
        continuation_binding(run, wal, stage)
