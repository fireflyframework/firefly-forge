# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Native ledger dispatch through separate transfer, command, publication and drain phases."""

from __future__ import annotations

import asyncio
import hashlib
import json
import tempfile
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any

from firefly_forge.runner_dispatch import RunnerPoolDispatcher
from firefly_forge.runner_kubernetes import Guard, KubernetesAuthority, KubernetesControlClient, KubernetesEffectUnknown
from firefly_forge.runner_kubernetes_activation import KubernetesCommandActivation, KubernetesCommandResources
from firefly_forge.runner_kubernetes_exec import KubernetesExecChannel, StreamSink
from firefly_forge.runner_kubernetes_models import (
    KubernetesBoundVolumeReceipt,
    KubernetesJobReceipt,
    KubernetesPodReceipt,
    KubernetesVolumeReceipt,
)
from firefly_forge.runner_kubernetes_qualification import KubernetesRuntimeVerifier
from firefly_forge.runner_object_artifacts import RunnerObjectArtifacts, RunnerObjectReference
from firefly_forge.runner_pool_artifacts import RunnerPoolArtifacts
from firefly_forge.runner_pool_models import FactoryFence, KubernetesProjectInvocation
from firefly_forge.runner_pool_owner import owner_reply
from firefly_forge.runner_run_owner import run_owner_reply

if TYPE_CHECKING:
    from firefly_forge.runner_git_snapshot import RunnerGitSnapshot
    from firefly_forge.runner_pool_execution import RunnerPoolExecutions


class KubernetesPoolDispatcher(RunnerPoolDispatcher):
    authority: KubernetesAuthority

    def __init__(
        self,
        authority: KubernetesAuthority,
        *,
        control: KubernetesControlClient | None = None,
        channel: KubernetesExecChannel | None = None,
        runtime: KubernetesRuntimeVerifier | None = None,
    ) -> None:
        self.authority = authority
        self.control = control or KubernetesControlClient(authority)
        self.channel = channel or KubernetesExecChannel(self.control)
        self.runtime = runtime or KubernetesRuntimeVerifier(self.control, authority.qualification)

    async def verify_daemon(self) -> None:
        async def current() -> None:
            self.authority.verify()

        await self.runtime.verify(current)

    async def verify_images(self) -> None:
        await self.verify_daemon()

    @staticmethod
    def input_references(
        objects: RunnerObjectArtifacts, invocation: KubernetesProjectInvocation
    ) -> tuple[RunnerObjectReference, RunnerObjectReference]:
        return tuple(
            objects.reference(invocation, kind, {"kind": kind, "bytes": size, "sha256": digest})
            for kind, size, digest in (
                ("source", invocation.source_bytes, invocation.archive_digest),
                ("stdin", invocation.stdin_bytes, invocation.stdin_digest or hashlib.sha256(b"").hexdigest()),
            )
        )  # type: ignore[return-value]

    async def validate_inputs(
        self,
        artifacts: RunnerPoolArtifacts,
        invocation: KubernetesProjectInvocation,
        source: RunnerObjectReference,
        stdin: RunnerObjectReference,
        guard: Guard,
    ) -> None:
        self.authority.profile.validate_invocation(invocation)
        objects = self._objects(artifacts)
        if (source, stdin) != self.input_references(objects, invocation):
            raise ValueError("Kubernetes admission requires the exact already-published input references")
        await self.runtime.verify(guard, invocation)
        for reference in (source, stdin):
            await guard()
            identity = objects.reference_identity(self.authority.profile, invocation, reference)
            await objects._manifest(identity, await objects.ledger.load(identity))
            await guard()

    @staticmethod
    def _objects(artifacts: RunnerPoolArtifacts) -> RunnerObjectArtifacts:
        if artifacts.objects is None:
            raise ValueError("Kubernetes execution requires durable native object publication")
        artifacts.objects.credential_registry = artifacts.credential_registry
        return artifacts.objects

    async def _carrier(
        self,
        artifacts: RunnerPoolArtifacts,
        invocation: KubernetesProjectInvocation,
        builder: RunnerGitSnapshot | None,
        guard: Guard,
    ) -> bytes:
        if builder is None:
            raise ValueError("Kubernetes project input requires the qualified minimum Git builder")
        # Scratch is private infrastructure, not a shared checkout or the durable source of truth.
        self.authority.verify()
        with tempfile.TemporaryDirectory(prefix=".kube-input-", dir=self.authority.state_directory) as directory:
            temporary = RunnerPoolArtifacts(Path(directory), self._objects(artifacts))
            temporary.credential_registry = artifacts.credential_registry
            for reference in self.input_references(self._objects(artifacts), invocation):
                data = await artifacts.read_durable(
                    self.authority.profile, invocation, reference.kind, reference.receipt(), authorize=guard
                )
                temporary.retain(self.authority.profile, invocation, reference.kind, data)
            metadata = await temporary.minimum_git(self.authority.profile, invocation, builder)
            await guard()
            return temporary.input_archive(self.authority.profile, invocation, git_archive=metadata)

    async def _pod(
        self, invocation: KubernetesProjectInvocation, job: KubernetesJobReceipt, guard: Guard
    ) -> KubernetesPodReceipt:
        async with asyncio.timeout(self.authority.profile.admission_seconds):
            while True:
                pod = await self.control.bind_pod(invocation, job, guard)
                if pod is not None:
                    return pod
                await asyncio.sleep(0.1)

    async def _volume(
        self,
        invocation: KubernetesProjectInvocation,
        claim: KubernetesVolumeReceipt,
        guard: Guard,
        *,
        bound: KubernetesBoundVolumeReceipt | None = None,
    ) -> KubernetesBoundVolumeReceipt:
        async with asyncio.timeout(self.authority.profile.admission_seconds):
            while True:
                value = await self.control.bind_volume(invocation, claim, guard, bound=bound)
                if value is not None:
                    return value
                await asyncio.sleep(0.1)

    async def _publish(
        self,
        artifacts: RunnerPoolArtifacts,
        invocation: KubernetesProjectInvocation,
        fence: FactoryFence,
        kind: str,
        capture: Callable[[StreamSink], Awaitable[None]],
        guard: Guard,
    ) -> dict[str, Any]:
        objects = self._objects(artifacts)
        maximum = invocation.max_output_bytes if kind == "output" else self.authority.profile.max_source_bytes + 1048576
        self.authority.verify()
        with tempfile.TemporaryFile(mode="w+b", dir=self.authority.state_directory) as file:
            checksum, size = hashlib.sha256(), 0

            async def sink(part: bytes) -> None:
                nonlocal size
                await guard()
                self.authority.verify()
                if type(part) is not bytes or len(part) + size > maximum:
                    raise ValueError("Collected Kubernetes artifact exceeds its exact invocation budget")
                size += len(part)
                checksum.update(part)
                file.write(part)

            await capture(sink)
            await guard()
            file.flush()
            file.seek(0)
            receipt = {"kind": kind, "bytes": size, "sha256": checksum.hexdigest()}

            async def chunks() -> AsyncIterator[bytes]:
                while part := file.read(65536):
                    await guard()
                    yield part

            await objects.upload(self.authority.profile, invocation, kind, receipt, chunks(), fence, authorize=guard)
            await guard()
            return receipt

    async def execute(
        self,
        executor: RunnerPoolExecutions,
        invocation: KubernetesProjectInvocation,
        claim: dict[str, Any],
        fence: FactoryFence,
        current: Guard,
        begin_cleanup: Callable[[], None],
    ) -> None:
        profile, pools = self.authority.profile, executor.pools
        policy = profile.kubernetes
        assert policy is not None

        async def guard() -> None:
            await current()
            self.authority.verify()
            self.runtime.qualification(invocation)

        async def record(stage: str, **values: Any) -> None:
            await guard()
            await pools.record_effect(
                profile,
                claim["id"],
                executor.owner,
                claim["epoch"],
                fence,
                "transfer_observed",
                {"backend": "kubernetes", "stage": stage, **values},
            )

        await self.runtime.verify(guard, invocation)
        carrier = await self._carrier(executor.artifacts, invocation, executor.git_snapshot, guard)
        references = self.input_references(self._objects(executor.artifacts), invocation)
        await guard()
        await pools.effect_intent(
            profile,
            claim["id"],
            executor.owner,
            claim["epoch"],
            fence,
            "create",
            payload={
                "backend": "kubernetes",
                "namespace": policy.namespace,
                "references": [reference.model_dump(mode="json") for reference in references],
            },
        )
        input_claim = await self.control.create_volume(invocation, "input", guard)
        await record("input_claim_created", claim=input_claim.model_dump(mode="json"))
        workspace_claim = await self.control.create_volume(invocation, "workspace", guard)
        await record("workspace_claim_created", claim=workspace_claim.model_dump(mode="json"))
        transfer_job = await self.control.create_transfer_job(invocation, guard)
        await record("transfer_job_created", job=transfer_job.model_dump(mode="json"))
        transfer_pod = await self._pod(invocation, transfer_job, guard)
        await record("transfer_pod_bound", pod=transfer_pod.model_dump(mode="json"))
        input_volume = await self._volume(invocation, input_claim, guard)
        await record("input_volume_bound", volume=input_volume.model_dump(mode="json"))
        carrier_receipt = await self.channel.stage_inputs(invocation, transfer_job, transfer_pod, carrier, guard)
        del carrier
        await record("input_staged", receipt=carrier_receipt)
        await record("transfer_stop_intent", pod_uid=transfer_pod.uid)
        # One known stop may lose its reply; only exact termination readback can resolve it.
        with suppress(KubernetesEffectUnknown):
            await self.channel.finish_transfer(invocation, transfer_job, transfer_pod, guard)
        if await self.control.wait_terminated(invocation, transfer_job, transfer_pod, guard) != 0:
            raise KubernetesEffectUnknown("The exact transfer Pod did not complete its fixed phase")
        await self.control.remove_drained_job(invocation, transfer_job, transfer_pod, guard)
        await record(
            "transfer_drained", job=transfer_job.model_dump(mode="json"), pod=transfer_pod.model_dump(mode="json")
        )
        await self.runtime.verify(guard, invocation)
        await self._volume(invocation, input_claim, guard, bound=input_volume)
        await pools.record_effect(
            profile,
            claim["id"],
            executor.owner,
            claim["epoch"],
            fence,
            "runner_create_intent",
            {"backend": "kubernetes", "transfer_pod_uid": transfer_pod.uid},
        )
        command_job = await self.control.create_parked_job(invocation, guard)
        await record("command_job_created", job=command_job.model_dump(mode="json"))
        command_pod = await self._pod(invocation, command_job, guard)
        workspace_volume = await self._volume(invocation, workspace_claim, guard)
        resources = KubernetesCommandResources(
            job=command_job, pod=command_pod, input=input_volume, workspace=workspace_volume
        )
        await pools.effect_receipt(
            profile, claim["id"], executor.owner, claim["epoch"], fence, "create", resources.model_dump(mode="json")
        )
        activation = KubernetesCommandActivation(
            pools,
            self.channel,
            invocation,
            claim,
            fence,
            resources,
            current=guard,
            run_lease=executor.run_leases.get(invocation.execution_id),
        )
        await self.channel.control(invocation, command_job, command_pod, "ready", b"", activation.resource_guard)
        await self.runtime.verify(guard, invocation)
        await activation.start()
        async with asyncio.timeout(None if invocation.timeout is None else invocation.timeout + 5):
            while True:
                status = await activation.status()
                if status["state"] == "drained":
                    break
                await asyncio.sleep(0.1)
        await record("command_drained", pod_uid=command_pod.uid, status=status)

        async def logs(sink: StreamSink) -> None:
            await self.control.collect_logs(invocation, command_job, command_pod, sink, activation.resource_guard)

        async def result(sink: StreamSink) -> None:
            await self.channel.collect_workspace(invocation, command_job, command_pod, sink, activation.resource_guard)

        output = await self._publish(executor.artifacts, invocation, fence, "output", logs, activation.resource_guard)
        returned = await self._publish(
            executor.artifacts, invocation, fence, "result", result, activation.resource_guard
        )
        receipts = {
            "backend": "kubernetes",
            "exit_code": status["exit_code"],
            "output": output,
            "result": returned,
            "pod_uid": command_pod.uid,
            "PID1_drained": True,
        }
        await pools.record_effect(
            profile, claim["id"], executor.owner, claim["epoch"], fence, "artifacts_retained", receipts
        )
        begin_cleanup()
        await pools.effect_intent(profile, claim["id"], executor.owner, claim["epoch"], fence, "cleanup")
        await self.runtime.verify(guard, invocation)
        await self.control.remove_drained_job(invocation, command_job, command_pod, guard)
        for volume in (input_volume, workspace_volume):
            await self.control.remove_volume(invocation, volume, guard)
        await guard()
        await pools.finish(
            profile,
            claim["id"],
            executor.owner,
            claim["epoch"],
            fence,
            {**receipts, "drained": True, "outcome": "completed"},
        )

    async def reconcile(self, executor: RunnerPoolExecutions, execution_id: str, fence: FactoryFence) -> dict[str, Any]:
        pools, profile = executor.pools, self.authority.profile
        await pools.check_factory_fence(fence)
        before = await pools.observe(profile, execution_id)
        if before["status"] == "finished":
            return before
        if before["status"] != "quarantined":
            raise RuntimeError("Only quarantined Kubernetes invocations can be reconciled")
        invocation = KubernetesProjectInvocation.model_validate(before["request"])

        async def guard() -> None:
            await pools.check_factory_fence(fence)
            current = await pools.observe(profile, execution_id)
            if current["status"] != "quarantined" or any(
                current.get(k) != before.get(k) for k in ("id", "request", "epoch")
            ):
                raise RuntimeError("The retained Kubernetes reconciliation identity changed")
            self.authority.verify()
            self.runtime.qualification(invocation)

        await self.runtime.verify(guard, invocation)
        history = await pools.history(profile, execution_id)
        created = [event["payload"] for event in history if event["kind"] == "create_receipt"]
        if len(created) != 1:
            raise RuntimeError("An unknown create has no exact native UID receipt; do not adopt or recreate it")
        resources = KubernetesCommandResources.model_validate(created[0])
        drains = [
            event["payload"]
            for event in history
            if event["kind"] == "transfer_observed" and event["payload"].get("stage") == "command_drained"
        ]
        retained = [event["payload"] for event in history if event["kind"] == "artifacts_retained"]
        if len(drains) > 1 or len(retained) > 1:
            raise ValueError("The native command has ambiguous drain or publication custody")
        if drains:
            drain = drains[0]
            status = drain["status"]
            if drain.get("pod_uid") != resources.pod.uid or status.get("state") != "drained":
                raise ValueError("The retained command drain belongs to another Pod or state")
            raw = json.dumps(status).encode()
            if invocation.run_liveness is None:
                owner_reply(raw, execution_id, sequence=status["sequence"])
            else:
                from firefly_forge.runner_run_liveness import run_digest

                run_owner_reply(
                    raw,
                    execution_id,
                    run_digest(invocation.model_dump(mode="json")),
                    sequence=status["sequence"],
                    grant_sequence=status["grant_sequence"],
                )
        else:
            # Exact termination proves this PID namespace is gone; resource absence alone cannot.
            code = await self.control.wait_terminated(invocation, resources.job, resources.pod, guard)
            status = {"exit_code": code}
        receipt: dict[str, Any] = {
            "backend": "kubernetes",
            "drained": True,
            "outcome": "unknown",
            "pod_uid": resources.pod.uid,
        }
        if retained:
            value = retained[0]
            if (
                not drains
                or value.get("PID1_drained") is not True
                or value.get("pod_uid") != resources.pod.uid
                or value.get("exit_code") != status["exit_code"]
            ):
                raise ValueError("Published artifacts lack the exact native command-drain binding")
            objects = self._objects(executor.artifacts)
            for kind in ("output", "result"):
                async for _ in objects.read(profile, invocation, kind, value[kind], authorize=guard):
                    pass
            receipt.update(value, outcome="completed")
        observed = await self.control.observe_job(invocation, resources.job, guard)
        if observed is None:
            await self.control.confirm_removed_job(invocation, resources.job, resources.pod, guard)
        else:
            await self.control.remove_drained_job(invocation, resources.job, resources.pod, guard)
        for volume in (resources.input, resources.workspace):
            if await self.control.observe_volume(invocation, volume.claim, guard, bound=volume) is None:
                await self.control.confirm_removed_volume(volume, guard)
            else:
                await self.control.remove_volume(invocation, volume, guard)
        await guard()
        await pools.reconcile(profile, execution_id, fence, receipt)
        return await pools.observe(profile, execution_id)
