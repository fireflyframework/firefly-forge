# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Ownership renewal permits unknown job lengths without extending work budgets."""

import asyncio
import sys

import pytest

from firefly_forge.runner_monitor_models import MonitorStartInput, MonitorWaitInput
from firefly_forge.runner_monitor_transport import OwnedProcessMonitor


def test_monitor_and_wait_have_no_default_work_duration_limit():
    assert MonitorStartInput(argv=["owned-command"]).timeout_seconds is None
    assert MonitorWaitInput(monitor_id="a" * 32, condition={"kind": "exit"}).timeout_seconds is None


async def test_healthy_owner_can_wait_past_multiple_grace_windows_in_one_call(tmp_path):
    records = []

    async def record(kind, payload):
        records.append((kind, payload))

    async with OwnedProcessMonitor(tmp_path, record=record, owner_grace_seconds=1) as manager:
        started = await manager.start(
            "a" * 32,
            MonitorStartInput(
                argv=[
                    sys.executable,
                    "-B",
                    "-u",
                    "-c",
                    "import time;time.sleep(2.2);print('unknown-duration-ready',flush=True);time.sleep(30)",
                ]
            ),
        )
        assert started["work_deadline_ms"] is None
        result = await manager.wait(
            MonitorWaitInput(monitor_id="a" * 32, condition={"kind": "output", "text": "unknown-duration-ready"})
        )
        assert result["outcome"] == "satisfied" and result["observation"]["status"] == "running"
        assert result["observation"]["ownership_expires_at_ms"] > started["ownership_expires_at_ms"]
        assert (await manager.cancel("a" * 32))["status"] == "cancelled"
    assert [kind for kind, _ in records] == ["monitor_spawn_intent", "monitor_started", "monitor_terminal"]


async def test_explicit_work_timeout_remains_separate_from_owner_grace(tmp_path):
    async def record(*args):
        pass

    async with OwnedProcessMonitor(tmp_path, record=record, owner_grace_seconds=3) as manager:
        started = await manager.start(
            "b" * 32,
            MonitorStartInput(argv=[sys.executable, "-B", "-c", "import time;time.sleep(30)"], timeout_seconds=1),
        )
        result = await manager.wait(MonitorWaitInput(monitor_id="b" * 32, condition={"kind": "exit"}))
        assert started["work_deadline_ms"] is not None
        assert result["observation"]["status"] == "timed_out"


async def test_revoked_authority_stops_renewal_and_drains_the_owned_group(tmp_path):
    records, revoked, terminal = [], asyncio.Event(), asyncio.Event()

    async def guard():
        if revoked.is_set():
            raise ValueError("Fictional authority was revoked")

    async def record(kind, payload):
        records.append((kind, payload))
        if kind == "monitor_terminal":
            terminal.set()

    async with OwnedProcessMonitor(tmp_path, record=record, guard=guard, owner_grace_seconds=1) as manager:
        await manager.start(
            "d" * 32,
            MonitorStartInput(argv=[sys.executable, "-B", "-c", "import time;time.sleep(30)"]),
        )
        revoked.set()
        await asyncio.wait_for(terminal.wait(), 5)
        with pytest.raises(ValueError, match="revoked"):
            await manager.status("d" * 32)
    assert [kind for kind, _ in records] == ["monitor_spawn_intent", "monitor_started", "monitor_terminal"]
    receipt = records[-1][1]
    assert receipt["status"] == "needs_reconciliation" and receipt["work_deadline_ms"] is None
    assert receipt["process_group_drained"] is True
    assert receipt["guardian_receipt"]["child_reaped"] is True
    assert type(receipt["exit_code"]) is int


@pytest.mark.parametrize("value", [True, 0, -1, "30"])
def test_explicit_work_timeout_requires_a_positive_integer(value):
    with pytest.raises(ValueError):
        MonitorStartInput(argv=["owned-command"], timeout_seconds=value)


@pytest.mark.parametrize("ownership_loss", ["heartbeat_stopped", "pipe_closed"])
async def test_guardian_drains_without_default_work_cap_after_owner_loss(tmp_path, ownership_loss):
    import json
    from pathlib import Path

    from firefly_forge import runner_monitor_guardian

    payload = {
        "argv": [sys.executable, "-B", "-c", "import time;time.sleep(30)"],
        "work_deadline_ns": None,
        "owner_grace_seconds": 1,
        "token": "c" * 32,
    }
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-B",
        "-I",
        str(Path(runner_monitor_guardian.__file__)),
        json.dumps(payload),
        cwd=tmp_path,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        assert process.stderr is not None and process.stdin is not None and process.stdout is not None
        started = json.loads(await process.stderr.readline())
        assert started["stage"] == "started"
        if ownership_loss == "pipe_closed":
            process.stdin.close()
        terminal = json.loads(await asyncio.wait_for(process.stderr.readline(), 5))
        assert terminal["stage"] == "terminal" and terminal["status"] == "owner_lost"
        assert terminal["child_reaped"] is True and terminal["process_group_drained"] is True
        assert await process.stdout.read() == b"" and await process.stderr.read() == b""
        assert await process.wait() == 0
    finally:
        if process.stdin is not None:
            process.stdin.close()
        await asyncio.wait_for(process.wait(), 5)


async def test_sealed_work_deadline_is_not_restarted_after_slow_start_admission(tmp_path):
    records = []

    async def record(kind, payload):
        records.append((kind, payload))
        if kind == "monitor_spawn_intent":
            await asyncio.sleep(1.1)

    async with OwnedProcessMonitor(tmp_path, record=record) as manager:
        with pytest.raises(ValueError, match="work deadline"):
            await manager.start(
                "e" * 32,
                MonitorStartInput(
                    argv=[
                        sys.executable,
                        "-B",
                        "-c",
                        "from pathlib import Path;Path('late-dispatch').write_text('unexpected')",
                    ],
                    timeout_seconds=1,
                ),
            )
    assert [kind for kind, _ in records] == ["monitor_spawn_intent"]
    assert not (tmp_path / "late-dispatch").exists()


async def test_slow_started_record_cannot_report_an_expired_owner_as_running(tmp_path):
    records = []

    async def record(kind, payload):
        records.append((kind, payload))
        if kind == "monitor_started":
            await asyncio.sleep(1.2)

    async with OwnedProcessMonitor(tmp_path, record=record, owner_grace_seconds=1) as manager:
        observed = await manager.start(
            "f" * 32, MonitorStartInput(argv=[sys.executable, "-B", "-c", "import time;time.sleep(30)"])
        )
        assert observed["status"] == "needs_reconciliation"
    terminal = records[-1][1]
    assert terminal["guardian_receipt"]["status"] == "owner_lost"
    assert terminal["process_group_drained"] is True


async def test_guardian_enforces_remaining_sealed_cap_during_started_record(tmp_path):
    records = []

    async def record(kind, payload):
        records.append((kind, payload))
        if kind in {"monitor_spawn_intent", "monitor_started"}:
            await asyncio.sleep(0.7)

    async with OwnedProcessMonitor(tmp_path, record=record, owner_grace_seconds=3) as manager:
        observed = await manager.start(
            "0" * 32,
            MonitorStartInput(argv=[sys.executable, "-B", "-c", "import time;time.sleep(30)"], timeout_seconds=1),
        )
        assert observed["status"] == "timed_out"
    terminal = records[-1][1]
    assert terminal["guardian_receipt"]["status"] == "timed_out"
    assert terminal["process_group_drained"] is True
