# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Recorded Kubernetes transport cannot establish live cluster acceptance."""

import importlib
import json
import ssl
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr
from test_runner_kubernetes_host import kubernetes_policy
from test_runner_pool_dispatch import policy as docker_policy
from test_runner_pool_dispatch import request as docker_request

from firefly_forge.runner_bundle_models import ProjectBundleIdentity
from firefly_forge.runner_pool_models import KubernetesProjectInvocation, RunnerPoolProfile


def configured():
    policy = docker_policy().model_dump(mode="json")
    policy.update(
        protocol="forge-kubernetes-pool-v1",
        backend="kubernetes",
        kubernetes=kubernetes_policy().model_dump(mode="json"),
        work_duration={"protocol": "forge-work-duration-v2", "timeout_seconds": 300},
    )
    return RunnerPoolProfile.model_validate(policy)


def request(profile):
    value = docker_request(profile).model_dump(mode="json")
    bundle = ProjectBundleIdentity(
        archive_digest=value["archive_digest"],
        archive_bytes=value["source_bytes"],
        source_digest=value["source_digest"],
        source_archive_digest="e" * 64,
        source_archive_bytes=50,
        git_digest="f" * 64,
        git_archive_digest="a" * 64,
        git_archive_bytes=25,
    )
    value.update(
        bundle=bundle.model_dump(mode="json"),
        cpus=1.0,
        memory_mb=512,
        pids_limit=128,
        work_duration={"protocol": "forge-work-duration-v2", "timeout_seconds": 300},
    )
    return KubernetesProjectInvocation.model_validate(value)


async def test_namespace_identity_is_checked_between_lease_guards(tmp_path):
    module = importlib.import_module("firefly_forge.runner_kubernetes")
    profile = configured()
    order = []

    async def guard():
        order.append("guard")

    def handle(incoming):
        order.append("GET")
        assert incoming.url.path == "/api/v1/namespaces/project-workers"
        assert incoming.headers["Authorization"] == "Bearer public-control-fixture"
        return httpx.Response(
            200, json={"metadata": {"name": profile.kubernetes.namespace, "uid": str(profile.kubernetes.namespace_uid)}}
        )

    authority = module.KubernetesAuthority(
        profile=profile,
        api_origin="https://kubernetes.fixture",
        tls=ssl.create_default_context(),
        token=SecretStr("public-control-fixture"),
        state_directory=tmp_path,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        service = module.KubernetesControlClient(authority, client=client)
        await service.verify_namespace(guard)
    assert order == ["guard", "GET", "guard"]


async def test_changed_namespace_uid_is_not_reinterpreted_as_the_configured_cluster(tmp_path):
    module = importlib.import_module("firefly_forge.runner_kubernetes")
    profile = configured()

    async def guard():
        return None

    authority = module.KubernetesAuthority(
        profile=profile,
        api_origin="https://kubernetes.fixture",
        tls=ssl.create_default_context(),
        token=SecretStr("public-control-fixture"),
        state_directory=tmp_path,
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200, json={"metadata": {"name": profile.kubernetes.namespace, "uid": str(uuid4())}}
            )
        )
    ) as client:
        with pytest.raises(module.KubernetesIdentityChanged):
            await module.KubernetesControlClient(authority, client=client).verify_namespace(guard)


def test_command_job_is_parked_without_credentials_or_automatic_retries(tmp_path):
    module = importlib.import_module("firefly_forge.runner_kubernetes")
    profile = configured()
    invocation = request(profile)
    authority = module.KubernetesAuthority(
        profile=profile,
        api_origin="https://kubernetes.fixture",
        tls=ssl.create_default_context(),
        token=SecretStr("public-control-fixture"),
        state_directory=tmp_path,
    )
    job = module.command_job(authority, invocation)
    assert job["metadata"]["namespace"] == profile.kubernetes.namespace
    assert job["spec"]["backoffLimit"] == 0
    assert "activeDeadlineSeconds" not in job["spec"] and "ttlSecondsAfterFinished" not in job["spec"]
    pod = job["spec"]["template"]["spec"]
    assert pod["automountServiceAccountToken"] is False and pod["enableServiceLinks"] is False
    assert pod["hostNetwork"] is False and pod["hostPID"] is False and pod["hostIPC"] is False
    assert pod["restartPolicy"] == "Never"
    container = pod["containers"][0]
    assert container["image"] == invocation.image
    assert container["securityContext"] == {
        "runAsNonRoot": True,
        "runAsUser": 10001,
        "runAsGroup": 10001,
        "allowPrivilegeEscalation": False,
        "readOnlyRootFilesystem": True,
        "capabilities": {"drop": ["ALL"]},
        "seccompProfile": {"type": "RuntimeDefault"},
    }
    assert container["command"][:4] == ["/app/.venv/bin/python", "-I", "-S", "-c"]
    assert list(invocation.argv) != container["command"]
    assert any(
        env.get("valueFrom", {}).get("fieldRef", {}).get("fieldPath") == "metadata.uid" for env in container["env"]
    )
    assert not any("secret" in str(value).lower() or "hostPath" in value for value in pod["volumes"])
    assert all("public-control-fixture" not in str(value) for value in job.values())


async def test_lost_create_response_is_unknown_without_automatic_job_resubmission(tmp_path):
    module = importlib.import_module("firefly_forge.runner_kubernetes")
    profile = configured()
    authority = module.KubernetesAuthority(
        profile=profile,
        api_origin="https://kubernetes.fixture",
        tls=ssl.create_default_context(),
        token=SecretStr("public-control-fixture"),
        state_directory=tmp_path,
    )
    calls = []

    async def guard():
        return None

    def handle(incoming):
        calls.append(incoming.method)
        if incoming.method == "GET":
            return httpx.Response(
                200,
                json={"metadata": {"name": profile.kubernetes.namespace, "uid": str(profile.kubernetes.namespace_uid)}},
            )
        raise httpx.ReadTimeout("Controlled lost Kubernetes reply", request=incoming)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        service = module.KubernetesControlClient(authority, client=client)
        with pytest.raises(module.KubernetesEffectUnknown):
            await service.create_parked_job(request(profile), guard)
    assert calls == ["GET", "POST"]


async def test_job_readback_and_deletion_require_exact_native_uid_and_spec(tmp_path):
    module = importlib.import_module("firefly_forge.runner_kubernetes")
    profile = configured()
    authority = module.KubernetesAuthority(
        profile=profile,
        api_origin="https://kubernetes.fixture",
        tls=ssl.create_default_context(),
        token=SecretStr("public-control-fixture"),
        state_directory=tmp_path,
    )
    uid = str(uuid4())
    saved, deleted = [], []

    async def guard():
        return None

    def handle(incoming):
        if incoming.url.path == "/api/v1/namespaces/project-workers":
            return httpx.Response(
                200,
                json={"metadata": {"name": profile.kubernetes.namespace, "uid": str(profile.kubernetes.namespace_uid)}},
            )
        if incoming.method == "POST":
            value = json.loads(incoming.content)
            value["metadata"]["uid"] = uid
            value["spec"]["selector"] = {"matchLabels": {"controller-uid": uid}}
            saved.append(value)
            return httpx.Response(201, json=value)
        if incoming.method == "DELETE":
            deleted.append(json.loads(incoming.content))
            return httpx.Response(200, json={"status": "Success"})
        return httpx.Response(200, json=saved[0])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        service = module.KubernetesControlClient(authority, client=client)
        invocation = request(profile)
        receipt = await service.create_parked_job(invocation, guard)
        assert receipt.uid == uid
        assert await service.observe_job(invocation, receipt, guard) == saved[0]
        saved[0]["spec"]["template"]["spec"]["containers"][0]["securityContext"]["runAsUser"] = 0
        with pytest.raises(module.KubernetesIdentityChanged):
            await service.observe_job(invocation, receipt, guard)
        saved[0]["spec"]["template"]["spec"]["containers"][0]["securityContext"]["runAsUser"] = 10001
        saved[0]["metadata"]["uid"] = str(uuid4())
        with pytest.raises(module.KubernetesIdentityChanged):
            await service.observe_job(invocation, receipt, guard)
        assert not deleted
        saved[0]["metadata"]["uid"] = uid
        await service.delete_job(invocation, receipt, guard)
        assert deleted == [
            {
                "apiVersion": "v1",
                "kind": "DeleteOptions",
                "preconditions": {"uid": uid},
                "propagationPolicy": "Foreground",
            }
        ]


@pytest.mark.parametrize("change", ["lifecycle", "boolean_coercion"])
async def test_create_reply_cannot_add_an_unreviewed_exec_surface_or_coerce_a_security_flag(change, tmp_path):
    module = importlib.import_module("firefly_forge.runner_kubernetes")
    profile = configured()
    authority = module.KubernetesAuthority(
        profile=profile,
        api_origin="https://kubernetes.fixture",
        tls=ssl.create_default_context(),
        token=SecretStr("public-control-fixture"),
        state_directory=tmp_path,
    )

    async def guard():
        return None

    def handle(incoming):
        if incoming.method == "GET":
            return httpx.Response(
                200,
                json={"metadata": {"name": profile.kubernetes.namespace, "uid": str(profile.kubernetes.namespace_uid)}},
            )
        value = json.loads(incoming.content)
        value["metadata"]["uid"] = str(uuid4())
        container = value["spec"]["template"]["spec"]["containers"][0]
        if change == "lifecycle":
            container["lifecycle"] = {"postStart": {"exec": {"command": ["unreviewed-executable"]}}}
        else:
            container["securityContext"]["runAsNonRoot"] = 1
        return httpx.Response(201, json=value)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(module.KubernetesEffectUnknown):
            await module.KubernetesControlClient(authority, client=client).create_parked_job(request(profile), guard)


@pytest.mark.parametrize("cpus,expected", [(1.0, "1"), (0.5, "500m"), (32.0, "32"), (0.001, "1m"), (1000, "1k")])
def test_cpu_quantities_are_canonical_without_rounding_admitted_resources(cpus, expected):
    module = importlib.import_module("firefly_forge.runner_kubernetes_models")
    assert module.cpu_quantity(cpus) == expected


@pytest.mark.parametrize("cpus", [0.0001, 1.0001, float("inf"), float("nan"), 0, -1, 2**63])
def test_unsupported_cpu_precision_or_nonpositive_capacity_is_refused(cpus):
    module = importlib.import_module("firefly_forge.runner_kubernetes_models")
    with pytest.raises(ValueError):
        module.cpu_quantity(cpus)


@pytest.mark.parametrize("mib,expected", [(512, "512Mi"), (1024, "1Gi"), (65536, "64Gi"), (1536, "1536Mi")])
def test_memory_quantities_preserve_exact_binary_capacity(mib, expected):
    module = importlib.import_module("firefly_forge.runner_kubernetes_models")
    assert module.memory_quantity(mib) == expected


@pytest.mark.parametrize("mib", [0, -1, True, 2**43])
def test_memory_capacity_that_the_api_would_cap_is_refused(mib):
    module = importlib.import_module("firefly_forge.runner_kubernetes_models")
    with pytest.raises(ValueError):
        module.memory_quantity(mib)


async def test_pod_activation_identity_is_single_job_owned_uid_and_never_a_replacement(tmp_path):
    import copy

    module = importlib.import_module("firefly_forge.runner_kubernetes")
    profile = configured()
    invocation = request(profile)
    authority = module.KubernetesAuthority(
        profile=profile,
        api_origin="https://kubernetes.fixture",
        tls=ssl.create_default_context(),
        token=SecretStr("public-control-fixture"),
        state_directory=tmp_path,
    )
    job = module.command_job(authority, invocation)
    job["metadata"]["uid"] = str(uuid4())
    receipt = module.KubernetesJobReceipt(
        name=job["metadata"]["name"],
        namespace=job["metadata"]["namespace"],
        uid=job["metadata"]["uid"],
        execution_id=invocation.execution_id,
        invocation_digest=module.pool_digest(invocation.model_dump(mode="json")),
        profile_revision=profile.revision,
        spec_digest=module.pool_digest(job["spec"]),
    )
    pod = copy.deepcopy(job["spec"]["template"])
    pod["metadata"].update(
        name="owned-pod",
        namespace=receipt.namespace,
        uid=str(uuid4()),
        ownerReferences=[
            {
                "apiVersion": "batch/v1",
                "kind": "Job",
                "name": receipt.name,
                "uid": receipt.uid,
                "controller": True,
                "blockOwnerDeletion": True,
            }
        ],
    )
    pod["spec"]["nodeName"] = profile.kubernetes.node_names[0]
    pod["status"] = {"phase": "Running"}
    selected = [pod]

    async def guard():
        return None

    def handle(incoming):
        if incoming.url.path.endswith("/namespaces/project-workers"):
            return httpx.Response(
                200, json={"metadata": {"name": receipt.namespace, "uid": str(profile.kubernetes.namespace_uid)}}
            )
        if "/jobs/" in incoming.url.path:
            return httpx.Response(200, json=job)
        if incoming.url.path.endswith("/pods"):
            assert incoming.url.params["limit"] == "2"
            return httpx.Response(200, json={"items": selected, "metadata": {}})
        return httpx.Response(200, json=pod)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        control = module.KubernetesControlClient(authority, client=client)
        pod["spec"].pop("nodeName")
        assert await control.bind_pod(invocation, receipt, guard) is None
        pod["spec"]["nodeName"] = profile.kubernetes.node_names[0]
        bound = await control.bind_pod(invocation, receipt, guard)
        assert bound.uid == pod["metadata"]["uid"] and bound.job_uid == receipt.uid
        assert await control.observe_pod(invocation, receipt, bound, guard) == pod
        selected.append(copy.deepcopy(pod))
        with pytest.raises(module.KubernetesIdentityChanged):
            await control.observe_pod(invocation, receipt, bound, guard)
        selected.pop()
        pod["metadata"]["uid"] = str(uuid4())
        with pytest.raises(module.KubernetesIdentityChanged):
            await control.observe_pod(invocation, receipt, bound, guard)
        selected.append(copy.deepcopy(pod))
        with pytest.raises(module.KubernetesIdentityChanged):
            await control.bind_pod(invocation, receipt, guard)
        selected.pop()
        pod["metadata"]["ownerReferences"][0]["uid"] = str(uuid4())
        with pytest.raises(module.KubernetesIdentityChanged):
            await control.bind_pod(invocation, receipt, guard)


async def test_control_refuses_changed_private_scratch_or_disabled_tls_before_http(tmp_path):
    module = importlib.import_module("firefly_forge.runner_kubernetes")
    profile = configured()
    tls = ssl.create_default_context()
    authority = module.KubernetesAuthority(
        profile=profile,
        api_origin="https://kubernetes.fixture",
        tls=tls,
        token=SecretStr("public-control-fixture"),
        state_directory=tmp_path,
    )
    calls = []

    async def guard():
        return None

    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: calls.append(r))) as client:
        control = module.KubernetesControlClient(authority, client=client)
        tmp_path.chmod(0o755)
        try:
            with pytest.raises(ValueError, match="private"):
                await control.verify_namespace(guard)
        finally:
            tmp_path.chmod(0o700)
        tls.check_hostname = False
        with pytest.raises(ValueError, match="TLS"):
            await control.verify_namespace(guard)
    assert calls == []


def test_transfer_job_has_a_distinct_uid_scope_and_no_project_launcher(tmp_path):
    module = importlib.import_module("firefly_forge.runner_kubernetes")
    profile = configured()
    invocation = request(profile)
    authority = module.KubernetesAuthority(
        profile=profile,
        api_origin="https://kubernetes.fixture",
        tls=ssl.create_default_context(),
        token=SecretStr("public-control-fixture"),
        state_directory=tmp_path,
    )
    job = module.transfer_job(authority, invocation)
    assert job["metadata"]["name"] == "forge-" + invocation.execution_id + "-transfer"
    pod = job["spec"]["template"]["spec"]
    container = pod["containers"][0]
    assert container["name"] == "transfer" and container["image"] == profile.transfer_image
    assert job["metadata"]["labels"]["forge.fireflyframework.org/phase"] == "transfer"
    assert container["volumeMounts"] == [
        {"name": "input", "mountPath": "/forge-input", "readOnly": False},
        {"name": "temporary", "mountPath": "/tmp"},
    ]
    assert not any(v.get("name") == "workspace" for v in pod["volumes"])
    assert "OwnerLiveness" not in container["command"][-1] and list(invocation.argv) != container["command"]
    assert pod["securityContext"]["fsGroup"] == profile.kubernetes.gid
    assert container["imagePullPolicy"] == "Never"


async def test_transfer_create_receipt_is_separate_and_cannot_be_used_as_a_command_receipt(tmp_path):
    module = importlib.import_module("firefly_forge.runner_kubernetes")
    profile = configured()
    invocation = request(profile)
    authority = module.KubernetesAuthority(
        profile=profile,
        api_origin="https://kubernetes.fixture",
        tls=ssl.create_default_context(),
        token=SecretStr("public-control-fixture"),
        state_directory=tmp_path,
    )
    saved = []

    async def guard():
        return None

    def handle(incoming):
        if incoming.url.path.endswith("/namespaces/project-workers"):
            return httpx.Response(
                200,
                json={"metadata": {"name": profile.kubernetes.namespace, "uid": str(profile.kubernetes.namespace_uid)}},
            )
        if incoming.method == "POST":
            job = json.loads(incoming.content)
            job["metadata"]["uid"] = str(uuid4())
            saved.append(job)
            return httpx.Response(201, json=job)
        return httpx.Response(200, json=saved[0])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        control = module.KubernetesControlClient(authority, client=client)
        receipt = await control.create_transfer_job(invocation, guard)
        assert receipt.phase == "transfer" and receipt.name.endswith("-transfer")
        assert await control.observe_job(invocation, receipt, guard) == saved[0]
        with pytest.raises(module.KubernetesIdentityChanged):
            await control.observe_job(invocation, receipt.model_copy(update={"phase": "command"}), guard)


@pytest.mark.parametrize("unsafe", [False, True])
async def test_server_omitted_false_defaults_keep_isolation_but_true_host_network_is_refused(tmp_path, unsafe):
    module = importlib.import_module("firefly_forge.runner_kubernetes")
    profile = configured()
    invocation = request(profile)
    authority = module.KubernetesAuthority(
        profile=profile,
        api_origin="https://kubernetes.fixture",
        tls=ssl.create_default_context(),
        token=SecretStr("public-control-fixture"),
        state_directory=tmp_path,
    )

    async def guard():
        return None

    def handle(incoming):
        if incoming.method == "GET":
            return httpx.Response(
                200,
                json={"metadata": {"name": profile.kubernetes.namespace, "uid": str(profile.kubernetes.namespace_uid)}},
            )
        job = json.loads(incoming.content)
        job["metadata"]["uid"] = str(uuid4())
        spec = job["spec"]["template"]["spec"]
        for key in ("hostNetwork", "hostPID", "hostIPC"):
            spec.pop(key)
        for volume in spec["volumes"]:
            if volume.get("persistentVolumeClaim", {}).get("readOnly") is False:
                volume["persistentVolumeClaim"].pop("readOnly")
        for mount in spec["containers"][0]["volumeMounts"]:
            if mount.get("readOnly") is False:
                mount.pop("readOnly")
        if unsafe:
            spec["hostNetwork"] = True
        return httpx.Response(201, json=job)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        control = module.KubernetesControlClient(authority, client=client)
        if unsafe:
            with pytest.raises(module.KubernetesEffectUnknown):
                await control.create_transfer_job(invocation, guard)
        else:
            assert (await control.create_transfer_job(invocation, guard)).phase == "transfer"
