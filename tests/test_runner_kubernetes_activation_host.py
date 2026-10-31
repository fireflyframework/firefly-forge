# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Native ledger transitions precede one owned Pod activation; no cluster is used."""

import importlib
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from test_runner_kubernetes_exec_host import prepared

from firefly_forge.runner_kubernetes_models import KubernetesBoundVolumeReceipt, KubernetesVolumeReceipt
from firefly_forge.runner_pool_models import FactoryFence
from firefly_forge.runner_pool_owner import OwnerLiveness


def setup(tmp_path):
    control, invocation, job, pod, observations = prepared(tmp_path)
    profile = control.authority.profile
    identity = dict(
        execution_id=invocation.execution_id,
        invocation_digest=job.invocation_digest,
        profile_revision=profile.revision,
        namespace=job.namespace,
    )
    volumes = tuple(
        KubernetesBoundVolumeReceipt(
            claim=KubernetesVolumeReceipt(
                purpose=kind, name="owned-" + kind, uid=str(uuid4()), spec_digest="b" * 64, **identity
            ),
            claim_spec_digest="c" * 64,
            volume_name="bound-" + kind,
            volume_uid=str(uuid4()),
            volume_spec_digest="d" * 64,
        )
        for kind in ("input", "workspace")
    )
    order = []

    async def bind(current, receipt, guard, *, bound):
        await guard()
        assert current == invocation and receipt == bound.claim
        order.append("volume:" + receipt.purpose)
        return bound

    control.bind_volume = bind
    owner = OwnerLiveness(invocation.execution_id, b"k" * 32, 30, lambda _: None)

    async def exchange(current, parent, activated, operation, body, guard):
        assert current == invocation and parent == job and activated == pod and operation == "owner"
        await guard()
        message = json.loads(body)
        order.append(message["operation"])
        return json.dumps(owner.handle(message)).encode()

    channel = SimpleNamespace(control_client=control, control=exchange)
    fence = FactoryFence(factory_schema="fixture_factory", pid=1, backend_started_at=datetime.now(UTC))
    claim = dict(id=uuid4().hex, owner_id="controller-owner", epoch=2, request=invocation.model_dump(mode="json"))

    class Pools:
        status = "created"
        revoked = False

        async def authorize(self, *args):
            assert args == (profile, claim["id"], claim["owner_id"], claim["epoch"], fence)
            if self.revoked:
                raise RuntimeError("Lease changed")

        async def observe(self, current, execution_id):
            assert current == profile and execution_id == invocation.execution_id
            return {**claim, "status": self.status}

        async def history(self, current, execution_id):
            return [{"kind": "create_receipt", "payload": self.receipt}]

        async def effect_intent(self, *args, payload):
            assert args == (profile, claim["id"], claim["owner_id"], claim["epoch"], fence, "start")
            assert self.status == "created"
            self.status = "starting"
            order.append("start_intent")
            assert payload["pod"]["uid"] == pod.uid

        async def effect_receipt(self, *args):
            assert args[:-1] == (profile, claim["id"], claim["owner_id"], claim["epoch"], fence, "start")
            assert args[-1]["pod"]["uid"] == pod.uid
            self.status = "running"
            order.append("start_receipt")

    pools = Pools()
    return SimpleNamespace(
        control=control,
        invocation=invocation,
        job=job,
        pod=pod,
        volumes=volumes,
        order=order,
        channel=channel,
        fence=fence,
        claim=claim,
        pools=pools,
        owner=owner,
        observations=observations,
    )


def activation(fixture):
    module = importlib.import_module("firefly_forge.runner_kubernetes_activation")
    resources = module.KubernetesCommandResources(
        job=fixture.job, pod=fixture.pod, input=fixture.volumes[0], workspace=fixture.volumes[1]
    )
    fixture.pools.receipt = resources.model_dump(mode="json")

    async def current():
        return None

    return module.KubernetesCommandActivation(
        fixture.pools,
        fixture.channel,
        fixture.invocation,
        fixture.claim,
        fixture.fence,
        resources,
        current=current,
    )


async def test_native_start_intent_precedes_exact_one_owner_claim_and_start(tmp_path):
    f = setup(tmp_path)
    operation = activation(f)
    await operation.start()
    assert [v for v in f.order if not v.startswith("volume:")] == ["start_intent", "claim", "start", "start_receipt"]
    assert f.owner.state == "running" and f.pools.status == "running"
    with pytest.raises(RuntimeError):
        await operation.start()
    assert f.order.count("claim") == f.order.count("start") == 1


async def test_durable_start_marker_prevents_new_instance_from_reclaiming_same_pod(tmp_path):
    f = setup(tmp_path)
    await activation(f).start()
    with pytest.raises(RuntimeError):
        await activation(f).start()
    assert f.order.count("claim") == f.order.count("start") == 1


@pytest.mark.parametrize("fault", ["lease", "create_receipt", "volume", "pod"])
async def test_current_authority_and_all_resource_fences_hold_before_activation(tmp_path, fault):
    f = setup(tmp_path)
    operation = activation(f)
    if fault == "lease":
        f.pools.revoked = True
    elif fault == "create_receipt":
        f.pools.receipt = {**f.pools.receipt, "pod": {**f.pools.receipt["pod"], "uid": str(uuid4())}}
    elif fault == "volume":

        async def changed(*args, **kwargs):
            return None

        f.control.bind_volume = changed
    else:

        async def changed(*args, **kwargs):
            raise RuntimeError("Pod identity changed")

        f.control.observe_pod = changed
    with pytest.raises((RuntimeError, ValueError)):
        await operation.start()
    assert "claim" not in f.order and "start" not in f.order and f.pools.status == "created"


async def test_lost_claim_reply_preserves_starting_state_and_never_replays(tmp_path):
    f = setup(tmp_path)
    original = f.channel.control

    async def lost(*args):
        await original(*args)
        raise OSError("Reply lost after PID1 accepted")

    f.channel.control = lost
    operation = activation(f)
    with pytest.raises(OSError):
        await operation.start()
    assert f.pools.status == "starting" and f.owner.claimed
    with pytest.raises(RuntimeError):
        await operation.start()
    assert f.order.count("claim") == 1 and "start" not in f.order


async def test_only_authenticated_pid1_drain_can_be_retained_as_execution_outcome(tmp_path):
    f = setup(tmp_path)
    operation = activation(f)
    await operation.start()
    assert (await operation.status())["state"] == "running"
    f.owner.finish(7)
    result = await operation.status()
    assert result["state"] == "drained" and result["exit_code"] == 7
    assert f.pools.status == "running"  # Observation alone never releases capacity.


async def test_authority_revoked_after_claim_does_not_start_project_command(tmp_path):
    f = setup(tmp_path)
    original = f.channel.control

    async def revoked(*args):
        result = await original(*args)
        f.pools.revoked = True
        return result

    f.channel.control = revoked
    operation = activation(f)
    with pytest.raises(RuntimeError):
        await operation.start()
    assert f.owner.claimed and f.owner.state == "waiting" and "start" not in f.order
    assert f.pools.status == "starting"
