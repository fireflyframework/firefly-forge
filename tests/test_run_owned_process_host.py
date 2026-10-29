# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Controlled POSIX group proof; this does not substitute for Linux PID1/Docker proof."""

import asyncio
import json
import os
import signal
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest
from pydantic import SecretStr
from test_run_owned_controller_host import initialized

from firefly_forge import runner_controller as server
from firefly_forge.runner_controller_client import RunnerControllerClient
from firefly_forge.runner_pool_dispatch import DockerPoolDispatcher

PROJECT = r"""
import json,os,signal,subprocess,sys,time
child_code='import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(60)'
child=subprocess.Popen([sys.executable,'-I','-B','-c',child_code])
def stop(*args):
 child.kill();child.wait(timeout=2);sys.exit(0)
signal.signal(signal.SIGTERM,stop)
print(json.dumps({'pid':os.getpid(),'pgid':os.getpgrp(),'child':child.pid}),flush=True)
while True:time.sleep(1)
"""

RUNNER = r"""
import json,os,secrets,signal,subprocess,sys,time
sys.path.insert(0,sys.argv[1])
from firefly_forge.runner_run_owner import RunOwnerLiveness
command=None
def emit(value):print(json.dumps(value),flush=True)
def stop(signum,frame):
 global command
 drained=True
 if command is not None:
  if command.poll() is None:
   os.killpg(command.pid,signal.SIGTERM)
   try:command.wait(timeout=1)
   except subprocess.TimeoutExpired:
    os.killpg(command.pid,signal.SIGKILL);command.wait(timeout=2)
  try:os.killpg(command.pid,0);drained=False
  except ProcessLookupError:pass
 emit({'local_test_terminal':True,'group_drained':drained,'owner_expired':signum==signal.SIGALRM})
 os._exit(0)
signal.signal(signal.SIGALRM,stop);signal.signal(signal.SIGTERM,stop)
owner=RunOwnerLiveness(sys.argv[2],sys.argv[3],secrets.token_bytes(32),int(sys.argv[4]),signal.alarm)
for raw in sys.stdin.buffer:
 try:
  if len(raw)>4096:raise ValueError()
  reply=owner.handle(json.loads(raw))
  if reply['state']=='running' and command is None:
   command=subprocess.Popen([sys.executable,'-I','-B','-c',sys.argv[5]],stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,start_new_session=True)
   emit({'local_test_started':json.loads(command.stdout.readline())})
  emit(reply)
 except (ValueError,TypeError,KeyError):emit({'local_test_refused':True})
stop(signal.SIGTERM,None)
"""


class LocalPeer:
    def __init__(self, process):
        self.process, self.acks = process, asyncio.Queue()
        self.started, self.terminal = {}, {}
        self.ended = asyncio.Event()
        self.reader = asyncio.create_task(self.read())

    @classmethod
    async def create(cls, invocation, lease, grace):
        source = str(Path(__file__).resolve().parents[1] / "src")
        return cls(
            await asyncio.create_subprocess_exec(
                sys.executable,
                "-I",
                "-B",
                "-c",
                RUNNER,
                source,
                invocation.execution_id,
                lease.invocation_digest,
                str(grace),
                PROJECT,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        )

    async def read(self):
        while raw := await self.process.stdout.readline():
            value = json.loads(raw)
            if "local_test_started" in value:
                self.started = value["local_test_started"]
            elif "local_test_terminal" in value:
                self.terminal = value
                self.ended.set()
            else:
                await self.acks.put(value)

    async def exchange(self, payload):
        self.process.stdin.write(payload + b"\n")
        await self.process.stdin.drain()
        return json.dumps(await asyncio.wait_for(self.acks.get(), 2)).encode()

    async def close(self):
        if self.process.returncode is None:
            self.process.send_signal(signal.SIGTERM)
        await asyncio.wait_for(self.process.wait(), 5)
        await self.reader
        assert self.terminal is not None and self.terminal["group_drained"] is True


@pytest.mark.parametrize("reason", ["revoked", "worker_lost"])
async def test_real_process_group_stops_per_run_while_sibling_and_factory_keep_renewing(monkeypatch, reason):
    started_at = time.monotonic()
    executor, first, fence, _, permissions, records, _, cancellations, *_ = await initialized(
        monkeypatch, lease_seconds=2
    )
    executor.run_clock = time.monotonic
    second = first.model_copy(
        update={
            "execution_id": "2" * 32,
            "run_id": UUID("3" * 32),
            "run_liveness": first.run_liveness.model_copy(update={"worker_epoch": "4" * 32}),
        }
    )
    a = (await executor.accept(first, fence, b"fixture", b""))["run_lease"]
    b = (await executor.accept(second, fence, b"fixture", b""))["run_lease"]
    peers = {}
    healthy = None
    try:
        peers["a" * 64] = await LocalPeer.create(first, executor.run_leases[first.execution_id], 2)
        peers["b" * 64] = await LocalPeer.create(second, executor.run_leases[second.execution_id], 2)
        dispatcher = object.__new__(DockerPoolDispatcher)
        dispatcher.authority = SimpleNamespace(profile=executor.profile)
        heartbeats = []

        async def daemon():
            heartbeats.append(1)

        async def transport(argv, timeout, limit, payload):
            return 0, await peers[argv[2]].exchange(payload)

        dispatcher.verify_daemon, dispatcher.transport = daemon, transport
        executor.dispatcher = dispatcher

        async def first_guard():
            await executor._assert_run_current(first, fence)

        async def second_guard():
            await executor._assert_run_current(second, fence)

        channel_a = executor._run_owner_channel(first, "a" * 64, first_guard)
        channel_b = executor._run_owner_channel(second, "b" * 64, second_guard)
        token = SecretStr("local-physical-proof-private-controller-credential")
        service = object.__new__(server.RunnerControllerService)
        service.settings = SimpleNamespace(
            active=True, authority=SimpleNamespace(profile=executor.profile), token=token
        )
        service.ready, service.pools, service.executions = True, executor.pools, executor
        client = RunnerControllerClient(
            SimpleNamespace(profile_id=first.profile_id, profile_revision=first.profile_revision, token=token)
        )

        async def request(path, payload):
            assert path == "renew-run"
            return await service.renew_run(server.PoolRunRenewal.model_validate(payload))

        monkeypatch.setattr(client, "_request", request)
        await channel_a.start()
        await channel_b.start()
        assert peers["a" * 64].started["pgid"] != peers["b" * 64].started["pgid"]

        async def keep_sibling():
            nonlocal b
            while True:
                await asyncio.sleep(0.3)
                b = await client.renew_run(second, fence, b)
                await channel_b.status()

        healthy = asyncio.create_task(keep_sibling())
        if reason == "revoked":
            permissions["revoked"].add(first.execution_id)
            with pytest.raises(ValueError):
                await client.renew_run(first, fence, a)
            assert cancellations == [(first.execution_id, fence)]
        else:
            # A healthy controller keeps observing, but there are no fresh worker grants.
            while not peers["a" * 64].ended.is_set():
                await asyncio.sleep(0.2)
                try:
                    await channel_a.status()
                except ValueError:
                    break
        await asyncio.wait_for(peers["a" * 64].ended.wait(), 6)
        assert peers["a" * 64].terminal == {"local_test_terminal": True, "group_drained": True, "owner_expired": True}
        assert not healthy.done() and peers["b" * 64].process.returncode is None
        os.killpg(peers["b" * 64].started["pgid"], 0)
        assert len(heartbeats) > 4 and b["sequence"] >= 2
        assert records[first.execution_id]["request"] == first.model_dump(mode="json")
        if directory := os.environ.get("FORGE_RUN_LIVENESS_HOST_EVIDENCE"):
            proof = {
                "status": "CONTROLLED_POSIX_GROUP_PROOF_NOT_LINUX_PID1_DOCKER_OR_NATIVE_QUALIFICATION",
                "scenario": reason,
                "elapsed_seconds": time.monotonic() - started_at,
                "explicit_owner_grace_seconds": 2,
                "target_group": peers["a" * 64].started,
                "target_terminal": peers["a" * 64].terminal,
                "sibling_group": peers["b" * 64].started,
                "sibling_observed_alive": True,
                "sibling_worker_grants": b["sequence"],
                "factory_control_checks": len(heartbeats),
                "database_authority": "controlled oracle; no PostgreSQL or retained services",
                "new_uncapped_availability_enabled": False,
            }
    finally:
        if healthy is not None:
            healthy.cancel()
            await asyncio.gather(healthy, return_exceptions=True)
        for peer in peers.values():
            await peer.close()

    if directory := os.environ.get("FORGE_RUN_LIVENESS_HOST_EVIDENCE"):
        proof["cleanup"] = {key: peer.terminal for key, peer in peers.items()}
        assert all(value["group_drained"] for value in proof["cleanup"].values())
        with (Path(directory) / (reason + ".json")).open("x") as stream:
            stream.write(json.dumps(proof, indent=2, sort_keys=True) + "\n")
