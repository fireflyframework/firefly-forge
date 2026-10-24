# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""An exited owned group may report EPERM; only proven reaping and absence permit cleanup."""

import asyncio
import errno
import signal

import pytest

from firefly_forge import git_diff_artifacts


class Child:
    pid = 123456

    def __init__(self, *, stall=False, missing_status=False, first_wait_stalls=False):
        self.returncode = None
        self.waits = 0
        self.stall = stall
        self.missing_status = missing_status
        self.first_wait_stalls = first_wait_stalls

    async def wait(self):
        self.waits += 1
        if self.stall or self.first_wait_stalls and self.waits == 1:
            await asyncio.sleep(10)
        if not self.missing_status:
            self.returncode = 0
        return self.returncode


def signals(monkeypatch, child, *, final_probe="absent", term_allowed=False):
    denied = PermissionError(errno.EPERM, "Controlled owned-group permission result")
    calls = []

    def send(pid, kind):
        assert pid == child.pid
        calls.append(kind)
        if kind == 0:
            assert child.returncode is not None
            if final_probe == "absent":
                raise ProcessLookupError(errno.ESRCH, "Owned group absent after reap")
            if final_probe == "denied":
                raise PermissionError(errno.EPERM, "Current group membership is not confirmed")
            return
        if kind == signal.SIGTERM and term_allowed:
            return
        raise denied

    monkeypatch.setattr(git_diff_artifacts.os, "killpg", send)
    return denied, calls


async def test_permission_race_accepts_only_reaped_child_and_absent_group(monkeypatch):
    child = Child()
    _, calls = signals(monkeypatch, child)
    await git_diff_artifacts._stop(child)
    assert child.waits == 1 and child.returncode == 0
    assert calls == [signal.SIGTERM, 0]


@pytest.mark.parametrize("group", ["alive", "denied"])
async def test_reaped_child_cannot_hide_a_surviving_or_uninspectable_group(monkeypatch, group):
    child = Child()
    denied, calls = signals(monkeypatch, child, final_probe=group)
    with pytest.raises(PermissionError) as captured:
        await git_diff_artifacts._stop(child)
    assert captured.value is denied
    assert child.waits == 1 and calls == [signal.SIGTERM, 0]


async def test_live_permission_denial_remains_bounded_and_is_not_suppressed(monkeypatch):
    child = Child(stall=True)
    denied, calls = signals(monkeypatch, child)
    async with asyncio.timeout(2):
        with pytest.raises(PermissionError) as captured:
            await git_diff_artifacts._stop(child)
    assert captured.value is denied
    assert child.waits == 1 and child.returncode is None
    assert calls == [signal.SIGTERM]


async def test_wait_without_a_reaped_status_cannot_authorize_cleanup(monkeypatch):
    child = Child(missing_status=True)
    denied, calls = signals(monkeypatch, child)
    with pytest.raises(PermissionError) as captured:
        await git_diff_artifacts._stop(child)
    assert captured.value is denied
    assert child.waits == 1 and calls == [signal.SIGTERM]


async def test_kill_phase_permission_race_still_requires_group_absence(monkeypatch):
    child = Child(first_wait_stalls=True)
    _, calls = signals(monkeypatch, child, term_allowed=True)
    await git_diff_artifacts._stop(child)
    assert child.waits == 2 and child.returncode == 0
    assert calls == [signal.SIGTERM, signal.SIGKILL, 0]


async def test_completed_child_keeps_existing_no_signal_behavior(monkeypatch):
    child = Child()
    child.returncode = 0
    _, calls = signals(monkeypatch, child)
    await git_diff_artifacts._stop(child)
    assert child.waits == 0 and calls == []
