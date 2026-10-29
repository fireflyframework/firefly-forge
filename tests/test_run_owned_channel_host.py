# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""The real owner channel freshens a peer challenge before spending a new run grant."""

import json

import pytest
from test_run_owned_liveness_host import lease, renewal

from firefly_forge import runner_run_owner as module


async def test_owner_channel_polls_without_renewing_and_spends_each_worker_grant_once():
    assert hasattr(module, "RunOwnerChannel"), "Controller run-bound owner channel is missing"
    live, clock = lease()
    alarms, frames = [], []
    peer = module.RunOwnerLiveness(
        "a" * 32, live.invocation_digest, bytes(range(32)), 30, alarms.append, clock=lambda: clock["now"]
    )

    async def exchange(factory):
        frame = json.loads(factory())
        frames.append(frame["operation"])
        return json.dumps(peer.handle(frame)).encode()

    channel = module.RunOwnerChannel("a" * 32, live, exchange)
    assert (await channel.start())["state"] == "running"
    assert frames == ["claim", "start"] and alarms == [30]
    clock["now"] = 5
    assert (await channel.status())["state"] == "running"
    assert frames[-1] == "observe" and alarms == [30]
    live.renew(renewal(live))
    await channel.status()
    assert frames[-2:] == ["observe", "renew"] and alarms == [30, 30]
    await channel.status()
    assert frames[-1] == "observe" and alarms == [30, 30]


async def test_lost_control_ack_is_never_retried_or_spent_as_a_new_lease():
    assert hasattr(module, "RunOwnerChannel"), "Controller run-bound owner channel is missing"
    live, clock = lease()
    peer = module.RunOwnerLiveness(
        "a" * 32, live.invocation_digest, bytes(range(32)), 30, lambda seconds: None, clock=lambda: clock["now"]
    )
    frames = []

    async def exchange(factory):
        frame = json.loads(factory())
        frames.append(frame["operation"])
        result = peer.handle(frame)
        if frame["operation"] == "renew":
            raise OSError("Controlled acknowledgement lost")
        return json.dumps(result).encode()

    channel = module.RunOwnerChannel("a" * 32, live, exchange)
    await channel.start()
    live.renew(renewal(live))
    with pytest.raises(OSError):
        await channel.status()
    before = list(frames)
    with pytest.raises(ValueError):
        await channel.status()
    assert frames == before and frames.count("renew") == 1


async def test_expired_lease_cannot_claim_the_private_peer_key():
    live, clock = lease()
    frames = []
    clock["now"] = 30

    async def exchange(factory):
        frames.append(factory())
        raise OSError("Expired control must not dispatch")

    channel = module.RunOwnerChannel("a" * 32, live, exchange)
    with pytest.raises(ValueError):
        await channel.start()
    assert frames == []
