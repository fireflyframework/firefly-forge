# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""PVC identity observations never substitute for an owned process drain."""

import copy
import importlib
import ssl
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr
from test_runner_kubernetes_control_host import configured, request


def setup(tmp_path):
    module = importlib.import_module("firefly_forge.runner_kubernetes")
    profile = configured()
    authority = module.KubernetesAuthority(
        profile=profile,
        api_origin="https://kubernetes.fixture",
        tls=ssl.create_default_context(),
        token=SecretStr("public-volume-fixture"),
        state_directory=tmp_path,
    )
    return module, authority, request(profile)


def namespace(authority):
    return httpx.Response(
        200,
        json={
            "metadata": {
                "name": authority.profile.kubernetes.namespace,
                "uid": str(authority.profile.kubernetes.namespace_uid),
            }
        },
    )


def test_volume_plans_have_only_explicit_scoped_csi_claim_inputs(tmp_path):
    module, authority, invocation = setup(tmp_path)
    for purpose in ["input", "workspace"]:
        value = module.volume_claim(authority, invocation, purpose)
        assert value["kind"] == "PersistentVolumeClaim" and value["apiVersion"] == "v1"
        assert value["metadata"]["namespace"] == authority.profile.kubernetes.namespace
        assert value["spec"]["storageClassName"] == authority.profile.kubernetes.storage_class
        assert value["spec"]["accessModes"] == ["ReadWriteOncePod"]
        assert value["spec"]["volumeMode"] == "Filesystem"
        assert set(value["spec"]) == {"storageClassName", "accessModes", "volumeMode", "resources"}
        assert "public-volume-fixture" not in str(value)
    with pytest.raises(ValueError):
        module.volume_claim(authority, invocation, "foreign")


@pytest.mark.parametrize("change", [None, "uid", "spec", "source", "lease"])
async def test_pvc_create_and_current_identity_refuse_substitution(tmp_path, change):
    module, authority, invocation = setup(tmp_path)
    saved = None
    requests = []
    alive = True

    async def guard():
        if not alive:
            raise RuntimeError("Lease replaced")

    def handle(incoming):
        nonlocal saved
        requests.append(incoming.method)
        if incoming.url.path.endswith("/namespaces/project-workers"):
            return namespace(authority)
        if incoming.method == "POST":
            import json

            saved = json.loads(incoming.content)
            saved["metadata"]["uid"] = str(uuid4())
            return httpx.Response(201, json=saved)
        value = copy.deepcopy(saved)
        if change == "uid":
            value["metadata"]["uid"] = str(uuid4())
        elif change == "spec":
            value["spec"]["accessModes"] = ["ReadWriteMany"]
        elif change == "source":
            value["spec"]["dataSource"] = {"kind": "PersistentVolumeClaim", "name": "other-project"}
        return httpx.Response(200, json=value)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        service = module.KubernetesControlClient(authority, client=client)
        receipt = await service.create_volume(invocation, "input", guard)
        if change == "lease":
            alive = False
        if change is None:
            assert await service.observe_volume(invocation, receipt, guard) == saved
        else:
            with pytest.raises((module.KubernetesIdentityChanged, RuntimeError)):
                await service.observe_volume(invocation, receipt, guard)
    assert requests.count("POST") == 1


async def test_lost_pvc_creation_reply_is_never_retried_or_relabelled_absent(tmp_path):
    module, authority, invocation = setup(tmp_path)
    posts = []

    async def guard():
        return None

    def handle(incoming):
        if incoming.method == "GET":
            return namespace(authority)
        posts.append(incoming)
        raise httpx.ReadError("private provider failure")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(module.KubernetesEffectUnknown) as error:
            await module.KubernetesControlClient(authority, client=client).create_volume(invocation, "input", guard)
    assert len(posts) == 1 and "private provider failure" not in str(error.value)


@pytest.mark.parametrize("change", [None, "foreign-claim", "host-path", "replacement-pv"])
async def test_assigned_volume_requires_exact_claim_uid_and_retained_csi_binding(tmp_path, change):
    module, authority, invocation = setup(tmp_path)
    saved = None
    assigned = False
    replaced = False
    pv_uid = str(uuid4())
    requests = []

    async def guard():
        return None

    def handle(incoming):
        nonlocal saved
        requests.append(incoming.method)
        if incoming.url.path.endswith("/namespaces/project-workers"):
            return namespace(authority)
        if incoming.method == "POST":
            import json

            saved = json.loads(incoming.content)
            saved["metadata"]["uid"] = str(uuid4())
            return httpx.Response(201, json=saved)
        if "/persistentvolumes/" in incoming.url.path:
            spec = {
                "storageClassName": authority.profile.kubernetes.storage_class,
                "accessModes": ["ReadWriteOncePod"],
                "volumeMode": "Filesystem",
                "claimRef": {k: saved["metadata"][k] for k in ["name", "namespace", "uid"]},
                "csi": {"driver": "csi.fixture.test", "volumeHandle": "opaque-volume"},
                "capacity": {"storage": "1Gi"},
            }
            if change == "foreign-claim":
                spec["claimRef"]["uid"] = str(uuid4())
            if change == "host-path":
                spec.pop("csi")
                spec["hostPath"] = {"path": "/private"}
            return httpx.Response(
                200,
                json={
                    "apiVersion": "v1",
                    "kind": "PersistentVolume",
                    "metadata": {"name": "owned-csi-volume", "uid": str(uuid4()) if replaced else pv_uid},
                    "spec": spec,
                    "status": {"phase": "Bound"},
                },
            )
        value = copy.deepcopy(saved)
        if assigned:
            value["spec"]["volumeName"] = "owned-csi-volume"
            value["status"] = {"phase": "Bound"}
        return httpx.Response(200, json=value)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        service = module.KubernetesControlClient(authority, client=client)
        receipt = await service.create_volume(invocation, "input", guard)
        assert await service.bind_volume(invocation, receipt, guard) is None
        assigned = True
        if change in ["foreign-claim", "host-path"]:
            with pytest.raises(module.KubernetesIdentityChanged):
                await service.bind_volume(invocation, receipt, guard)
        else:
            bound = await service.bind_volume(invocation, receipt, guard)
            assert bound.claim == receipt and bound.volume_uid == pv_uid
            if change == "replacement-pv":
                replaced = True
                with pytest.raises(module.KubernetesIdentityChanged):
                    await service.bind_volume(invocation, receipt, guard, bound=bound)
            else:
                assert await service.bind_volume(invocation, receipt, guard, bound=bound) == bound
    assert requests.count("POST") == 1 and "DELETE" not in requests
