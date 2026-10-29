# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Owned MCP work waits beyond control deadlines without retrying a lost call."""

import asyncio
import json
from pathlib import Path

import httpx2
import pytest
from test_mcp_registry import approved, protocol

from firefly_forge.agentic_checkpoint_invocation import runtime_contract
from firefly_forge.source_budget_allowances import admission_contract_digest
from firefly_forge.work_duration_models import WorkDurationPolicyV2


def accelerated_control_deadlines(monkeypatch, module):
    real = asyncio.timeout

    def timeout(value):
        return real(0.05 if value in {15, 30} else value)

    monkeypatch.setattr(module.asyncio, "timeout", timeout)


def delayed_tool(service, *, wait=None):
    transport, _ = protocol()
    calls = []
    entered, closed = asyncio.Event(), asyncio.Event()
    timeouts = []

    async def response(request):
        is_call = request.method == "POST" and json.loads(request.content).get("method") == "tools/call"
        if is_call:
            calls.append(json.loads(request.content))
            timeouts.append(request.extensions.get("timeout"))
            entered.set()
            try:
                if wait is None:
                    await asyncio.sleep(0.1)
                else:
                    await wait.wait()
            finally:
                closed.set()
        return await transport.handle_async_request(request)

    service.transport = httpx2.MockTransport(response)
    return calls, entered, closed, timeouts


async def call(service, snapshot, **kwargs):
    return await service.call(
        snapshot,
        "github",
        "get_file_contents",
        {"owner": "team", "repo": "product"},
        roles=["implementer"],
        stage="implement",
        **kwargs,
    )


async def test_new_uncapped_mcp_waits_beyond_legacy_control_window_once(tmp_path, monkeypatch):
    module, service, _, _, snapshot = await approved(tmp_path)
    calls, _, closed, timeouts = delayed_tool(service)
    accelerated_control_deadlines(monkeypatch, module)
    result = await call(service, snapshot, work_duration=WorkDurationPolicyV2())
    assert result["text"] == ["Owned source receipt"] and result["untrusted"] is True
    assert sum(item["method"] == "tools/call" for item in calls) == 1 and closed.is_set()
    assert timeouts[0]["read"] is None
    assert timeouts[0]["connect"] == timeouts[0]["write"] == timeouts[0]["pool"] == 15


async def test_legacy_mcp_still_has_its_original_finite_session_window(tmp_path, monkeypatch):
    module, service, _, _, snapshot = await approved(tmp_path)
    _, entered, closed, timeouts = delayed_tool(service)
    accelerated_control_deadlines(monkeypatch, module)
    with pytest.raises(module.MCPAdmissionError):
        await call(service, snapshot)
    assert entered.is_set() and closed.is_set() and timeouts[0]["read"] == 15


async def test_new_chosen_mcp_deadline_stops_the_wait_without_a_second_dispatch(tmp_path):
    module, service, _, _, snapshot = await approved(tmp_path)
    calls, entered, closed, _ = delayed_tool(service, wait=asyncio.Event())
    with pytest.raises(module.MCPAdmissionError):
        await call(service, snapshot, work_duration=WorkDurationPolicyV2(timeout_seconds=1))
    assert entered.is_set() and closed.is_set()
    assert len(calls) == 1


async def test_uncapped_mcp_wait_is_cancellable_and_never_reissues_the_call(tmp_path):
    _, service, _, _, snapshot = await approved(tmp_path)
    calls, entered, closed, _ = delayed_tool(service, wait=asyncio.Event())
    task = asyncio.create_task(call(service, snapshot, work_duration=WorkDurationPolicyV2()))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert closed.is_set() and len(calls) == 1
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("phase", ["initialize", "tools/list"])
async def test_uncapped_work_does_not_remove_handshake_or_catalog_deadlines(tmp_path, monkeypatch, phase):
    module, service, _, _, snapshot = await approved(tmp_path)
    transport, calls = protocol()
    entered, closed = asyncio.Event(), asyncio.Event()

    async def stalled(request):
        if request.method == "POST" and json.loads(request.content).get("method") == phase:
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                closed.set()
        return await transport.handle_async_request(request)

    service.transport = httpx2.MockTransport(stalled)
    accelerated_control_deadlines(monkeypatch, module)
    with pytest.raises(module.MCPAdmissionError):
        await call(service, snapshot, work_duration=WorkDurationPolicyV2())
    assert entered.is_set() and closed.is_set()
    assert not any(item["method"] == "tools/call" for item in calls)


async def test_chosen_work_deadline_includes_handshake_without_restarting_at_tool_call(tmp_path):
    module, service, _, _, snapshot = await approved(tmp_path)
    transport, _ = protocol()
    dispatches = []

    async def slow(request):
        method = json.loads(request.content).get("method") if request.method == "POST" else None
        if method in {"initialize", "tools/call"}:
            dispatches.append(method)
            await asyncio.sleep(0.65)
        return await transport.handle_async_request(request)

    service.transport = httpx2.MockTransport(slow)
    with pytest.raises(module.MCPAdmissionError):
        await call(service, snapshot, work_duration=WorkDurationPolicyV2(timeout_seconds=1))
    assert dispatches == ["initialize", "tools/call"]


async def test_grant_revoked_during_uncapped_wait_cannot_return_the_late_result(tmp_path):
    module, service, _, grant, snapshot = await approved(tmp_path)
    release = asyncio.Event()
    calls, entered, closed, _ = delayed_tool(service, wait=release)
    task = asyncio.create_task(call(service, snapshot, work_duration=WorkDurationPolicyV2()))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        service.revoke("a" * 32, grant["id"])
        release.set()
        with pytest.raises(module.MCPAdmissionError, match="MCP exchange"):
            await task
        assert closed.is_set() and len(calls) == 1
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def test_mcp_work_policy_bytes_revoke_runtime_and_recovery_admission(monkeypatch):
    before = runtime_contract(), admission_contract_digest()
    original_read = Path.read_bytes
    monkeypatch.setattr(
        Path,
        "read_bytes",
        lambda path: (
            original_read(path) + b"\nchanged-mcp-work-policy\n"
            if path.name == "mcp_registry.py"
            else original_read(path)
        ),
    )
    assert runtime_contract() != before[0]
    assert admission_contract_digest() != before[1]
