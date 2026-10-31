# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Pinned operator measurements complement, and never invent, Kubernetes PID policy."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

from firefly_forge.runner_kubernetes import Guard, KubernetesControlClient, KubernetesIdentityChanged
from firefly_forge.runner_pool_models import KubernetesProjectInvocation, pool_digest


class KubernetesQualificationPin(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    path: Path
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class QualifiedNode(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    uid: str
    configuration_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    node_info_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    pod_pids_limit: int = Field(gt=0)


class KubernetesRuntimeQualification(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    protocol: Literal["forge-kubernetes-runtime-qualification-v1"]
    status: Literal["ROOT_ACCEPTED_OWNED_RUNTIME_NOT_PRODUCTION_OR_CUSTOMER_ACCEPTANCE"]
    profile_revision: str = Field(pattern=r"^[a-f0-9]{64}$")
    network_policy_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    runtime_class_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    storage_class_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    nodes: dict[str, QualifiedNode]
    pid_limit_enforced: Literal[True]
    network_none_enforced: Literal[True]
    rwop_exclusivity_observed: Literal[True]
    single_uid_activation_observed: Literal[True]
    evidence_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="before")
    @classmethod
    def measured_booleans(cls, value: Any) -> Any:
        if isinstance(value, dict) and any(
            value.get(key) is not True
            for key in (
                "pid_limit_enforced",
                "network_none_enforced",
                "rwop_exclusivity_observed",
                "single_uid_activation_observed",
            )
        ):
            raise ValueError("Kubernetes runtime prerequisites require exact measured acceptance")
        return value


def read_qualification(pin: KubernetesQualificationPin) -> KubernetesRuntimeQualification:
    path = pin.path
    if not path.is_absolute() or path.resolve(strict=True) != path:
        raise ValueError("Runtime qualification requires its physical private file")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        before = os.fstat(fd)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.getuid()
            or stat.S_IMODE(before.st_mode) != 0o600
            or before.st_nlink != 1
            or before.st_size > 1048576
        ):
            raise ValueError("Runtime qualification must be a bounded private owner-controlled file")
        with os.fdopen(os.dup(fd), "rb") as stream:
            data = stream.read(1048577)

        def stamp(value: os.stat_result) -> tuple[int, ...]:
            return (
                value.st_dev,
                value.st_ino,
                value.st_uid,
                value.st_gid,
                value.st_mode,
                value.st_nlink,
                value.st_size,
                value.st_mtime_ns,
                value.st_ctime_ns,
            )

        if (
            stamp(os.fstat(fd)) != stamp(before)
            or stamp(path.lstat()) != stamp(before)
            or hashlib.sha256(data).hexdigest() != pin.sha256
        ):
            raise ValueError("The reviewed Kubernetes runtime qualification changed")
    finally:
        os.close(fd)
    return KubernetesRuntimeQualification.model_validate(json.loads(data))


def resource_identity(value: dict[str, Any], fields: tuple[str, ...]) -> str:
    return pool_digest({"uid": value.get("metadata", {}).get("uid"), **{name: value.get(name) for name in fields}})


class KubernetesRuntimeVerifier:
    def __init__(self, control: KubernetesControlClient, pin: KubernetesQualificationPin | None) -> None:
        self.control, self.pin = control, pin

    def qualification(self, invocation: KubernetesProjectInvocation | None = None) -> KubernetesRuntimeQualification:
        if self.pin is None:
            raise ValueError("Kubernetes node PID, network, CSI and image qualification is required before activation")
        result = read_qualification(self.pin)
        profile = self.control.authority.profile
        policy = profile.kubernetes
        assert policy is not None
        if (
            result.profile_revision != profile.revision
            or set(result.nodes) != set(policy.node_names)
            or any(node.pod_pids_limit != profile.max_pids for node in result.nodes.values())
            or invocation is not None
            and invocation.pids_limit != profile.max_pids
        ):
            raise ValueError("The invocation must select the exact measured node PID policy; no automatic clamping")
        return result

    async def verify(self, guard: Guard, invocation: KubernetesProjectInvocation | None = None) -> None:
        expected = self.qualification(invocation)
        control, profile = self.control, self.control.authority.profile
        policy = profile.kubernetes
        assert policy is not None
        await control.verify_namespace(guard)

        async def get(path: str) -> dict[str, Any]:
            response = await control._request("GET", path, guard)
            if response.status_code != 200 or type(response.json()) is not dict:
                raise KubernetesIdentityChanged("The qualified Kubernetes runtime identity is unavailable")
            return cast(dict[str, Any], response.json())

        policies = await get("/apis/networking.k8s.io/v1/namespaces/" + policy.namespace + "/networkpolicies?limit=2")
        values = policies.get("items")
        if not isinstance(values, list) or len(values) != 1 or policies.get("metadata", {}).get("continue"):
            raise KubernetesIdentityChanged("Network isolation requires the exact qualified namespace policy set")
        network = values[0]
        network_spec = network.get("spec", {})
        if (
            network.get("metadata", {}).get("uid") != str(policy.network_policy_uid)
            or network.get("metadata", {}).get("name") != policy.network_policy_name
            or network_spec.get("podSelector") != {}
            or set(network_spec) - {"podSelector", "policyTypes", "ingress", "egress"}
            or sorted(network_spec.get("policyTypes", [])) != ["Egress", "Ingress"]
            or network_spec.get("ingress", []) != []
            or network_spec.get("egress", []) != []
            or resource_identity(network, ("spec",)) != expected.network_policy_sha256
        ):
            raise KubernetesIdentityChanged("The qualified namespace no-network policy changed")
        runtime = await get("/apis/node.k8s.io/v1/runtimeclasses/" + policy.runtime_class)
        if resource_identity(runtime, ("handler", "overhead", "scheduling")) != expected.runtime_class_sha256:
            raise KubernetesIdentityChanged("The qualified runtime class changed")
        storage = await get("/apis/storage.k8s.io/v1/storageclasses/" + policy.storage_class)
        if (
            storage.get("reclaimPolicy") != "Delete"
            or resource_identity(
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
            )
            != expected.storage_class_sha256
        ):
            raise KubernetesIdentityChanged("The qualified CSI class or reclaim policy changed")
        for name, qualified in expected.nodes.items():
            node = await get("/api/v1/nodes/" + name)
            metadata, status = node.get("metadata", {}), node.get("status", {})
            images = {image for item in status.get("images", []) for image in item.get("names", [])}
            if (
                metadata.get("uid") != qualified.uid
                or metadata.get("annotations", {}).get("forge.fireflyframework.org/qualified-config-sha256")
                != qualified.configuration_sha256
                or pool_digest(status.get("nodeInfo")) != qualified.node_info_sha256
                or node.get("spec", {}).get("unschedulable", False) is not False
                or not any(c.get("type") == "Ready" and c.get("status") == "True" for c in status.get("conditions", []))
                or not set((*profile.approved_images, profile.transfer_image)) <= images
            ):
                raise KubernetesIdentityChanged("A qualified node, enforcement configuration or cached image changed")
        await guard()
        if self.qualification(invocation) != expected:
            raise KubernetesIdentityChanged("The runtime qualification changed during inspection")
