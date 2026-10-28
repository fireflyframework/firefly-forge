# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""A guardian may signal only its direct child whose identity it has not reaped."""

import signal
from types import SimpleNamespace

import pytest

from firefly_forge import runner_monitor_guardian as guardian


@pytest.mark.parametrize("failure", ["reaped", "foreign_identity"])
def test_lost_child_reservation_never_signals_foreign_group(monkeypatch, failure):
    calls = []

    def observe(*args):
        if failure == "reaped":
            raise ChildProcessError("fictional reaped child; numeric PID may be reused")
        return SimpleNamespace(si_pid=999)

    monkeypatch.setattr(guardian.os, "waitid", observe)
    monkeypatch.setattr(guardian.os, "killpg", lambda *args: calls.append(args))
    with pytest.raises((ChildProcessError, ValueError)):
        guardian.signal_reserved(123, signal.SIGTERM)
    assert calls == []


def test_absent_nonreaping_primitive_is_unavailable_before_spawn(monkeypatch):
    monkeypatch.delattr(guardian.os, "WNOWAIT")
    with pytest.raises(ValueError, match="requires non-reaping POSIX"):
        guardian.require_supported()
