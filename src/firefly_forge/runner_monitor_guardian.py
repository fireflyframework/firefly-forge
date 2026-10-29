# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Private host guardian: reserve the child PID until group cleanup finishes."""

from __future__ import annotations

import json
import os
import select
import signal
import subprocess
import sys
import time
from typing import Any


def require_supported() -> None:
    if not all(hasattr(os, name) for name in ("waitid", "WNOWAIT", "P_PID", "WEXITED", "WNOHANG", "killpg")):
        raise ValueError("Owned process monitoring requires non-reaping POSIX child observation")
    try:
        os.waitid(os.P_PID, os.getpid(), os.WEXITED | os.WNOHANG | os.WNOWAIT)
    except ChildProcessError:
        pass
    except (OSError, NotImplementedError) as exc:
        raise ValueError("Non-reaping POSIX child observation is unavailable") from exc


def reserved(pid: int) -> Any:
    observation = os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
    if observation is not None and observation.si_pid != pid:
        raise ValueError("Owned child identity changed; no signal is permitted")
    return observation


def signal_reserved(pid: int, sig: signal.Signals) -> str:
    observation = reserved(pid)  # ECHILD refuses signaling after another owner reaps the identity.
    try:
        os.killpg(pid, sig)
    except ProcessLookupError:
        return "absent"
    except PermissionError:
        if observation is None or runnable_group(pid):
            raise
        return "quiescent"
    return "sent"


def runnable_group(pid: int) -> list[int]:
    result = subprocess.run(
        ["/bin/ps", "-g", str(pid), "-o", "pid=", "-o", "pgid=", "-o", "stat="],
        capture_output=True,
        check=False,
        timeout=2,
        env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"},
    )
    if result.returncode not in {0, 1} or len(result.stdout) > 65536 or result.stderr:
        raise ValueError("Owned process group census could not be bounded")
    active = []
    for line in result.stdout.decode().splitlines():
        identity, group, state = line.split()
        if int(group) != pid:
            raise ValueError("Process census crossed the owned group")
        if not state.startswith("Z"):
            active.append(int(identity))
    return active


def drain(pid: int) -> dict[str, Any]:
    terminated = signal_reserved(pid, signal.SIGTERM)
    end = time.monotonic() + 2
    while reserved(pid) is None and time.monotonic() < end:
        time.sleep(0.02)
    killed = signal_reserved(pid, signal.SIGKILL)
    observed, status = os.waitpid(pid, 0)
    if observed != pid:
        raise ValueError("Owned child reap changed")
    if runnable_group(pid):
        raise ValueError("Owned process group retains runnable members")
    return {
        "exit_code": os.waitstatus_to_exitcode(status),
        "term_signal": terminated,
        "kill_signal": killed,
        "child_reaped": True,
        "process_group_drained": True,
    }


def main() -> None:
    require_supported()
    request = json.loads(sys.argv[1])
    argv, work_deadline, token = request["argv"], request["work_deadline_ns"], request["token"]
    grace = request["owner_grace_seconds"]
    if (
        type(argv) is not list
        or not 1 <= len(argv) <= 64
        or any(type(part) is not str or not part or "\x00" in part for part in argv)
        or type(grace) is not int
        or grace < 1
        or (work_deadline is not None and (type(work_deadline) is not int or work_deadline < 1))
        or type(token) is not str
        or len(token) != 32
    ):
        raise ValueError("Bounded private guardian request required")

    if work_deadline is not None and time.monotonic_ns() >= work_deadline:
        raise ValueError("Chosen work deadline elapsed before dispatch")

    def report(stage: str, **value: Any) -> None:
        print(json.dumps({"version": 1, "stage": stage, "token": token, **value}), file=sys.stderr, flush=True)

    child = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=sys.stdout.buffer,
        stderr=sys.stdout.buffer,
        start_new_session=True,
        close_fds=True,
    )
    status = "needs_reconciliation"
    try:
        if os.getpgid(child.pid) != child.pid:
            raise ValueError("Child did not establish its owned process group")
        report("started", pid=child.pid, pgid=child.pid)
        owner_deadline = time.monotonic_ns() + grace * 1_000_000_000
        sequence, buffer = 0, b""
        while True:
            if reserved(child.pid) is not None:
                status = "exited"
                break
            now = time.monotonic_ns()
            if now >= owner_deadline:
                status = "owner_lost"
                break
            if work_deadline is not None and now >= work_deadline:
                status = "timed_out"
                break
            remaining = owner_deadline - now
            if work_deadline is not None:
                remaining = min(remaining, work_deadline - now)
            readable, _, _ = select.select([sys.stdin.fileno()], [], [], min(50_000_000, remaining) / 1_000_000_000)
            if readable:
                data = os.read(sys.stdin.fileno(), 4096)
                if not data:
                    status = "owner_lost"
                    break
                buffer += data
                if len(buffer) > 4096:
                    raise ValueError("Bounded guardian control frame required")
                while b"\n" in buffer:
                    raw, buffer = buffer.split(b"\n", 1)
                    command = json.loads(raw)
                    if command == {"kind": "cancel", "token": token}:
                        status = "cancelled"
                        break
                    if (
                        set(command) != {"kind", "token", "sequence"}
                        or command["kind"] != "heartbeat"
                        or command["token"] != token
                        or type(command["sequence"]) is not int
                        or command["sequence"] != sequence + 1
                    ):
                        raise ValueError("Exact sequenced guardian ownership heartbeat required")
                    sequence = command["sequence"]
                    owner_deadline = time.monotonic_ns() + grace * 1_000_000_000
                    report("heartbeat_ack", pid=child.pid, pgid=child.pid, sequence=sequence)
                if status == "cancelled":
                    break
    finally:
        evidence = drain(child.pid)
        child.returncode = evidence["exit_code"]
        report("terminal", pid=child.pid, pgid=child.pid, status=status, **evidence)


if __name__ == "__main__":
    main()
