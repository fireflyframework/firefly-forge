# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""A fake agent drives actual public tool callbacks without any model invocation."""

import sys
from types import SimpleNamespace

import pytest

from firefly_forge.agentic_runtime import RuntimeReport, execute_agentic
from firefly_forge.runner_monitor_models import MonitorStartInput, MonitorWaitInput
from firefly_forge.runner_monitor_transport import OwnedProcessMonitor
from firefly_forge.workspace_tools import ToolRuntimeContext

TOOLS = {"start_process", "process_status", "process_logs", "wait_process", "cancel_process"}


async def test_public_tools_wait_then_use_service_without_model_polling(tmp_path, monkeypatch):
    records, calls = [], []

    async def record(kind, payload):
        records.append(kind)

    transport = OwnedProcessMonitor(tmp_path, record=record)
    manager = SimpleNamespace(
        start=lambda request: transport.start("a" * 32, request),
        status=transport.status,
        logs=transport.logs,
        wait=transport.wait,
        cancel=transport.cancel,
        close=transport.close,
    )

    class Agent:
        def __init__(self, **kwargs):
            self.tools = {tool.name: tool for tool in kwargs["tools"]}

        async def run(self, *args, **kwargs):
            assert self.tools.keys() >= TOOLS
            child = [sys.executable, "-B", "-u", "-c", "import time; print('ready'); time.sleep(30)"]
            started = await self.tools["start_process"].execute(
                request=MonitorStartInput(argv=child, timeout_seconds=5)
            )
            waited = await self.tools["wait_process"].execute(
                request=MonitorWaitInput(
                    monitor_id=started["monitor_id"], condition={"kind": "output", "text": "ready"}, timeout_seconds=2
                )
            )
            assert waited["outcome"] == "satisfied"
            assert (await self.tools["process_status"].execute(monitor_id=started["monitor_id"]))["status"] == "running"
            assert "ready" in (await self.tools["process_logs"].execute(monitor_id=started["monitor_id"]))["output"]
            await self.tools["write_file"].execute(path="reaction.txt", content="service was ready")
            await self.tools["cancel_process"].execute(monitor_id=started["monitor_id"])
            calls.append("one agent execution; no model")
            return SimpleNamespace(
                output=RuntimeReport(summary="reacted"),
                usage=SimpleNamespace(input_tokens=0, output_tokens=0, requests=0),
            )

    monkeypatch.setattr("fireflyframework_agentic.agents.FireflyAgent", Agent)

    async def emit(*args):
        pass

    context = ToolRuntimeContext(
        scope_root=tmp_path,
        repository_root=tmp_path,
        allowed_tools=frozenset(TOOLS | {"execute_command", "write_file"}),
        process_monitor=manager,
    )
    summary, usage = await execute_agentic(
        tmp_path, "Use the ready service", "pyfly", "none", emit, model=object(), runtime_context=context
    )
    assert summary == "reacted" and usage["requests"] == 0
    assert usage["files_written"] == ["reaction.txt"]
    assert calls == ["one agent execution; no model"]
    assert records == ["monitor_spawn_intent", "monitor_started", "monitor_terminal"]
    assert transport._closed


async def test_granted_monitor_without_configured_context_fails_before_agent(tmp_path):
    async def emit(*args):
        pass

    with pytest.raises(ValueError, match="monitor.*configured|monitor.*available"):
        await execute_agentic(
            tmp_path,
            "wait",
            "none",
            "none",
            emit,
            model=object(),
            runtime_context=ToolRuntimeContext(
                scope_root=tmp_path, repository_root=tmp_path, allowed_tools=frozenset(TOOLS | {"execute_command"})
            ),
        )
