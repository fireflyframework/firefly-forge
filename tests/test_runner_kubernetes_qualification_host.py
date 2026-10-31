# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Fictional operator measurements exercise gates, never claim cluster qualification."""

import hashlib
import importlib
import json
import os
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from test_runner_kubernetes_exec_host import prepared


def qualified(tmp_path):
    module = importlib.import_module("firefly_forge.runner_kubernetes_qualification")
    control, invocation, job, pod, _ = prepared(tmp_path)
    policy = control.authority.profile.kubernetes
    network = {
        "metadata": {"name": policy.network_policy_name, "uid": str(policy.network_policy_uid)},
        "spec": {"podSelector": {}, "policyTypes": ["Ingress", "Egress"], "ingress": [], "egress": []},
    }
    runtime = {"metadata": {"name": policy.runtime_class, "uid": str(uuid4())}, "handler": "qualified-runtime"}
    storage = {
        "metadata": {"name": policy.storage_class, "uid": str(uuid4())},
        "provisioner": "csi.fixture.test",
        "reclaimPolicy": "Delete",
        "volumeBindingMode": "WaitForFirstConsumer",
    }
    node = {
        "metadata": {
            "name": policy.node_names[0],
            "uid": str(uuid4()),
            "annotations": {
                "forge.fireflyframework.org/qualified-config-sha256": "f" * 64,
            },
        },
        "spec": {},
        "status": {
            "nodeInfo": {"kernelVersion": "controlled-fixture", "containerRuntimeVersion": "fixture://1"},
            "conditions": [{"type": "Ready", "status": "True"}],
            "images": [
                {"names": list((*control.authority.profile.approved_images, control.authority.profile.transfer_image))}
            ],
        },
    }
    value = {
        "protocol": "forge-kubernetes-runtime-qualification-v1",
        "status": "ROOT_ACCEPTED_OWNED_RUNTIME_NOT_PRODUCTION_OR_CUSTOMER_ACCEPTANCE",
        "profile_revision": control.authority.profile.revision,
        "network_policy_sha256": module.resource_identity(network, ("spec",)),
        "runtime_class_sha256": module.resource_identity(runtime, ("handler", "overhead", "scheduling")),
        "storage_class_sha256": module.resource_identity(
            storage,
            (
                "provisioner",
                "parameters",
                "reclaimPolicy",
                "volumeBindingMode",
                "allowVolumeExpansion",
                "mountOptions",
                "allowedTopologies",
            ),
        ),
        "nodes": {
            policy.node_names[0]: {
                "uid": node["metadata"]["uid"],
                "configuration_sha256": "f" * 64,
                "node_info_sha256": module.pool_digest(node["status"]["nodeInfo"]),
                "pod_pids_limit": control.authority.profile.max_pids,
            }
        },
        "pid_limit_enforced": True,
        "network_none_enforced": True,
        "rwop_exclusivity_observed": True,
        "single_uid_activation_observed": True,
        "evidence_sha256": "a" * 64,
    }
    path = tmp_path / "fictional-qualification.json"
    path.write_text(json.dumps(value))
    path.chmod(0o600)
    pin = module.KubernetesQualificationPin(path=path, sha256=hashlib.sha256(path.read_bytes()).hexdigest())
    authority = replace(control.authority, qualification=pin)
    documents = {
        "/api/v1/namespaces/" + policy.namespace: {
            "metadata": {"name": policy.namespace, "uid": str(policy.namespace_uid)}
        },
        "/apis/networking.k8s.io/v1/namespaces/" + policy.namespace + "/networkpolicies": {
            "metadata": {},
            "items": [network],
        },
        "/apis/node.k8s.io/v1/runtimeclasses/" + policy.runtime_class: runtime,
        "/apis/storage.k8s.io/v1/storageclasses/" + policy.storage_class: storage,
        "/api/v1/nodes/" + policy.node_names[0]: node,
    }
    return SimpleNamespace(
        module=module,
        authority=authority,
        invocation=invocation,
        value=value,
        path=path,
        pin=pin,
        documents=documents,
        network=network,
        storage=storage,
        runtime=runtime,
        node=node,
    )


async def test_exact_qualified_runtime_and_selected_pid_limit_revalidate_all_current_identities(tmp_path):
    from firefly_forge.runner_kubernetes import KubernetesControlClient

    f = qualified(tmp_path)
    requests = []

    def handle(request):
        requests.append(request.method)
        return httpx.Response(200, json=f.documents[request.url.path])

    async def current():
        return None

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        await f.module.KubernetesRuntimeVerifier(KubernetesControlClient(f.authority, client=client), f.pin).verify(
            current, f.invocation
        )
    assert requests == ["GET"] * 5


@pytest.mark.parametrize(
    "fault",
    ["pid", "extra_policy", "policy", "node_uid", "node_config", "node_ready", "image", "runtime", "storage", "file"],
)
async def test_drift_of_current_node_policy_or_operator_proof_refuses_activation(tmp_path, fault):
    from firefly_forge.runner_kubernetes import KubernetesControlClient

    f = qualified(tmp_path)
    invocation = f.invocation
    if fault == "pid":
        invocation = invocation.model_copy(update={"pids_limit": invocation.pids_limit - 1})
    elif fault == "extra_policy":
        f.documents[next(p for p in f.documents if p.endswith("/networkpolicies"))]["items"].append(f.network)
    elif fault == "policy":
        f.network["spec"]["egress"] = [{}]
    elif fault == "node_uid":
        f.node["metadata"]["uid"] = str(uuid4())
    elif fault == "node_config":
        f.node["metadata"]["annotations"]["forge.fireflyframework.org/qualified-config-sha256"] = "e" * 64
    elif fault == "node_ready":
        f.node["status"]["conditions"][0]["status"] = "False"
    elif fault == "image":
        f.node["status"]["images"] = []
    elif fault == "runtime":
        f.runtime["handler"] = "unqualified-handler"
    elif fault == "storage":
        f.storage["reclaimPolicy"] = "Retain"
    else:
        f.path.write_text(f.path.read_text() + " ")

    async def current():
        return None

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=f.documents[r.url.path]))
    ) as client:
        with pytest.raises((RuntimeError, ValueError)):
            await f.module.KubernetesRuntimeVerifier(KubernetesControlClient(f.authority, client=client), f.pin).verify(
                current, invocation
            )


def test_access_time_is_not_mistaken_for_mutated_qualification_content(tmp_path, monkeypatch):
    f = qualified(tmp_path)
    original = os.fstat
    count = 0

    def observed(fd):
        nonlocal count
        count += 1
        result = original(fd)
        if count > 1:
            values = {name: getattr(result, name) for name in dir(result) if name.startswith("st_")}
            values["st_atime"] += 1
            values["st_atime_ns"] += 1000000000
            return SimpleNamespace(**values)
        return result

    monkeypatch.setattr(f.module.os, "fstat", observed)
    assert f.module.read_qualification(f.pin).profile_revision == f.authority.profile.revision


def test_missing_or_nonboolean_runtime_acceptance_is_never_synthesized(tmp_path):
    f = qualified(tmp_path)
    with pytest.raises(ValueError):
        f.module.KubernetesRuntimeVerifier(SimpleNamespace(authority=f.authority), None).qualification()
    for field in (
        "pid_limit_enforced",
        "network_none_enforced",
        "rwop_exclusivity_observed",
        "single_uid_activation_observed",
    ):
        with pytest.raises(ValueError):
            f.module.KubernetesRuntimeQualification.model_validate({**f.value, field: 1})
