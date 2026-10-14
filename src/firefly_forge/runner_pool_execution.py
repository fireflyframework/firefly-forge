# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Fenced local command lifecycle; uncertain effects remain reserved for reconciliation."""

from __future__ import annotations

import asyncio
import json
import re
from contextlib import suppress
from typing import Any
from uuid import uuid4

from pyfly.container import service

from firefly_forge.runner_git_snapshot import RunnerGitSnapshot
from firefly_forge.runner_pool_artifacts import RunnerPoolArtifacts
from firefly_forge.runner_pool_dispatch import DockerPoolDispatcher
from firefly_forge.runner_pool_models import (
    FactoryFence,
    RunnerInvocation,
    RunnerPoolProfile,
    RunnerProjectInvocation,
    parse_invocation,
)
from firefly_forge.runner_pools import RunnerPools


@service
class RunnerPoolExecutions:
    def __init__(
        self,
        pools: RunnerPools,
        artifacts: RunnerPoolArtifacts,
        dispatcher: DockerPoolDispatcher,
        git_snapshot: RunnerGitSnapshot | None = None,
    ) -> None:
        self.pools, self.artifacts, self.dispatcher = pools, artifacts, dispatcher
        self.git_snapshot = git_snapshot
        self.owner = uuid4().hex
        self.tasks: dict[str, asyncio.Task[None]] = {}
        self.fence: FactoryFence | None = None
        self.admission_lock = asyncio.Lock()
        self.stopping = False

    @property
    def profile(self) -> RunnerPoolProfile:
        return self.dispatcher.authority.profile

    async def accept(
        self, invocation: RunnerInvocation, fence: FactoryFence, archive: bytes, stdin: bytes
    ) -> dict[str, Any]:
        if self.stopping:
            raise RuntimeError("Runner controller is stopping")
        await self.pools.check_factory_fence(fence)
        await self.dispatcher.verify_daemon()
        self.artifacts.prepare(self.profile, invocation, archive)
        if isinstance(invocation, RunnerProjectInvocation):
            if self.git_snapshot is None:
                raise RuntimeError("Project bundle controller capability is inactive")
            await self.artifacts.minimum_git(self.profile, invocation, self.git_snapshot)
        self.artifacts.retain(self.profile, invocation, "stdin", stdin)
        result = await self.pools.submit(self.profile, invocation, fence=fence)
        self.fence = fence
        await self.pump()
        return result

    async def pump(self) -> None:
        async with self.admission_lock:
            if self.fence is None or self.stopping:
                return
            self.tasks = {key: task for key, task in self.tasks.items() if not task.done()}
            while len(self.tasks) < self.profile.slots:
                claim = await self.pools.claim(self.profile, self.owner, self.fence)
                if claim is None:
                    break
                self.tasks[claim["request"]["execution_id"]] = asyncio.create_task(
                    self.execute_claim(claim, self.fence)
                )

    async def stop(self) -> None:
        self.stopping = True
        self.fence = None
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.tasks.clear()

    async def _renew(self, claim: dict[str, Any]) -> None:
        while True:
            await asyncio.sleep(self.profile.lease_seconds / 3)
            if not await self.pools.renew(self.profile, claim["id"], self.owner, claim["epoch"]):
                raise RuntimeError("Runner lease or factory owner was lost")

    async def execute_claim(self, claim: dict[str, Any], fence: FactoryFence) -> None:
        invocation = parse_invocation(claim["request"])
        current = await self.pools.observe(self.profile, invocation.execution_id)
        if current["status"] != "leased" or current["owner_id"] != self.owner or current["epoch"] != claim["epoch"]:
            return
        if current["request"] != invocation.model_dump(mode="json"):
            raise ValueError("Invocation claim changed its immutable request")
        heartbeat = asyncio.create_task(self._renew(claim))
        work = asyncio.create_task(self._execute(invocation, claim, fence))
        try:
            completed, _ = await asyncio.wait((heartbeat, work), return_when=asyncio.FIRST_COMPLETED)
            if heartbeat in completed:
                await heartbeat
            await work
        except BaseException:
            work.cancel()
            await asyncio.gather(work, return_exceptions=True)
            with suppress(Exception):
                await self.pools.quarantine(
                    self.profile, claim["id"], self.owner, claim["epoch"], {"reason": "execution_or_transport_unknown"}
                )
            # No unverified resource removal or capacity release on this path.
            # Independent PID1 timers bound code/grace; a live owner reconciles exact resources.
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
        self.tasks.pop(invocation.execution_id, None)
        if self.fence is not None:
            with suppress(Exception):
                await self.pump()

    async def _execute(self, invocation: RunnerInvocation, claim: dict[str, Any], fence: FactoryFence) -> None:
        async def guard() -> None:
            await self.pools.authorize(self.profile, claim["id"], self.owner, claim["epoch"], fence)

        async def control(
            argv: list[str], *, timeout: int = 30, limit: int = 500_000, input_data: bytes | None = None
        ) -> bytes:
            code, output = await self.dispatcher.fenced_control(
                argv, guard, timeout=timeout, limit=limit, input_data=input_data
            )
            if code:
                raise RuntimeError("Owned runner operation failed or requires reconciliation")
            return output

        carrier = self.artifacts.input_archive(self.profile, invocation)
        metadata = None
        if isinstance(invocation, RunnerProjectInvocation):
            if self.git_snapshot is None:
                raise RuntimeError("Project bundle controller capability is inactive")
            metadata = await self.artifacts.minimum_git(self.profile, invocation, self.git_snapshot)
        name = self.dispatcher.name(invocation)
        target = {
            "daemon_id": self.profile.daemon_id,
            "namespace": self.dispatcher.authority.namespace,
            "container_name": name,
            "transfer_name": name + "-transfer",
            "labels": self.dispatcher.labels(invocation),
        }
        await self.pools.effect_intent(
            self.profile, claim["id"], self.owner, claim["epoch"], fence, "create", payload=target
        )
        helper = (await control(self.dispatcher.transfer_plan(invocation))).decode().strip()
        if not re.fullmatch(r"[a-f0-9]{64}", helper):
            raise RuntimeError("Transfer create returned an unknown identity")
        observed = await self.dispatcher.inspect(invocation, helper)
        if observed is None or observed["Id"] != helper:
            raise RuntimeError("Transfer resource identity was not observed")
        await self.pools.record_effect(
            self.profile, claim["id"], self.owner, claim["epoch"], fence, "transfer_observed", {"transfer_id": helper}
        )
        await control(
            ["cp", "--archive", "-", helper + ":/workspace"],
            input_data=carrier,
        )
        if metadata is not None:
            await control(["cp", "--archive", "-", helper + ":/forge-git"], input_data=metadata)
        await control(["start", "--attach", helper])
        observed = await self.dispatcher.inspect(invocation, helper)
        if observed is None or observed["State"]["Status"] != "exited" or observed["State"]["ExitCode"] != 0:
            raise RuntimeError("Transfer initialization outcome is unknown")
        volumes = {mount["Destination"]: mount["Name"] for mount in observed["Mounts"]}
        if set(volumes) != {"/workspace", "/forge-git"}:
            raise RuntimeError("Transfer roots do not match the owned policy")
        await self.pools.record_effect(
            self.profile,
            claim["id"],
            self.owner,
            claim["epoch"],
            fence,
            "runner_create_intent",
            {"transfer_id": helper},
        )
        cid = (
            (await control(self.dispatcher.container_plan(invocation, volumes["/workspace"], volumes["/forge-git"])))
            .decode()
            .strip()
        )
        if not re.fullmatch(r"[a-f0-9]{64}", cid):
            raise RuntimeError("Runner create returned an unknown identity")
        observed = await self.dispatcher.inspect(invocation, cid)
        if observed is None or observed["Id"] != cid:
            raise RuntimeError("Created runner identity was not observed")
        await self.pools.effect_receipt(
            self.profile,
            claim["id"],
            self.owner,
            claim["epoch"],
            fence,
            "create",
            {"container_id": cid, "transfer_id": helper, "volumes": volumes},
        )
        await self.pools.effect_intent(self.profile, claim["id"], self.owner, claim["epoch"], fence, "start")
        await control(["start", cid])
        async with asyncio.timeout(15):
            while True:
                code, _ = await self.dispatcher.fenced_control(
                    [
                        "exec",
                        cid,
                        "/app/.venv/bin/python",
                        "-I",
                        "-S",
                        "-c",
                        "import os,stat;assert stat.S_ISFIFO(os.lstat('/tmp/forge-start').st_mode)",
                    ],
                    guard,
                    timeout=5,
                    limit=4096,
                )
                if code == 0:
                    break
                await asyncio.sleep(0.1)
        await control(self.dispatcher.release_plan(invocation, cid), timeout=5, limit=4096)
        await self.pools.effect_receipt(
            self.profile,
            claim["id"],
            self.owner,
            claim["epoch"],
            fence,
            "start",
            {"container_id": cid, "released": True},
        )
        async with asyncio.timeout(invocation.timeout + 5):
            while True:
                code, output = await self.dispatcher.fenced_control(
                    self.dispatcher.result_plan(invocation, cid), guard, timeout=3, limit=4096
                )
                if code == 0:
                    status = json.loads(output)
                    if (
                        status.get("execution_id") != invocation.execution_id
                        or type(status.get("exit_code")) is not int
                    ):
                        raise RuntimeError("Runner exit receipt does not match its invocation")
                    break
                observed = await self.dispatcher.inspect(invocation, cid)
                if observed is None or not observed["State"]["Running"]:
                    raise RuntimeError("Runner exited before bounded artifact collection")
                await asyncio.sleep(0.1)
        logs = await control(["logs", cid], timeout=10, limit=invocation.max_output_bytes)
        returned = await control(
            self.dispatcher.collect_plan(invocation, cid), timeout=30, limit=self.profile.max_source_bytes + 1_048_576
        )
        output_receipt = self.artifacts.retain(self.profile, invocation, "output", logs)
        result_receipt = self.artifacts.retain(self.profile, invocation, "result", returned)
        await self.pools.record_effect(
            self.profile,
            claim["id"],
            self.owner,
            claim["epoch"],
            fence,
            "artifacts_retained",
            {"exit_code": status["exit_code"], "output": output_receipt, "result": result_receipt},
        )
        await self.pools.effect_intent(self.profile, claim["id"], self.owner, claim["epoch"], fence, "cleanup")
        for resource in (cid, helper):
            observed = await self.dispatcher.inspect(invocation, resource)
            if observed is not None:
                await control(["container", "rm", "--force", "--volumes", observed["Id"]], timeout=15, limit=8192)
            if await self.dispatcher.inspect(invocation, resource) is not None:
                raise RuntimeError("Runner resource drain is not established")
        await self.pools.finish(
            self.profile,
            claim["id"],
            self.owner,
            claim["epoch"],
            fence,
            {
                "drained": True,
                "outcome": "completed",
                "exit_code": status["exit_code"],
                "output": output_receipt,
                "result": result_receipt,
            },
        )

    async def reconcile(self, execution_id: str, fence: FactoryFence) -> dict[str, Any]:
        current = await self.pools.observe(self.profile, execution_id)
        if current["status"] != "quarantined":
            raise RuntimeError("Only quarantined invocations can be reconciled")
        invocation = parse_invocation(current["request"])
        history = await self.pools.history(self.profile, execution_id)

        async def guard() -> None:
            await self.pools.check_factory_fence(fence)
            if (await self.pools.observe(self.profile, execution_id))["status"] != "quarantined":
                raise RuntimeError("Reconciliation authority changed")

        await guard()
        await self.dispatcher.verify_daemon()
        name = self.dispatcher.name(invocation)
        target = {
            "daemon_id": self.profile.daemon_id,
            "namespace": self.dispatcher.authority.namespace,
            "container_name": name,
            "transfer_name": name + "-transfer",
            "labels": self.dispatcher.labels(invocation),
        }
        intents = [event for event in history if event["kind"] == "create_intent"]
        if intents and (len(intents) != 1 or intents[0]["payload"] != target):
            raise RuntimeError("Persisted runner target differs from controller authority")
        created: dict[str, Any] = next((event["payload"] for event in history if event["kind"] == "create_receipt"), {})
        transfer: dict[str, Any] = next(
            (event["payload"] for event in history if event["kind"] == "transfer_observed"), {}
        )
        prior_observed: dict[str, Any] = next(
            (event["payload"] for event in reversed(history) if event["kind"] == "reconciliation_observation"), {}
        )
        created = {**created, **prior_observed}
        runner_intent = any(event["kind"] == "runner_create_intent" for event in history)
        observations = []
        for key, resource_name in (("container_id", name), ("transfer_id", name + "-transfer")):
            receipt_id = created.get(key) or transfer.get(key)
            observed = await self.dispatcher.inspect(invocation, receipt_id or resource_name)
            if observed is not None:
                if receipt_id and receipt_id != observed["Id"]:
                    raise RuntimeError("Persisted resource identity differs from daemon observation")
                observations.append(observed)
            elif intents and not receipt_id and (key == "transfer_id" or runner_intent):
                raise RuntimeError("An unknown create has no observed identity; capacity remains quarantined")
        retained: dict[str, Any] = next(
            (event["payload"] for event in history if event["kind"] == "artifacts_retained"), {}
        )
        receipt: dict[str, Any] = {
            "drained": True,
            "outcome": "unknown",
            "observed_ids": [value["Id"] for value in observations],
        }
        if retained:
            for kind in ("output", "result"):
                self.artifacts.read(self.profile, invocation, kind, retained[kind])
            receipt.update(retained)
            receipt["outcome"] = "completed"
        if observations:
            identities = {
                "container_id" if value.get("Name", "").lstrip("/") == name else "transfer_id": value["Id"]
                for value in observations
            }
            # Use inspected names only after matching their persisted owned target.
            for value in observations:
                if value.get("Name", "").lstrip("/") not in {name, name + "-transfer"}:
                    raise RuntimeError("Observed resource name differs from its persisted target")
            await self.pools.reconciliation_observation(
                self.profile, execution_id, fence, {**prior_observed, **identities}
            )
        for observed in observations:
            code, _ = await self.dispatcher.fenced_control(
                ["container", "rm", "--force", "--volumes", observed["Id"]], guard, timeout=15, limit=8192
            )
            if code or await self.dispatcher.inspect(invocation, observed["Id"]) is not None:
                raise RuntimeError("Reconciliation did not observe exact resource drain")
        await guard()
        await self.pools.reconcile(self.profile, execution_id, fence, receipt)
        self.fence = fence
        await self.pump()
        return await self.pools.observe(self.profile, execution_id)

    async def cancel(self, execution_id: str, fence: FactoryFence) -> dict[str, Any]:
        await self.pools.check_factory_fence(fence)
        current = await self.pools.observe(self.profile, execution_id)
        if current["status"] in {"queued", "cancelled"}:
            await self.pools.cancel_waiting(self.profile, execution_id, fence)
        elif current["status"] != "finished":
            task = self.tasks.get(execution_id)
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            current = await self.pools.observe(self.profile, execution_id)
            if current["status"] != "quarantined":
                await self.pools.quarantine(
                    self.profile,
                    current["id"],
                    current["owner_id"],
                    current["epoch"],
                    {"reason": "factory_cancel_requested"},
                )
            return await self.reconcile(execution_id, fence)
        return await self.pools.observe(self.profile, execution_id)
