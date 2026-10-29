# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Owner frames are materialized once after the last current-authority fence."""

import json
from types import SimpleNamespace

import pytest
from test_run_owned_controller_host import initialized

from firefly_forge.runner_pool_dispatch import DockerPoolDispatcher
from firefly_forge.runner_run_owner import RunOwnerLiveness


async def test_fenced_dispatch_materializes_the_private_frame_after_both_guards():
    events = []
    dispatcher = object.__new__(DockerPoolDispatcher)

    async def guard():
        events.append("guard")

    async def daemon():
        events.append("daemon")

    def frame():
        events.append("frame")
        return b"private-frame"

    async def transport(argv, timeout, limit, payload):
        events.append("transport")
        assert payload == b"private-frame"
        return 0, b"receipt"

    dispatcher.verify_daemon, dispatcher.transport = daemon, transport
    assert await dispatcher.fenced_control(["fixture"], guard, input_factory=frame) == (0, b"receipt")
    assert events == ["guard", "daemon", "guard", "frame", "transport"]


async def test_real_controller_owner_channel_refuses_frames_after_its_run_is_revoked(monkeypatch):
    executor, invocation, fence, clock, permission, *_ = await initialized(monkeypatch)
    await executor.accept(invocation, fence, b"fixture", b"")
    assert hasattr(executor, "_run_owner_channel"), "Controller PID1 run channel is missing"
    live = executor.run_leases[invocation.execution_id]
    peer = RunOwnerLiveness(
        invocation.execution_id,
        live.invocation_digest,
        bytes(range(32)),
        30,
        lambda seconds: None,
        clock=lambda: clock["now"],
    )
    frames = []
    dispatcher = object.__new__(DockerPoolDispatcher)
    dispatcher.authority = SimpleNamespace(profile=executor.profile)

    async def daemon():
        pass

    async def transport(argv, timeout, limit, payload):
        frame = json.loads(payload)
        frames.append(frame["operation"])
        return 0, json.dumps(peer.handle(frame)).encode()

    async def guard():
        await executor._assert_run_current(invocation, fence)

    dispatcher.verify_daemon, dispatcher.transport = daemon, transport
    executor.dispatcher = dispatcher
    channel = executor._run_owner_channel(invocation, "a" * 64, guard)
    assert (await channel.start())["state"] == "running"
    assert frames == ["claim", "start"]
    permission["active"] = False
    with pytest.raises(ValueError):
        await channel.status()
    assert frames == ["claim", "start"]
