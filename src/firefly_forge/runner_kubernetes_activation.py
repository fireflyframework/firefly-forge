# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""One native start intent activates one retained command Pod, never a replacement."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, ConfigDict, model_validator

from firefly_forge.runner_kubernetes import Guard, KubernetesIdentityChanged
from firefly_forge.runner_kubernetes_exec import KubernetesExecChannel
from firefly_forge.runner_kubernetes_models import (
    KubernetesBoundVolumeReceipt,
    KubernetesJobReceipt,
    KubernetesPodReceipt,
)
from firefly_forge.runner_pool_models import FactoryFence, KubernetesProjectInvocation, RunnerPoolProfile
from firefly_forge.runner_pool_owner import owner_reply, owner_request
from firefly_forge.runner_pools import RunnerPools
from firefly_forge.runner_run_liveness import RunLease, run_digest
from firefly_forge.runner_run_owner import RunOwnerChannel


class KubernetesCommandResources(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    job: KubernetesJobReceipt
    pod: KubernetesPodReceipt
    input: KubernetesBoundVolumeReceipt
    workspace: KubernetesBoundVolumeReceipt

    @model_validator(mode="after")
    def one_invocation(self) -> KubernetesCommandResources:
        if self.job.phase != "command" or self.pod.job_uid != self.job.uid:
            raise ValueError("Command activation requires its exact command Job and Pod")
        expected = (
            self.job.namespace,
            self.job.execution_id,
            self.job.invocation_digest,
            self.job.profile_revision,
        )
        for resource in (self.pod, self.input.claim, self.workspace.claim):
            if (
                resource.namespace,
                resource.execution_id,
                resource.invocation_digest,
                resource.profile_revision,
            ) != expected:
                raise ValueError("Command resources cannot cross invocation or namespace boundaries")
        if self.input.claim.purpose != "input" or self.workspace.claim.purpose != "workspace":
            raise ValueError("Input and workspace volume identities are separate")
        if self.input.claim.uid == self.workspace.claim.uid or self.input.volume_uid == self.workspace.volume_uid:
            raise ValueError("Command input and workspace cannot share a volume")
        return self


class KubernetesCommandActivation:
    def __init__(
        self,
        pools: RunnerPools,
        channel: KubernetesExecChannel,
        invocation: KubernetesProjectInvocation,
        claim: dict[str, Any],
        fence: FactoryFence,
        resources: KubernetesCommandResources,
        *,
        current: Guard,
        run_lease: RunLease | None = None,
    ) -> None:
        self.pools, self.channel, self.invocation = pools, channel, invocation
        self.claim, self.fence, self.resources = claim, fence, resources
        self.current, self.run_lease = current, run_lease
        self._attempted, self._started, self._closed = False, False, False
        self._key: bytes | None = None
        self._challenge: str | None = None
        self._sequence = 0
        self._run_channel: RunOwnerChannel | None = None
        self._lock = asyncio.Lock()

    @property
    def profile(self) -> RunnerPoolProfile:
        return self.channel.control_client.authority.profile

    async def guard(self) -> None:
        await self.current()
        await self.pools.authorize(
            self.profile, self.claim["id"], self.claim["owner_id"], self.claim["epoch"], self.fence
        )
        self.profile.validate_invocation(self.invocation)
        if self.claim["request"] != self.invocation.model_dump(mode="json"):
            raise KubernetesIdentityChanged("The native claim differs from its immutable invocation")

    async def resource_guard(self) -> None:
        await self.guard()
        control, r = self.channel.control_client, self.resources
        for volume in (r.input, r.workspace):
            if await control.bind_volume(self.invocation, volume.claim, self.guard, bound=volume) != volume:
                raise KubernetesIdentityChanged("A retained command volume binding changed")
        await control.observe_pod(self.invocation, r.job, r.pod, self.guard)
        await self.guard()

    async def _control(self, body: bytes) -> bytes:
        await self.resource_guard()
        r = self.resources
        response = await self.channel.control(self.invocation, r.job, r.pod, "owner", body, self.guard)
        await self.resource_guard()
        return response

    async def start(self) -> dict[str, Any]:
        async with self._lock:
            if self._attempted:
                raise RuntimeError("A command activation cannot be retried")
            self._attempted = True
            await self.resource_guard()
            current = await self.pools.observe(self.profile, self.invocation.execution_id)
            if (
                any(current.get(key) != self.claim[key] for key in ("id", "owner_id", "epoch", "request"))
                or current["status"] != "created"
            ):
                raise RuntimeError("Command activation requires its current created native claim")
            history = await self.pools.history(self.profile, self.invocation.execution_id)
            receipts = [row["payload"] for row in history if row["kind"] == "create_receipt"]
            expected = self.resources.model_dump(mode="json")
            if receipts != [expected]:
                raise KubernetesIdentityChanged("Command resources lack their exact native creation receipt")
            if (self.invocation.run_liveness is not None) != (self.run_lease is not None):
                raise ValueError("Run-owned command activation requires its current native run lease")
            if self.run_lease is not None and self.run_lease.invocation_digest != run_digest(
                self.invocation.model_dump(mode="json")
            ):
                raise ValueError("Run-owned activation cannot select another invocation lease")
            await self.resource_guard()
            await self.pools.effect_intent(
                self.profile,
                self.claim["id"],
                self.claim["owner_id"],
                self.claim["epoch"],
                self.fence,
                "start",
                payload=expected,
            )
            try:
                if self.run_lease is not None:

                    async def exchange(materialize: Callable[[], bytes]) -> bytes:
                        await self.resource_guard()
                        return await self._control(materialize())

                    self._run_channel = RunOwnerChannel(self.invocation.execution_id, self.run_lease, exchange)
                    status = await self._run_channel.start()
                else:
                    raw = await self._control(
                        json.dumps(
                            {
                                "protocol": "forge-runner-owner-v2",
                                "execution_id": self.invocation.execution_id,
                                "operation": "claim",
                            }
                        ).encode()
                    )
                    claimed = owner_reply(raw, self.invocation.execution_id, claim=True)
                    self._key, self._challenge = bytes.fromhex(claimed["key"]), claimed["challenge"]
                    status = await self._frame("start")
                await self.resource_guard()
                await self.pools.effect_receipt(
                    self.profile,
                    self.claim["id"],
                    self.claim["owner_id"],
                    self.claim["epoch"],
                    self.fence,
                    "start",
                    {**expected, "released": True},
                )
                self._started = True
                return status
            except BaseException:
                self._closed = True
                raise

    async def _frame(self, operation: str) -> dict[str, Any]:
        if self._closed or self._key is None or self._challenge is None:
            raise RuntimeError("The owned command requires reconciliation")
        self._sequence += 1
        try:
            raw = await self._control(
                owner_request(self._key, self.invocation.execution_id, operation, self._sequence, self._challenge)
            )
            reply = owner_reply(raw, self.invocation.execution_id, sequence=self._sequence)
            self._challenge = reply["challenge"]
            return reply
        except BaseException:
            self._closed = True
            raise

    async def status(self) -> dict[str, Any]:
        async with self._lock:
            if not self._started or self._closed:
                raise RuntimeError("The command has no confirmed native activation")
            try:
                await self.resource_guard()
                result = (
                    await self._run_channel.status() if self._run_channel is not None else await self._frame("renew")
                )
                await self.resource_guard()
                return result
            except BaseException:
                self._closed = True
                raise
