# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Exercise fixture boundaries with strict units and a real AnyIO portal, without SQL."""

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
import test_scm_natural_checkpoints_native as native_checkpoint
from anyio.from_thread import start_blocking_portal
from test_git_failed_unit_adoption_native import witness

from firefly_forge.project_access import AccessDenied


@pytest.mark.asyncio
async def test_adoption_witness_uses_supported_unit_plain_reads_and_detaches_values():
    active = None
    calls = []
    run = SimpleNamespace(data={"status": "failed", "used": 30})
    task = SimpleNamespace(task_id="implement", status="failed", attempt=6, data={"retained": True})
    event = SimpleNamespace(kind="worker_feedback", digest="a" * 64, data={"disposition": "failed"})

    @asynccontextmanager
    async def unit():
        nonlocal active
        assert active is None
        active = object()
        calls.append(("unit", active))
        try:
            yield active
        finally:
            active = None

    async def row(_run_id):
        assert active is not None
        calls.append(("run", active))
        return run

    async def tasks(_run_id):
        assert active is not None
        calls.append(("tasks", active))
        return [task]

    async def events(_run_id):
        assert active is not None
        calls.append(("events", active))
        return [event]

    env = SimpleNamespace(
        run_id="1" * 32,
        store=SimpleNamespace(_unit=unit, run_records=SimpleNamespace(find_by_id=row)),
        journal=SimpleNamespace(
            tasks=SimpleNamespace(find_for_run=tasks), records=SimpleNamespace(find_for_run=events)
        ),
    )
    result = await witness(env)
    assert [kind for kind, _ in calls] == ["unit", "run", "tasks", "events"]
    assert all(session is calls[0][1] for _, session in calls)
    assert active is None
    run.data["used"] = 0
    task.data["retained"] = False
    event.data["disposition"] = "changed"
    assert result["run"] == {"status": "failed", "used": 30}
    assert result["tasks"] == [("implement", "failed", 6, {"retained": True})]
    assert result["entries"] == [("worker_feedback", "a" * 64, {"disposition": "failed"})]


def test_checkpoint_actor_portal_preserves_explicit_remote_false_and_denials():
    assert callable(getattr(native_checkpoint, "checkpoint_actor", None)), "Native portal adapter required"
    seen = []
    actor = object()

    async def resolve(delivery, *, remote):
        seen.append((delivery, remote))
        if delivery != "current":
            raise AccessDenied("Controlled stale receipt")
        return actor

    with start_blocking_portal() as portal:
        case = SimpleNamespace(http=SimpleNamespace(portal=portal), service=SimpleNamespace(resolve_checkpoint=resolve))
        assert native_checkpoint.checkpoint_actor(case, "current") is actor
        with pytest.raises(AccessDenied, match="Controlled stale receipt"):
            native_checkpoint.checkpoint_actor(case, "stale")
    assert seen == [("current", False), ("stale", False)]
