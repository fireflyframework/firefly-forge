# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Owned local children exercise monitor waits without providers or network."""

import asyncio
import sys

import pytest

from firefly_forge.runner_monitor_models import MonitorStartInput, MonitorWaitInput
from firefly_forge.runner_monitor_transport import OwnedProcessMonitor


def python(code):
    return [sys.executable, "-B", "-u", "-c", code]


@pytest.fixture
def recorded():
    rows = []

    async def record(kind, payload):
        rows.append((kind, payload))

    return rows, record


async def test_start_wait_ready_then_react_and_cancel_owned_child(tmp_path, recorded):
    rows, record = recorded
    manager = OwnedProcessMonitor(tmp_path, record=record)
    request = MonitorStartInput(
        argv=python(
            "from pathlib import Path; import time; Path('ready.txt').write_text('ready'); "
            "print('READY'); time.sleep(30)"
        ),
        timeout_seconds=5,
    )
    try:
        started = await manager.start("a" * 32, request)
        assert started["status"] == "running"
        evidence = await manager.wait(
            MonitorWaitInput(
                monitor_id="a" * 32,
                timeout_seconds=2,
                condition={"kind": "output", "text": "READY"},
            )
        )
        assert evidence["outcome"] == "satisfied"
        assert evidence["observation"]["status"] == "running"
        # A different authorized operation can use the ready service's output.
        from firefly_forge.process import execute

        result = await execute(python("from pathlib import Path; print(Path('ready.txt').read_text())"), tmp_path)
        assert result.exit_code == 0 and result.output.strip() == "ready"
        logs = await manager.logs("a" * 32)
        assert logs["output"] == "READY\n"
        stopped = await manager.cancel("a" * 32)
        assert stopped["status"] == "cancelled"
        assert stopped["exit_code"] is not None
        assert [kind for kind, _ in rows] == ["monitor_spawn_intent", "monitor_started", "monitor_terminal"]
        assert rows[-1][1]["process_group_drained"] is True
        assert (await manager.cancel("a" * 32)) == stopped
    finally:
        await manager.close()


async def test_exit_wait_has_exact_output_and_terminal_receipt(tmp_path, recorded):
    rows, record = recorded
    async with OwnedProcessMonitor(tmp_path, record=record) as manager:
        await manager.start(
            "b" * 32, MonitorStartInput(argv=python("print('done'); raise SystemExit(3)"), timeout_seconds=5)
        )
        result = await manager.wait(
            MonitorWaitInput(
                monitor_id="b" * 32,
                timeout_seconds=2,
                condition={"kind": "exit"},
            )
        )
        assert result["outcome"] == "satisfied"
        assert result["observation"]["exit_code"] == 3
        assert (await manager.logs("b" * 32))["output"] == "done\n"
        assert rows[-1][0] == "monitor_terminal"


async def test_file_wait_is_confined_and_returns_content_hash(tmp_path, recorded):
    import hashlib

    _, record = recorded
    async with OwnedProcessMonitor(tmp_path, record=record) as manager:
        await manager.start(
            "c" * 32,
            MonitorStartInput(
                argv=python(
                    "from pathlib import Path; import time; time.sleep(.05); "
                    "Path('event').write_text('ok'); time.sleep(30)"
                ),
                timeout_seconds=5,
            ),
        )
        expected = hashlib.sha256(b"ok").hexdigest()
        result = await manager.wait(
            MonitorWaitInput(
                monitor_id="c" * 32,
                timeout_seconds=2,
                condition={"kind": "file", "path": "event", "sha256": expected},
            )
        )
        assert result["outcome"] == "satisfied" and result["file_sha256"] == expected
        (tmp_path / "linked").symlink_to(tmp_path / "event")
        with pytest.raises(ValueError, match="link"):
            await manager.wait(
                MonitorWaitInput(
                    monitor_id="c" * 32,
                    timeout_seconds=1,
                    condition={"kind": "file", "path": "linked"},
                )
            )


async def test_explicit_timeout_and_output_overflow_drain_without_restarting(tmp_path, recorded):
    rows, record = recorded
    async with OwnedProcessMonitor(tmp_path, record=record, max_output_bytes=1024) as manager:
        await manager.start("d" * 32, MonitorStartInput(argv=python("import time; time.sleep(30)"), timeout_seconds=1))
        await asyncio.sleep(1.1)
        assert (await manager.status("d" * 32))["status"] == "timed_out"
        await manager.start(
            "e" * 32,
            MonitorStartInput(argv=python("print('x' * 2048); import time; time.sleep(30)"), timeout_seconds=5),
        )
        result = await manager.wait(
            MonitorWaitInput(monitor_id="e" * 32, timeout_seconds=2, condition={"kind": "exit"})
        )
        assert result["observation"]["status"] == "output_limit"
        assert len((await manager.logs("e" * 32))["output"].encode()) <= 1024
        with pytest.raises(ValueError, match="already"):
            await manager.start("d" * 32, MonitorStartInput(argv=python("print('replay')"), timeout_seconds=1))
        assert sum(kind == "monitor_started" for kind, _ in rows) == 2


async def test_restart_never_adopts_or_dispatches_retained_handle(tmp_path, recorded):
    rows, record = recorded
    async with OwnedProcessMonitor(tmp_path, record=record, retained_ids=frozenset({"f" * 32})) as manager:
        assert (await manager.status("f" * 32))["status"] == "needs_reconciliation"
        with pytest.raises(ValueError, match="Retained"):
            await manager.start("f" * 32, MonitorStartInput(argv=python("print('replay')")))
        with pytest.raises(ValueError, match="owned"):
            await manager.cancel("f" * 32)
        assert rows == []


async def test_stopping_wait_preserves_service_until_invocation_cleanup(tmp_path, recorded):
    _, record = recorded
    async with OwnedProcessMonitor(tmp_path, record=record) as manager:
        await manager.start("1" * 32, MonitorStartInput(argv=python("import time; time.sleep(30)"), timeout_seconds=5))
        task = asyncio.create_task(
            manager.wait(
                MonitorWaitInput(monitor_id="1" * 32, timeout_seconds=3, condition={"kind": "output", "text": "never"})
            )
        )
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert (await manager.status("1" * 32))["status"] == "running"
    assert (await manager.status("1" * 32))["status"] == "cancelled"


@pytest.mark.parametrize(
    "condition",
    [
        {"kind": "file", "path": "../outside"},
        {"kind": "file", "path": ".env"},
        {"kind": "file", "path": ".git/config"},
        {"kind": "output", "text": ""},
        {"kind": "exit", "unknown": True},
    ],
)
def test_wait_conditions_reject_unsafe_or_ambiguous_input(condition):
    with pytest.raises(ValueError):
        MonitorWaitInput(monitor_id="a" * 32, condition=condition)


async def test_immediate_cancel_drains_before_supervisor_first_turn(tmp_path, recorded):
    rows, record = recorded
    async with OwnedProcessMonitor(tmp_path, record=record) as manager:
        await manager.start("2" * 32, MonitorStartInput(argv=python("import time; time.sleep(30)"), timeout_seconds=5))
        assert (await manager.cancel("2" * 32))["status"] == "cancelled"
        assert rows[-1][1]["process_group_drained"] is True


async def test_uncertain_start_receipt_drains_without_running_again(tmp_path):
    async def record(kind, payload):
        if kind == "monitor_started":
            raise ValueError("fictional failed durable handoff")

    async with OwnedProcessMonitor(tmp_path, record=record) as manager:
        with pytest.raises(ValueError, match="failed durable"):
            await manager.start(
                "3" * 32,
                MonitorStartInput(
                    argv=python(
                        "import time; from pathlib import Path; time.sleep(.2); Path('replayed').write_text('bad')"
                    ),
                    timeout_seconds=5,
                ),
            )
        await asyncio.sleep(0.3)
        assert not (tmp_path / "replayed").exists()
        assert (await manager.status("3" * 32))["status"] == "needs_reconciliation"


@pytest.mark.parametrize(
    "secret,first",
    [
        ("fixture-monitor-token-secret", b"fixture-monitor"),
        ("fixture-\U0001f352-token", b"fixture-\xf0"),
    ],
)
async def test_logs_redact_credentials_even_while_the_last_chunk_is_incomplete(tmp_path, recorded, secret, first):
    _, record = recorded
    prefix = first.decode("utf-8", errors="ignore")
    rest = secret.encode()[len(first) :] + b"\n"
    async with OwnedProcessMonitor(
        tmp_path, record=record, credential_registry=lambda: {"API_TOKEN": secret}
    ) as manager:
        await manager.start(
            "4" * 32,
            MonitorStartInput(
                argv=python(
                    f"import sys,time;sys.stdout.buffer.write({first!r});sys.stdout.flush();"
                    f"time.sleep(.4);sys.stdout.buffer.write({rest!r});sys.stdout.flush();time.sleep(30)"
                ),
                timeout_seconds=5,
            ),
        )
        await manager.wait(
            MonitorWaitInput(monitor_id="4" * 32, timeout_seconds=2, condition={"kind": "output", "text": prefix})
        )
        early = await manager.logs("4" * 32)
        assert prefix not in early["output"]
        await asyncio.sleep(0.5)
        later = await manager.logs("4" * 32)
        assert secret not in later["output"] and "[redacted]" in later["output"]
        import hashlib

        assert later["output_sha256"] == hashlib.sha256(later["output"].encode()).hexdigest()


async def test_file_wait_rechecks_a_replaced_parent_directory(tmp_path, recorded, monkeypatch):
    _, record = recorded
    (tmp_path / "public").mkdir()
    (tmp_path / "public/event").write_text("public")
    outside = tmp_path.parent / (tmp_path.name + "-outside")
    outside.mkdir()
    (outside / "event").write_text("private")
    async with OwnedProcessMonitor(tmp_path, record=record) as manager:
        await manager.start("5" * 32, MonitorStartInput(argv=python("import time; time.sleep(30)"), timeout_seconds=5))
        original = manager.files._source_absence

        def replace(path):
            observed = original(path)
            (tmp_path / "public").rename(tmp_path / "retained-public")
            (tmp_path / "public").symlink_to(outside, target_is_directory=True)
            return observed

        monkeypatch.setattr(manager.files, "_source_absence", replace)
        with pytest.raises(ValueError, match="symlink|linked|directory"):
            await manager.wait(
                MonitorWaitInput(monitor_id="5" * 32, condition={"kind": "file", "path": "public/event"})
            )


async def test_wait_timeout_preserves_service_and_cleanup_kills_descendant(tmp_path, recorded):
    _, record = recorded
    code = (
        "import subprocess,sys,time; subprocess.Popen([sys.executable,'-B','-c',"
        "\"import time; from pathlib import Path; time.sleep(2); Path('escaped').write_text('bad')\"]); "
        "print('ready'); time.sleep(30)"
    )
    async with OwnedProcessMonitor(tmp_path, record=record) as manager:
        await manager.start("6" * 32, MonitorStartInput(argv=python(code), timeout_seconds=5))
        result = await manager.wait(
            MonitorWaitInput(monitor_id="6" * 32, timeout_seconds=1, condition={"kind": "output", "text": "never"})
        )
        assert result["outcome"] == "timeout" and result["observation"]["status"] == "running"
    await asyncio.sleep(1.2)
    assert not (tmp_path / "escaped").exists()


async def test_manager_never_signals_an_unowned_numeric_pid(tmp_path, recorded, monkeypatch):
    _, record = recorded
    calls = []

    def foreign_signal(pid, sig):
        calls.append((pid, sig))
        raise AssertionError("Manager must use its owned guardian control pipe")

    async with OwnedProcessMonitor(tmp_path, record=record) as manager:
        started = await manager.start(
            "7" * 32,
            MonitorStartInput(argv=python("import time; print('ready', flush=True); time.sleep(2)"), timeout_seconds=3),
        )
        await manager.wait(MonitorWaitInput(monitor_id="7" * 32, condition={"kind": "output", "text": "ready"}))
        monkeypatch.setattr("os.killpg", foreign_signal)
        stopped = await manager.cancel(started["monitor_id"])
        assert stopped["status"] == "cancelled" and calls == []


async def test_file_wait_streams_larger_file_than_log_response_window(tmp_path, recorded):
    import hashlib

    _, record = recorded
    body = b"public-file-event" * 1000
    (tmp_path / "large-event").write_bytes(body)
    async with OwnedProcessMonitor(tmp_path, record=record, max_output_bytes=1024) as manager:
        await manager.start("8" * 32, MonitorStartInput(argv=python("import time;time.sleep(30)"), timeout_seconds=3))
        evidence = await manager.wait(
            MonitorWaitInput(
                monitor_id="8" * 32,
                condition={"kind": "file", "path": "large-event", "sha256": hashlib.sha256(body).hexdigest()},
            )
        )
        assert evidence["outcome"] == "satisfied"


async def test_file_wait_refuses_parent_replacement_after_open(tmp_path, recorded, monkeypatch):
    from contextlib import contextmanager

    _, record = recorded
    (tmp_path / "public").mkdir()
    (tmp_path / "public/event").write_text("public")
    outside = tmp_path.parent / (tmp_path.name + "-outside")
    outside.mkdir()
    (outside / "event").write_text("outside")
    async with OwnedProcessMonitor(tmp_path, record=record) as manager:
        await manager.start("9" * 32, MonitorStartInput(argv=python("import time;time.sleep(30)"), timeout_seconds=3))
        original, first = manager.files._source_file, True

        @contextmanager
        def changed(path):
            nonlocal first
            with original(path) as stream:
                if first:
                    first = False
                    (tmp_path / "public").rename(tmp_path / "retained")
                    (tmp_path / "public").symlink_to(outside, target_is_directory=True)
                yield stream

        monkeypatch.setattr(manager.files, "_source_file", changed)
        with pytest.raises(ValueError, match="symlink|linked|directory"):
            await manager.wait(
                MonitorWaitInput(monitor_id="9" * 32, condition={"kind": "file", "path": "public/event"})
            )


async def test_file_wait_does_not_return_a_known_credential_hash(tmp_path, recorded):
    _, record = recorded
    secret = "fixture-file-event-token"
    (tmp_path / "event").write_text("x" * 65530 + secret)
    async with OwnedProcessMonitor(
        tmp_path, record=record, credential_registry=lambda: {"API_TOKEN": secret}
    ) as manager:
        await manager.start("b" * 32, MonitorStartInput(argv=python("import time;time.sleep(30)"), timeout_seconds=3))
        with pytest.raises(ValueError, match="credentials"):
            await manager.wait(MonitorWaitInput(monitor_id="b" * 32, condition={"kind": "file", "path": "event"}))


async def test_exit_condition_is_unavailable_when_terminal_custody_cannot_be_verified(tmp_path, recorded, monkeypatch):
    _, record = recorded
    async with OwnedProcessMonitor(tmp_path, record=record) as manager:
        original = manager._control

        async def invalid_terminal(handle):
            await original(handle)
            handle.receipt.clear()
            raise ValueError("Fictional invalid terminal receipt")

        monkeypatch.setattr(manager, "_control", invalid_terminal)
        await manager.start("c" * 32, MonitorStartInput(argv=python("print('finished')")))
        result = await manager.wait(MonitorWaitInput(monitor_id="c" * 32, condition={"kind": "exit"}))
        assert result["observation"]["status"] == "needs_reconciliation"
        assert result["outcome"] == "unavailable"


async def test_output_wait_uses_the_same_protected_projection_as_logs(tmp_path, recorded):
    _, record = recorded
    async with OwnedProcessMonitor(
        tmp_path, record=record, credential_registry=lambda: {"API_TOKEN": "readiness-fixture-protected"}
    ) as manager:
        await manager.start(
            "d" * 32,
            MonitorStartInput(argv=python("import time;print('readiness-fixture',end='',flush=True);time.sleep(30)")),
        )
        result = await manager.wait(
            MonitorWaitInput(
                monitor_id="d" * 32, condition={"kind": "output", "text": "readiness-fixture"}, timeout_seconds=1
            )
        )
        assert "readiness-fixture" not in (await manager.logs("d" * 32))["output"]
        assert result["outcome"] == "timeout"


@pytest.mark.parametrize("barrier", ["drain", "terminal_record"])
@pytest.mark.parametrize("failure", [False, True])
async def test_terminal_observation_waits_for_verified_drain_and_commit(tmp_path, monkeypatch, barrier, failure):
    from contextlib import ExitStack
    from types import SimpleNamespace

    from firefly_forge.runner_monitor_transport import _Handle

    entered, release = asyncio.Event(), asyncio.Event()

    class PrivatePipe:
        def write(self, value):
            pass

        async def drain(self):
            pass

    async def record(kind, payload):
        if barrier == "terminal_record":
            entered.set()
            await release.wait()
            if failure:
                raise ValueError("Fictional terminal transaction rollback")

    async def drain(handle):
        if barrier == "drain":
            entered.set()
            await release.wait()
            if failure:
                raise ValueError("Fictional unverifiable process drain")
        handle.receipt = {"exit_code": 3, "status": "exited", "process_group_drained": True}

    async with OwnedProcessMonitor(tmp_path, record=record) as manager:
        handle = _Handle(
            "e" * 32, SimpleNamespace(stdin=PrivatePipe(), returncode=0), 1, 30001, None, ExitStack(), "f" * 32, 123
        )
        handle.reader = asyncio.create_task(asyncio.sleep(0))
        handle.control = asyncio.create_task(asyncio.sleep(0))
        manager._handles[handle.identity] = handle
        monkeypatch.setattr(manager, "_drain", drain)
        handle.task = asyncio.create_task(manager._supervise(handle))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            observed = await manager.status(handle.identity)
            assert observed["status"] == "settling" and observed["exit_code"] is None
        finally:
            release.set()
            await handle.task
        final = await manager.status(handle.identity)
        assert final["status"] == ("needs_reconciliation" if failure else "exited")
        if failure:
            waited = await manager.wait(MonitorWaitInput(monitor_id=handle.identity, condition={"kind": "exit"}))
            assert waited["outcome"] == "unavailable"
        else:
            assert final["exit_code"] == 3


async def test_parallel_owned_processes_keep_waits_logs_and_cancellation_independent(tmp_path, recorded):
    rows, record = recorded
    identities = ("d" * 32, "e" * 32)
    async with OwnedProcessMonitor(tmp_path, record=record, owner_grace_seconds=1) as manager:
        await asyncio.gather(
            *(
                manager.start(
                    identity,
                    MonitorStartInput(
                        argv=python(
                            "from pathlib import Path; import time; "
                            f"print('{identity[0]} ready'); "
                            f"\nwhile not Path('{identity[0]}.finish').exists(): time.sleep(.01)"
                            f"\nprint('{identity[0]} done')"
                        )
                    ),
                )
                for identity in identities
            )
        )
        ready = await asyncio.gather(
            *(
                manager.wait(
                    MonitorWaitInput(
                        monitor_id=identity,
                        condition={"kind": "output", "text": identity[0] + " ready"},
                    )
                )
                for identity in identities
            )
        )
        assert all(result["outcome"] == "satisfied" for result in ready)
        assert all(result["observation"]["work_deadline_ms"] is None for result in ready)
        first = await manager.cancel(identities[0])
        assert first["status"] == "cancelled"
        assert first["exit_code"] is not None
        assert (await manager.status(identities[1]))["status"] == "running"
        assert (await manager.logs(identities[1]))["output"] == "e ready\n"
        (tmp_path / "e.finish").write_text("continue")
        second = await manager.wait(MonitorWaitInput(monitor_id=identities[1], condition={"kind": "exit"}))
        assert second["outcome"] == "satisfied"
        assert second["observation"]["exit_code"] == 0
        assert (await manager.logs(identities[1]))["output"] == "e ready\ne done\n"
        for identity in identities:
            records = [(kind, value) for kind, value in rows if value["monitor_id"] == identity]
            assert [kind for kind, _ in records] == ["monitor_spawn_intent", "monitor_started", "monitor_terminal"]
            assert records[-1][1]["process_group_drained"] is True
            assert records[0][1]["work_deadline_ms"] is None
