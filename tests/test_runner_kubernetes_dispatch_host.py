# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Controller-to-ledger flow against recorded cluster effects, never a live cluster."""

import copy
import hashlib
import importlib
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import SecretStr
from test_runner_kubernetes_exec_host import prepared
from test_runner_object_artifacts_host import fixture as object_fixture

from firefly_forge.runner_git_snapshot import RunnerGitSnapshot
from firefly_forge.runner_kubernetes_models import (
    KubernetesBoundVolumeReceipt,
    KubernetesJobReceipt,
    KubernetesPodReceipt,
    KubernetesVolumeReceipt,
)
from firefly_forge.runner_pool_artifacts import RunnerPoolArtifacts
from firefly_forge.runner_pool_bundle import frame_project_archives
from firefly_forge.runner_pool_models import FactoryFence, KubernetesProjectInvocation, pool_digest
from firefly_forge.runner_pool_owner import OwnerLiveness
from firefly_forge.sandbox import pack_workspace


async def flow(tmp_path, fault=None):
    module = importlib.import_module("firefly_forge.runner_kubernetes_dispatch")
    root = tmp_path / "controller"
    root.mkdir(mode=0o700)
    control, initial, _, _, _ = prepared(root)
    profile = control.authority.profile
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    (workspace / "main.py").write_text("print('owned fixture')\n")
    source = pack_workspace(workspace, profile.max_source_bytes)[0]
    builder = RunnerGitSnapshot(Path("/usr/bin/git"))
    metadata = await builder.build(source, root, profile.max_source_bytes)
    archive, bundle = frame_project_archives(source, metadata, profile.max_source_bytes)
    raw = initial.model_dump(mode="json")
    raw.update(
        project_id=uuid4().hex,
        archive_digest=bundle.archive_digest,
        source_bytes=len(archive),
        source_digest=bundle.source_digest,
        bundle=bundle.model_dump(mode="json"),
    )
    invocation = KubernetesProjectInvocation.model_validate(raw)
    object_root = tmp_path / "objects"
    object_root.mkdir(mode=0o700)
    objects, _, _, ledger = object_fixture(object_root)
    artifacts = RunnerPoolArtifacts(root, objects)
    order = []
    fence = FactoryFence(factory_schema="fixture_factory", pid=100, backend_started_at=datetime.now(UTC))

    class Pools:
        row = None
        events = []
        valid = True

        async def check_factory_fence(self, current):
            assert current == fence
            if not self.valid:
                raise RuntimeError("Current factory authority lost")

        async def submit(self, current, request, *, fence, **extra):
            assert current == profile and self.row is None and not extra
            await self.check_factory_fence(fence)
            self.row = {
                "id": uuid4().hex,
                "status": "queued",
                "owner_id": None,
                "epoch": 0,
                "request": request.model_dump(mode="json"),
                "receipt": None,
            }
            order.append("submitted")
            return copy.deepcopy(self.row)

        async def claim(self, current, owner, current_fence):
            await self.check_factory_fence(current_fence)
            if self.row is None or self.row["status"] != "queued":
                return None
            self.row.update(status="leased", owner_id=owner, epoch=1)
            return copy.deepcopy(self.row)

        async def observe(self, current, identity):
            assert current == profile and identity == invocation.execution_id
            if self.row is None:
                raise KeyError(identity)
            return copy.deepcopy(self.row)

        async def authorize(self, current, identity, owner, epoch, current_fence):
            await self.check_factory_fence(current_fence)
            assert current == profile and self.row["id"] == identity
            assert self.row["owner_id"] == owner and self.row["epoch"] == epoch
            assert self.row["status"] not in {"quarantined", "finished"}

        async def history(self, current, identity):
            return copy.deepcopy(self.events)

        async def renew(self, current, identity, owner, epoch):
            return self.valid

        async def record_effect(self, current, identity, owner, epoch, current_fence, kind, payload):
            await self.authorize(current, identity, owner, epoch, current_fence)
            self.events.append({"kind": kind, "payload": copy.deepcopy(payload)})
            order.append(kind + ":" + payload.get("stage", ""))

        async def effect_intent(self, current, identity, owner, epoch, current_fence, kind, *, payload=None):
            await self.authorize(current, identity, owner, epoch, current_fence)
            before, after = {
                "create": ("leased", "creating"),
                "start": ("created", "starting"),
                "cleanup": ("running", "draining"),
            }[kind]
            assert self.row["status"] == before
            self.row["status"] = after
            self.events.append({"kind": kind + "_intent", "payload": copy.deepcopy(payload or {})})
            order.append(kind + "_intent")

        async def effect_receipt(self, current, identity, owner, epoch, current_fence, kind, payload):
            await self.authorize(current, identity, owner, epoch, current_fence)
            before, after = {"create": ("creating", "created"), "start": ("starting", "running")}[kind]
            assert self.row["status"] == before
            self.row["status"] = after
            self.events.append({"kind": kind + "_receipt", "payload": copy.deepcopy(payload)})
            order.append(kind + "_receipt")

        async def finish(self, current, identity, owner, epoch, current_fence, receipt):
            await self.authorize(current, identity, owner, epoch, current_fence)
            assert self.row["status"] == "draining" and receipt["drained"] is True
            assert order.index("remove_job:command") < order.index("remove_volume:input")
            assert "remove_volume:workspace" in order
            self.row.update(status="finished", receipt=copy.deepcopy(receipt))
            order.append("finished")

        async def quarantine(self, current, identity, owner, epoch, diagnostic):
            self.row["status"] = "quarantined"
            order.append("quarantined")

        async def reconcile(self, current, identity, current_fence, receipt):
            await self.check_factory_fence(current_fence)
            assert self.row["status"] == "quarantined" and receipt["drained"] is True
            assert not pods and not leases
            self.row.update(status="finished", receipt=copy.deepcopy(receipt))
            order.append("reconciled")

    pools = Pools()
    leases = {}
    pods = {}
    transfer_finished = False
    command_owner = OwnerLiveness(invocation.execution_id, b"k" * 32, 30, lambda _: None)

    def common():
        return {
            "namespace": profile.kubernetes.namespace,
            "execution_id": invocation.execution_id,
            "invocation_digest": pool_digest(invocation.model_dump(mode="json")),
            "profile_revision": profile.revision,
        }

    async def create_volume(current, purpose, guard):
        await guard()
        order.append("create_volume:" + purpose)
        receipt = KubernetesVolumeReceipt(
            purpose=purpose, name="owned-" + purpose, uid=str(uuid4()), spec_digest="a" * 64, **common()
        )
        leases[purpose] = KubernetesBoundVolumeReceipt(
            claim=receipt,
            claim_spec_digest="b" * 64,
            volume_name="bound-" + purpose,
            volume_uid=str(uuid4()),
            volume_spec_digest="c" * 64,
        )
        return receipt

    async def bind_volume(current, receipt, guard, *, bound=None):
        await guard()
        value = leases[receipt.purpose]
        if bound is not None and value != bound:
            raise RuntimeError("Bound volume replaced")
        return value

    async def create_job(current, guard, phase):
        await guard()
        if phase == "command":
            assert transfer_finished and "remove_job:transfer" in order
        order.append("create_job:" + phase)
        receipt = KubernetesJobReceipt(
            name="owned-" + phase, uid=str(uuid4()), phase=phase, spec_digest="d" * 64, **common()
        )
        pods[phase] = KubernetesPodReceipt(
            name="pod-" + phase,
            uid=str(uuid4()),
            job_uid=receipt.uid,
            node_name=profile.kubernetes.node_names[0],
            spec_digest="e" * 64,
            **common(),
        )
        if fault == "lost_create" and phase == "command":
            raise RuntimeError("Create reply lost after mutation")
        return receipt

    async def transfer_job(current, guard):
        return await create_job(current, guard, "transfer")

    async def command_job(current, guard):
        return await create_job(current, guard, "command")

    async def bind_pod(current, receipt, guard):
        await guard()
        return pods[receipt.phase]

    async def observe_pod(current, parent, pod, guard):
        await guard()
        if pods.get(parent.phase) != pod:
            raise RuntimeError("Pod replaced")
        return {"metadata": {"uid": pod.uid}}

    async def terminated(current, parent, pod, guard):
        await observe_pod(current, parent, pod, guard)
        assert parent.phase == "transfer"
        if fault == "transfer_not_drained":
            raise TimeoutError("Transfer termination unconfirmed")
        assert transfer_finished
        return 0

    async def remove_job(current, parent, pod, guard):
        await observe_pod(current, parent, pod, guard)
        if parent.phase == "command":
            assert command_owner.state == "drained"
        if fault == "cleanup" and parent.phase == "command":
            raise RuntimeError("Cleanup unconfirmed")
        order.append("remove_job:" + parent.phase)
        pods.pop(parent.phase)

    async def remove_volume(current, volume, guard):
        await guard()
        assert "remove_job:command" in order
        order.append("remove_volume:" + volume.claim.purpose)
        leases.pop(volume.claim.purpose)

    async def collect_logs(current, job, pod, sink, guard):
        await observe_pod(current, job, pod, guard)
        assert command_owner.state == "drained"
        await sink(b"controlled output\n")

    control.create_volume = create_volume
    control.bind_volume = bind_volume
    control.create_transfer_job = transfer_job
    control.create_parked_job = command_job
    control.bind_pod = bind_pod
    control.observe_pod = observe_pod
    control.wait_terminated = terminated
    control.remove_drained_job = remove_job
    control.remove_volume = remove_volume
    control.collect_logs = collect_logs

    async def observe_job(current, receipt, guard):
        await guard()
        return {} if receipt.phase in pods else None

    async def observe_volume(current, receipt, guard, *, bound=None):
        await guard()
        return {} if receipt.purpose in leases else None

    async def confirm_job(current, receipt, pod, guard):
        await guard()
        assert receipt.phase not in pods

    async def confirm_volume(volume, guard):
        await guard()
        assert volume.claim.purpose not in leases

    control.observe_job = observe_job
    control.observe_volume = observe_volume
    control.confirm_removed_job = confirm_job
    control.confirm_removed_volume = confirm_volume

    def clear_fault():
        nonlocal fault
        fault = None

    class Channel:
        def __init__(self):
            self.control_client = control

        async def stage_inputs(self, current, parent, pod, data, guard):
            await observe_pod(current, parent, pod, guard)
            order.append("stage_inputs")
            assert parent.phase == "transfer" and data
            return {"kind": "prepared_input", "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}

        async def finish_transfer(self, current, parent, pod, guard):
            nonlocal transfer_finished
            await observe_pod(current, parent, pod, guard)
            transfer_finished = True
            order.append("transfer_stopped")

        async def control(self, current, parent, pod, operation, body, guard):
            import json

            await observe_pod(current, parent, pod, guard)
            if operation == "ready":
                return b""
            value = json.loads(body)
            order.append("owner:" + value["operation"])
            reply = command_owner.handle(value)
            if value["operation"] == "start":
                command_owner.finish(0)
            return json.dumps(reply).encode()

        async def collect_workspace(self, current, parent, pod, sink, guard):
            await observe_pod(current, parent, pod, guard)
            assert command_owner.state == "drained"
            if fault == "result":
                raise RuntimeError("Result stream interrupted")
            await sink(source)

    async def runtime(guard, current=None):
        await guard()

    verifier = SimpleNamespace(verify=runtime, qualification=lambda current=None: None)
    dispatcher = module.KubernetesPoolDispatcher(
        control.authority, control=control, channel=Channel(), runtime=verifier
    )

    async def authorized():
        await pools.check_factory_fence(fence)

    for kind, data in (("source", archive), ("stdin", b"")):
        await objects.upload(
            profile,
            invocation,
            kind,
            {"kind": kind, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()},
            stream(data),
            fence,
            authorize=authorized,
        )
    return SimpleNamespace(
        module=module,
        profile=profile,
        invocation=invocation,
        artifacts=artifacts,
        pools=pools,
        dispatcher=dispatcher,
        builder=builder,
        fence=fence,
        order=order,
        objects=objects,
        pods=pods,
        volumes=leases,
        clear_fault=clear_fault,
    )


async def stream(data):
    for offset in range(0, len(data), 65536):
        yield data[offset : offset + 65536]


async def run(f):
    from firefly_forge.runner_controller import PoolObjectSubmission, RunnerControllerService
    from firefly_forge.runner_pool_execution import RunnerPoolExecutions

    executor = RunnerPoolExecutions(f.pools, f.artifacts, f.dispatcher, f.builder)
    f.executor = executor
    settings = SimpleNamespace(
        authority=f.dispatcher.authority,
        token=SecretStr("controlled-token-at-least-thirty-two-characters"),
        active=True,
    )
    service = RunnerControllerService(settings, f.pools, f.artifacts, f.dispatcher, executor)
    service.ready = True
    references = f.dispatcher.input_references(f.objects, f.invocation)
    submitted = await service.submit_objects(
        PoolObjectSubmission(
            invocation=f.invocation,
            fence=f.fence,
            source=references[0],
            stdin=references[1],
        )
    )
    assert submitted["request"] == f.invocation.model_dump(mode="json")
    import asyncio

    await asyncio.gather(*list(executor.tasks.values()))
    assert executor.tasks == {}
    return f.pools.row


async def test_controller_native_claim_transfer_command_publication_drain_and_capacity_release(tmp_path):
    f = await flow(tmp_path)
    result = await run(f)
    assert result["status"] == "finished" and result["receipt"]["exit_code"] == 0
    assert f.order.index("create_intent") < f.order.index("create_volume:input")
    assert f.order.index("remove_job:transfer") < f.order.index("create_job:command")
    assert f.order.index("start_intent") < f.order.index("owner:claim") < f.order.index("owner:start")
    assert f.order.index("artifacts_retained:") < f.order.index("cleanup_intent") < f.order.index("finished")
    assert f.pods == {} and f.volumes == {}
    for kind in ("output", "result"):

        async def current():
            return None

        data = b"".join(
            [
                part
                async for part in f.objects.read(
                    f.profile, f.invocation, kind, result["receipt"][kind], authorize=current
                )
            ]
        )
        assert data


@pytest.mark.parametrize("fault", ["lost_create", "transfer_not_drained", "result", "cleanup"])
async def test_unconfirmed_cluster_or_publication_outcome_never_retries_or_releases_capacity(tmp_path, fault):
    f = await flow(tmp_path, fault)
    result = await run(f)
    assert result["status"] == "quarantined" and "finished" not in f.order
    assert f.order.count("create_job:command") <= 1 and f.order.count("owner:start") <= 1
    if fault == "transfer_not_drained":
        assert "create_job:command" not in f.order
    if fault in {"lost_create", "result"}:
        assert "cleanup_intent" not in f.order


async def test_explicit_readback_closes_known_publication_and_cleanup_without_second_activation(tmp_path):
    f = await flow(tmp_path, "cleanup")
    assert (await run(f))["status"] == "quarantined"
    f.clear_fault()
    restored = await f.executor.reconcile(f.invocation.execution_id, f.fence)
    assert restored["status"] == "finished" and restored["receipt"]["outcome"] == "completed"
    assert f.order.count("owner:claim") == f.order.count("owner:start") == 1
    assert f.order.count("create_job:command") == 1 and f.order.count("reconciled") == 1
    assert await f.executor.reconcile(f.invocation.execution_id, f.fence) == restored
    assert f.order.count("reconciled") == 1


async def test_unknown_create_cannot_be_recovered_by_adopting_a_name_or_recreating_job(tmp_path):
    f = await flow(tmp_path, "lost_create")
    assert (await run(f))["status"] == "quarantined"
    before = list(f.order)
    with pytest.raises(RuntimeError):
        await f.executor.reconcile(f.invocation.execution_id, f.fence)
    assert f.order == before and f.pools.row["status"] == "quarantined"


async def test_native_container_resolves_common_dispatcher_without_docker_constructor_or_union_di(tmp_path):
    from pyfly.container import Container

    from firefly_forge.runner_controller import RunnerControllerService, RunnerControllerSettings
    from firefly_forge.runner_dispatch import RunnerPoolDispatcher
    from firefly_forge.runner_pool_execution import RunnerPoolExecutions
    from firefly_forge.runner_pools import RunnerPools

    f = await flow(tmp_path)
    execution = RunnerPoolExecutions(f.pools, f.artifacts, f.dispatcher, f.builder)
    settings = SimpleNamespace(authority=f.dispatcher.authority)
    container = Container()
    for kind, instance in (
        (RunnerControllerSettings, settings),
        (RunnerPools, f.pools),
        (RunnerPoolArtifacts, f.artifacts),
        (RunnerPoolDispatcher, f.dispatcher),
        (RunnerPoolExecutions, execution),
    ):
        container.register_instance(kind, instance)
    container.register(RunnerControllerService)
    service = container.resolve(RunnerControllerService)
    assert service.dispatcher is f.dispatcher and service.executions is execution


@pytest.mark.parametrize("fault", [None, "foreign_response", "foreign_reference"])
async def test_worker_client_submits_exact_references_once_without_paths_or_byte_bundle(tmp_path, monkeypatch, fault):
    import json

    import httpx
    from test_runner_kubernetes_control_host import configured, request
    from test_runner_object_client_host import client_for

    from firefly_forge.runner_kubernetes_dispatch import KubernetesPoolDispatcher
    from firefly_forge.runner_object_artifacts import RunnerObjectArtifacts

    profile = configured()
    invocation = request(profile).model_copy(update={"project_id": uuid4()})
    references = KubernetesPoolDispatcher.input_references(RunnerObjectArtifacts, invocation)
    sent = []

    async def handle(incoming):
        sent.append(incoming)
        assert incoming.url.path == "/runner/v1/object-invocations"
        body = json.loads(incoming.content)
        assert set(body) == {"invocation", "fence", "source", "stdin"}
        assert body["source"] == references[0].model_dump(mode="json")
        assert body["stdin"] == references[1].model_dump(mode="json")
        observed = body["invocation"]
        if fault == "foreign_response":
            observed["execution_id"] = uuid4().hex
        return httpx.Response(200, json={"request": observed, "status": "queued"})

    client = client_for(profile, monkeypatch, handle)
    fence = FactoryFence(factory_schema="fixture_factory", pid=1, backend_started_at=datetime.now(UTC))
    if fault == "foreign_reference":
        references = (references[0].model_copy(update={"execution_id": uuid4().hex}), references[1])
    if fault is None:
        result = await client.submit_objects(invocation, fence, *references)
        assert result["request"] == invocation.model_dump(mode="json") and len(sent) == 1
    else:
        with pytest.raises((RuntimeError, ValueError)):
            await client.submit_objects(invocation, fence, *references)
        assert len(sent) == (0 if fault == "foreign_reference" else 1)
