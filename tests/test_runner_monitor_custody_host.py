# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Real local processes and Journal rows; memory is not PostgreSQL proof."""

import sys

import pytest
from test_sandbox_command_ordinals_host import command_case as _command_case

from firefly_forge.journal import Journal
from firefly_forge.runner_monitor_models import MonitorStartInput, MonitorWaitInput
from firefly_forge.sandbox import SandboxReconciliationRequired
from firefly_forge.sandbox_models import SandboxInput, capture_sandbox

command_case = _command_case


@pytest.fixture(autouse=True)
def fixture_credential_admission(command_case):
    from firefly_forge.secrets import contains_known_credential

    def reject(value):
        if contains_known_credential(value):
            raise ValueError("Controlled fixture credential rejection")

    command_case.store.reject_credentials = reject


def request():
    return MonitorStartInput(
        argv=[sys.executable, "-B", "-u", "-c", "import time; print('ready'); time.sleep(30)"], timeout_seconds=5
    )


async def test_owned_handoff_allows_next_command_and_preserves_real_terminal(command_case):
    case = command_case
    case.store.run.data["sandbox_snapshot"] = capture_sandbox(SandboxInput(work_duration={}, process_monitor=True))
    executor = case.fresh()
    manager = await executor.monitor(case.run_id, case.workspace, "verify")
    try:
        started = await manager.start(request())
        identity = started["monitor_id"]
        await manager.wait(
            MonitorWaitInput(monitor_id=identity, timeout_seconds=2, condition={"kind": "output", "text": "ready"})
        )
        rows = await case.journal.sandbox_executions(case.run_id)
        assert len(rows) == 1 and "exit_code" not in rows[0]["effects"]["start"]["evidence"]
        result = await executor.execute(case.run_id, case.workspace, case.workspace, ["use-ready-service"], 2, "verify")
        assert result.exit_code == 0
        assert [r["ordinal"] for r in await case.journal.sandbox_executions(case.run_id)] == [1, 2]
        assert (await manager.cancel(identity))["status"] == "cancelled"
        monitor = next(
            row for row in await case.journal.sandbox_executions(case.run_id) if row["execution_id"] == identity
        )
        assert monitor["status"] == "removed"
        assert "exit_code" not in monitor["effects"]["start"]["evidence"]
        entries = await case.journal.entries(case.run_id)
        terminal = next(row for row in entries if row["kind"] == "runner_monitor_terminal")
        assert type(terminal["payload"]["exit_code"]) is int
        assert Journal.verify_chain(entries)
        assert (
            await executor.execute(case.run_id, case.workspace, case.workspace, ["after-cleanup"], 2, "verify")
        ).exit_code == 0
    finally:
        await manager.close()


async def test_monitor_consumes_existing_command_budget(command_case):
    case = command_case
    case.store.run.data["sandbox_snapshot"] = capture_sandbox(
        SandboxInput(work_duration={}, process_monitor=True, command_limit=1)
    )
    executor = case.fresh()
    manager = await executor.monitor(case.run_id, case.workspace, "verify")
    try:
        await manager.start(request())
        with pytest.raises(ValueError, match="budget"):
            await executor.execute(case.run_id, case.workspace, case.workspace, ["over-budget"], 2, "verify")
        assert case.calls == []
    finally:
        await manager.close()


async def test_new_executor_cannot_adopt_live_monitor_or_unlock_commands(command_case):
    case = command_case
    case.store.run.data["sandbox_snapshot"] = capture_sandbox(SandboxInput(work_duration={}, process_monitor=True))
    executor = case.fresh()
    manager = await executor.monitor(case.run_id, case.workspace, "verify")
    try:
        started = await manager.start(request())
        successor = case.fresh()
        observed = await successor.monitor(case.run_id, case.workspace, "verify")
        assert (await observed.status(started["monitor_id"]))["status"] == "needs_reconciliation"
        with pytest.raises(SandboxReconciliationRequired):
            await successor.execute(case.run_id, case.workspace, case.workspace, ["cannot-adopt"], 2, "verify")
        with pytest.raises(ValueError, match="owned"):
            await observed.cancel(started["monitor_id"])
        assert case.calls == []
    finally:
        await manager.close()


async def test_owned_http_server_wait_then_real_authorized_command_and_drain(command_case):
    import asyncio
    import json
    import socket

    from firefly_forge.runner_backends import RunnerBackends

    case = command_case
    case.store.run.data["sandbox_snapshot"] = capture_sandbox(
        SandboxInput(work_duration={}, process_monitor=True, command_limit=2, timeout=1, monitor_owner_grace_seconds=1)
    )
    executor = case.fresh()
    executor.backends = RunnerBackends()
    manager = await executor.monitor(case.run_id, case.workspace, "verify")
    script = (
        "from http.server import HTTPServer,BaseHTTPRequestHandler\n"
        "from pathlib import Path\nimport json\n"
        "class Handler(BaseHTTPRequestHandler):\n"
        " def do_GET(self):\n"
        "  self.send_response(200);self.end_headers();self.wfile.write(b'owned-monitor-ready')\n"
        " def log_message(self,*args): pass\n"
        "server=HTTPServer(('127.0.0.1',0),Handler)\n"
        "Path('monitor-ready.json').write_text(json.dumps({'port':server.server_address[1]}))\n"
        "print('ready',flush=True)\nserver.serve_forever()\n"
    )
    try:
        started = await manager.start(MonitorStartInput(argv=[sys.executable, "-B", "-u", "-c", script]))
        assert started["work_deadline_ms"] is None
        identity = started["monitor_id"]
        await asyncio.sleep(1.3)
        evidence = await manager.wait(
            MonitorWaitInput(
                monitor_id=identity, timeout_seconds=3, condition={"kind": "file", "path": "monitor-ready.json"}
            )
        )
        assert evidence["outcome"] == "satisfied"
        assert evidence["observation"]["ownership_expires_at_ms"] > started["ownership_expires_at_ms"]
        port = json.loads((case.workspace / "monitor-ready.json").read_bytes())["port"]
        query = (
            "from urllib.request import build_opener,ProxyHandler\n"
            "with build_opener(ProxyHandler({})).open('http://127.0.0.1:" + str(port) + "',timeout=2) as response:\n"
            " data=response.read(1024);assert data==b'owned-monitor-ready';print(data.decode())\n"
        )
        response = await executor.execute(
            case.run_id, case.workspace, case.workspace, [sys.executable, "-B", "-I", "-c", query], 3, "verify"
        )
        assert response.exit_code == 0 and response.output.strip() == "owned-monitor-ready"
        assert (await manager.status(identity))["status"] == "running"
        assert (await manager.cancel(identity))["status"] == "cancelled"
        with pytest.raises(OSError), socket.create_connection(("127.0.0.1", port), timeout=0.2):
            pass
        rows = await case.journal.sandbox_executions(case.run_id)
        assert sorted(row["ordinal"] for row in rows) == [1, 2]
        assert all(row["status"] == "removed" for row in rows)
        with pytest.raises(ValueError, match="budget"):
            await executor.execute(case.run_id, case.workspace, case.workspace, [sys.executable, "-V"], 1, "verify")
        entries = await case.journal.entries(case.run_id)
        assert Journal.verify_chain(entries)
        terminal = next(row["payload"] for row in entries if row["kind"] == "runner_monitor_terminal")
        assert terminal["process_group_drained"] is True
        assert terminal["guardian_receipt"]["child_reaped"] is True
        assert case.calls == []
    finally:
        await manager.close()


async def test_failed_terminal_transaction_cannot_finish_invocation_as_settled(command_case, monkeypatch):
    case = command_case
    case.store.run.data["sandbox_snapshot"] = capture_sandbox(SandboxInput(work_duration={}, process_monitor=True))
    manager = await case.fresh().monitor(case.run_id, case.workspace, "verify")
    started = await manager.start(request())
    original = case.journal.sandbox_effect_receipt

    async def fail_cleanup(*args, **kwargs):
        if kwargs["effect"] == "cleanup":
            raise ValueError("fictional terminal transaction failure")
        return await original(*args, **kwargs)

    monkeypatch.setattr(case.journal, "sandbox_effect_receipt", fail_cleanup)
    try:
        assert (await manager.cancel(started["monitor_id"]))["status"] == "needs_reconciliation"
        assert not any(row["kind"] == "runner_monitor_terminal" for row in await case.journal.entries(case.run_id))
        with pytest.raises(SandboxReconciliationRequired, match="reconciliation"):
            await manager.close()
    finally:
        await manager.transport.close()


@pytest.mark.parametrize(
    "field,value", [("parent_attempt", 99), ("snapshot_digest", "f" * 64), ("work_deadline_ms", 1)]
)
async def test_closed_monitor_refuses_rechained_terminal_scope_drift(command_case, field, value):
    import copy

    from firefly_forge.journal import digest
    from firefly_forge.runner_monitor_custody import monitor_admits
    from firefly_forge.sandbox_command_slots import _canonical

    case = command_case
    case.store.run.data["sandbox_snapshot"] = capture_sandbox(SandboxInput(work_duration={}, process_monitor=True))
    manager = await case.fresh().monitor(case.run_id, case.workspace, "verify")
    try:
        started = await manager.start(request())
        await manager.cancel(started["monitor_id"])
        row = copy.deepcopy(next(r for r in case.store.sandboxes.rows if r.id.hex == started["monitor_id"]))
        entries = copy.deepcopy(await case.journal.entries(case.run_id))
        original = _canonical(row, entries, case.run_id)
        assert monitor_admits(row, original, entries)
        terminal = next(e for e in entries if e["kind"] == "runner_monitor_terminal")
        terminal["payload"][field] = value
        terminal_digest = None
        for entry in entries:
            if (
                terminal_digest is not None
                and entry["kind"] in {"sandbox_intent", "sandbox_receipt"}
                and entry["payload"].get("effect") == "cleanup"
                and "evidence" in entry["payload"]
            ):
                entry["payload"]["evidence"]["monitor_terminal_digest"] = terminal_digest
            entry["previous_hash"] = entries[entry["sequence"] - 2]["digest"] if entry["sequence"] > 1 else "0" * 64
            entry["digest"] = digest({k: v for k, v in entry.items() if k != "digest"})
            if entry is terminal:
                terminal_digest = entry["digest"]
        row.data["effects"]["cleanup"]["evidence"]["monitor_terminal_digest"] = terminal_digest
        assert Journal.verify_chain(entries)
        canonical = _canonical(row, entries, case.run_id)
        assert not monitor_admits(row, canonical, entries)
    finally:
        await manager.close()


@pytest.mark.parametrize("project,monitor,chosen,expected", [(3, 2, 5, 2), (3, None, 5, 3)])
async def test_monitor_binds_smallest_explicit_work_cap(command_case, project, monitor, chosen, expected):
    case = command_case
    case.store.run.data["sandbox_snapshot"] = capture_sandbox(
        SandboxInput(work_duration={"timeout_seconds": project}, process_monitor=True, monitor_timeout_seconds=monitor)
    )
    manager = await case.fresh().monitor(case.run_id, case.workspace, "verify")
    try:
        started = await manager.start(request().model_copy(update={"timeout_seconds": chosen}))
        assert started["work_deadline_ms"] - started["issued_at_ms"] == expected * 1000
        row = next(
            r for r in await case.journal.sandbox_executions(case.run_id) if r["execution_id"] == started["monitor_id"]
        )
        assert row["request"]["request"]["timeout"] == expected
    finally:
        await manager.close()
