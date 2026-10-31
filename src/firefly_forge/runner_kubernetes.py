# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Explicit Kubernetes control identity and parked, credential-free command plans."""

from __future__ import annotations

import asyncio
import copy
import os
import ssl
import stat
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast

import httpx
from pydantic import SecretStr
from pyfly.container import service

from firefly_forge.execution_environment import container_python_environment
from firefly_forge.runner_kubernetes_models import (
    KubernetesBoundVolumeReceipt,
    KubernetesJobReceipt,
    KubernetesPodReceipt,
    KubernetesVolumeReceipt,
    cpu_quantity,
    memory_quantity,
)
from firefly_forge.runner_pool_launcher import LAUNCHER, owned_launcher
from firefly_forge.runner_pool_models import RunnerInvocation, RunnerPoolProfile, pool_digest
from firefly_forge.scm_authority_models import exact_origin

Guard = Callable[[], Awaitable[None]]

if TYPE_CHECKING:
    from firefly_forge.runner_kubernetes_qualification import KubernetesQualificationPin


class KubernetesIdentityChanged(RuntimeError):
    """The current control-plane object does not match the administrator binding."""


class KubernetesEffectUnknown(RuntimeError):
    """An operation was sent; native custody must retain it without resubmission."""


@dataclass(frozen=True)
class KubernetesAuthority:
    profile: RunnerPoolProfile
    api_origin: str
    tls: ssl.SSLContext
    token: SecretStr
    state_directory: Path
    qualification: KubernetesQualificationPin | None = None
    _state_identity: tuple[int, int] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if self.profile.backend != "kubernetes" or self.profile.kubernetes is None:
            raise ValueError("Kubernetes control requires its explicit immutable backend profile")
        exact_origin(self.api_origin)
        if not self.tls.check_hostname or self.tls.verify_mode != ssl.CERT_REQUIRED:
            raise ValueError("Kubernetes control requires verified TLS and hostname identity")
        if not self.token.get_secret_value().strip():
            raise ValueError("The administrator Kubernetes credential is unavailable")
        info = self._private_state()
        object.__setattr__(self, "_state_identity", (info.st_dev, info.st_ino))

    def _private_state(self) -> os.stat_result:
        path = self.state_directory
        info = path.lstat()
        if (
            not path.is_absolute()
            or path.resolve(strict=True) != path
            or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o700
        ):
            raise ValueError("Kubernetes controller scratch must be a private canonical owned directory")
        return info

    def verify(self) -> None:
        info = self._private_state()
        if (info.st_dev, info.st_ino) != self._state_identity:
            raise ValueError("Kubernetes controller private directory identity changed")
        if not self.tls.check_hostname or self.tls.verify_mode != ssl.CERT_REQUIRED:
            raise ValueError("Kubernetes control requires verified TLS and hostname identity")


def resource_name(invocation: RunnerInvocation, kind: str) -> str:
    if kind not in {"command", "input", "workspace", "transfer"}:
        raise ValueError("Unknown owned Kubernetes resource kind")
    return "forge-" + invocation.execution_id + "-" + kind


def command_job(authority: KubernetesAuthority, invocation: RunnerInvocation) -> dict[str, Any]:
    profile = authority.profile
    profile.validate_invocation(invocation)
    policy = profile.kubernetes
    assert policy is not None
    labels = {
        "forge.fireflyframework.org/execution": invocation.execution_id,
        "forge.fireflyframework.org/phase": "command",
    }
    security = {
        "runAsNonRoot": True,
        "runAsUser": policy.uid,
        "runAsGroup": policy.gid,
        "allowPrivilegeEscalation": False,
        "readOnlyRootFilesystem": True,
        "capabilities": {"drop": ["ALL"]},
        "seccompProfile": {"type": "RuntimeDefault"},
    }
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": resource_name(invocation, "command"),
            "namespace": policy.namespace,
            "labels": labels,
            "annotations": {
                "forge.fireflyframework.org/invocation": pool_digest(invocation.model_dump(mode="json")),
                "forge.fireflyframework.org/profile": profile.revision,
            },
        },
        "spec": {
            "parallelism": 1,
            "completions": 1,
            "backoffLimit": 0,
            "template": {
                "metadata": {"labels": labels},
                "spec": {
                    "serviceAccountName": policy.service_account,
                    "automountServiceAccountToken": False,
                    "enableServiceLinks": False,
                    "hostNetwork": False,
                    "hostPID": False,
                    "hostIPC": False,
                    "shareProcessNamespace": False,
                    "restartPolicy": "Never",
                    "runtimeClassName": policy.runtime_class,
                    "affinity": {
                        "nodeAffinity": {
                            "requiredDuringSchedulingIgnoredDuringExecution": {
                                "nodeSelectorTerms": [
                                    {
                                        "matchFields": [
                                            {
                                                "key": "metadata.name",
                                                "operator": "In",
                                                "values": list(policy.node_names),
                                            }
                                        ]
                                    }
                                ],
                            }
                        }
                    },
                    "securityContext": {
                        "runAsNonRoot": True,
                        "runAsUser": policy.uid,
                        "runAsGroup": policy.gid,
                        "fsGroup": policy.gid,
                        "fsGroupChangePolicy": "OnRootMismatch",
                        "seccompProfile": {"type": "RuntimeDefault"},
                    },
                    "containers": [
                        {
                            "name": "command",
                            "image": invocation.image,
                            "imagePullPolicy": "Never",
                            "command": [
                                "/app/.venv/bin/python",
                                "-I",
                                "-S",
                                "-c",
                                owned_launcher() if invocation.work_duration is not None else LAUNCHER,
                            ],
                            "securityContext": security,
                            "env": [
                                {"name": "FORGE_POD_UID", "valueFrom": {"fieldRef": {"fieldPath": "metadata.uid"}}},
                                *[
                                    {"name": name, "value": value}
                                    for name, value in {
                                        "HOME": "/tmp/forge-home",
                                        "XDG_CACHE_HOME": "/tmp/forge-cache",
                                        "GIT_CONFIG_NOSYSTEM": "1",
                                        "GIT_CONFIG_GLOBAL": "/dev/null",
                                        "GIT_ATTR_NOSYSTEM": "1",
                                        **container_python_environment(),
                                    }.items()
                                ],
                            ],
                            "resources": {
                                "requests": {
                                    "cpu": cpu_quantity(invocation.cpus),
                                    "memory": memory_quantity(invocation.memory_mb),
                                },
                                "limits": {
                                    "cpu": cpu_quantity(invocation.cpus),
                                    "memory": memory_quantity(invocation.memory_mb),
                                },
                            },
                            "volumeMounts": [
                                {"name": "input", "mountPath": "/forge-input", "subPath": "payload", "readOnly": True},
                                {
                                    "name": "input",
                                    "mountPath": "/workspace/.git",
                                    "subPath": "payload/git",
                                    "readOnly": True,
                                },
                                {"name": "workspace", "mountPath": "/workspace"},
                                {"name": "temporary", "mountPath": "/tmp"},
                            ],
                        }
                    ],
                    "volumes": [
                        {
                            "name": "input",
                            "persistentVolumeClaim": {
                                "claimName": resource_name(invocation, "input"),
                                "readOnly": True,
                            },
                        },
                        {
                            "name": "workspace",
                            "persistentVolumeClaim": {"claimName": resource_name(invocation, "workspace")},
                        },
                        {"name": "temporary", "emptyDir": {"medium": "Memory"}},
                    ],
                },
            },
        },
    }


def transfer_job(authority: KubernetesAuthority, invocation: RunnerInvocation) -> dict[str, Any]:
    job = copy.deepcopy(command_job(authority, invocation))
    job["metadata"]["name"] = resource_name(invocation, "transfer")
    job["metadata"]["labels"]["forge.fireflyframework.org/phase"] = "transfer"
    pod = job["spec"]["template"]
    pod["metadata"]["labels"]["forge.fireflyframework.org/phase"] = "transfer"
    spec = pod["spec"]
    container = spec["containers"][0]
    container.update(
        name="transfer",
        image=authority.profile.transfer_image,
        command=[
            "/app/.venv/bin/python",
            "-I",
            "-S",
            "-c",
            "import ctypes,os,signal\nassert os.getpid()==1\n"
            "assert ctypes.CDLL(None).prctl(4,0,0,0,0)==0\n"
            "signal.signal(signal.SIGTERM,lambda *args:os._exit(0))\n"
            "while True: signal.pause()\n",
        ],
        volumeMounts=[
            {"name": "input", "mountPath": "/forge-input", "readOnly": False},
            {"name": "temporary", "mountPath": "/tmp"},
        ],
    )
    spec["volumes"] = [v for v in spec["volumes"] if v["name"] != "workspace"]
    next(v for v in spec["volumes"] if v["name"] == "input")["persistentVolumeClaim"]["readOnly"] = False
    return job


def volume_claim(authority: KubernetesAuthority, invocation: RunnerInvocation, purpose: str) -> dict[str, Any]:
    authority.verify()
    authority.profile.validate_invocation(invocation)
    policy = authority.profile.kubernetes
    assert policy is not None
    if purpose not in {"input", "workspace"}:
        raise ValueError("Only separate native input and workspace claims are supported")
    storage = (
        max(1, (3 * invocation.source_bytes + invocation.stdin_bytes + 2097151) // 1048576)
        if purpose == "input"
        else policy.workspace_storage_mb
    )
    return {
        "apiVersion": "v1",
        "kind": "PersistentVolumeClaim",
        "metadata": {
            "name": resource_name(invocation, purpose),
            "namespace": policy.namespace,
            "labels": {
                "forge.fireflyframework.org/execution": invocation.execution_id,
                "forge.fireflyframework.org/purpose": purpose,
            },
            "annotations": {
                "forge.fireflyframework.org/invocation": pool_digest(invocation.model_dump(mode="json")),
                "forge.fireflyframework.org/profile": authority.profile.revision,
            },
        },
        "spec": {
            "storageClassName": policy.storage_class,
            "accessModes": ["ReadWriteOncePod"],
            "volumeMode": "Filesystem",
            "resources": {"requests": {"storage": memory_quantity(storage)}},
        },
    }


@service
class KubernetesControlClient:
    def __init__(self, authority: KubernetesAuthority, *, client: httpx.AsyncClient | None = None) -> None:
        self.authority = authority
        self._owned_client = client is None
        self.client = client or httpx.AsyncClient(
            verify=authority.tls, follow_redirects=False, trust_env=False, timeout=30
        )

    async def stop(self) -> None:
        if self._owned_client:
            await self.client.aclose()

    async def _request(
        self, method: str, path: str, guard: Guard, *, body: dict[str, Any] | None = None
    ) -> httpx.Response:
        await guard()
        self.authority.verify()
        try:
            response = await self.client.request(
                method,
                self.authority.api_origin + path,
                headers={"Authorization": "Bearer " + self.authority.token.get_secret_value()},
                json=body,
                follow_redirects=False,
            )
        except httpx.HTTPError:
            if method != "GET":
                raise KubernetesEffectUnknown("Kubernetes operation outcome is unknown; preserve its intent") from None
            raise KubernetesIdentityChanged("Kubernetes control observation is unavailable") from None
        await guard()
        self.authority.verify()
        return response

    async def verify_namespace(self, guard: Guard) -> None:
        policy = self.authority.profile.kubernetes
        assert policy is not None
        response = await self._request("GET", "/api/v1/namespaces/" + policy.namespace, guard)
        if response.status_code != 200:
            raise KubernetesIdentityChanged("The configured Kubernetes namespace cannot be verified")
        value = response.json()
        if (
            not isinstance(value, dict)
            or not isinstance(value.get("metadata"), dict)
            or value["metadata"].get("uid") != str(policy.namespace_uid)
            or value["metadata"].get("name") != policy.namespace
        ):
            raise KubernetesIdentityChanged("The configured Kubernetes namespace identity changed")

    def _volume_path(self, name: str | None = None) -> str:
        policy = self.authority.profile.kubernetes
        assert policy is not None
        return "/api/v1/namespaces/" + policy.namespace + "/persistentvolumeclaims" + ("/" + name if name else "")

    def _validate_volume_plan(self, value: Any, planned: dict[str, Any]) -> None:
        if (
            not isinstance(value, dict)
            or not isinstance(value.get("spec"), dict)
            or not isinstance(value.get("metadata"), dict)
            or value["metadata"].get("deletionTimestamp") is not None
            or not _contains_plan(value, planned)
            or not _exact_value({k: v for k, v in value["spec"].items() if k != "volumeName"}, planned["spec"])
        ):
            raise KubernetesIdentityChanged("The volume claim differs from its exact scoped request")
        if "volumeName" in value["spec"]:
            from firefly_forge.runner_kubernetes_models import kubernetes_name

            kubernetes_name(value["spec"]["volumeName"])

    async def create_volume(
        self, invocation: RunnerInvocation, purpose: Literal["input", "workspace"], guard: Guard
    ) -> KubernetesVolumeReceipt:
        planned = volume_claim(self.authority, invocation, purpose)
        await self.verify_namespace(guard)
        response = await self._request("POST", self._volume_path(), guard, body=planned)
        if response.status_code != 201:
            raise KubernetesEffectUnknown("Volume creation is unconfirmed; never recreate blindly")
        try:
            value = response.json()
            self._validate_volume_plan(value, planned)
            return KubernetesVolumeReceipt(
                purpose=purpose,
                name=planned["metadata"]["name"],
                namespace=planned["metadata"]["namespace"],
                uid=value["metadata"]["uid"],
                execution_id=invocation.execution_id,
                invocation_digest=pool_digest(invocation.model_dump(mode="json")),
                profile_revision=self.authority.profile.revision,
                spec_digest=pool_digest(value["spec"]),
            )
        except (ValueError, KeyError, TypeError, KubernetesIdentityChanged):
            raise KubernetesEffectUnknown(
                "Created volume identity is unconfirmed; preserve its native intent"
            ) from None

    async def observe_volume(
        self,
        invocation: RunnerInvocation,
        receipt: KubernetesVolumeReceipt,
        guard: Guard,
        *,
        bound: KubernetesBoundVolumeReceipt | None = None,
    ) -> dict[str, Any] | None:
        value = await self._current_volume(invocation, receipt, guard)
        if bound is not None and bound.claim != receipt:
            raise KubernetesIdentityChanged("A bound volume observation selected another claim")
        expected = bound.claim_spec_digest if bound is not None else receipt.spec_digest
        if value is not None and pool_digest(value["spec"]) != expected:
            raise KubernetesIdentityChanged("The owned volume specification changed")
        if value is not None and bound is not None:
            await self.bind_volume(invocation, receipt, guard, bound=bound)
        return value

    async def _current_volume(
        self, invocation: RunnerInvocation, receipt: KubernetesVolumeReceipt, guard: Guard
    ) -> dict[str, Any] | None:
        planned = volume_claim(self.authority, invocation, receipt.purpose)
        if (
            receipt.name != planned["metadata"]["name"]
            or receipt.namespace != planned["metadata"]["namespace"]
            or receipt.profile_revision != self.authority.profile.revision
            or receipt.execution_id != invocation.execution_id
            or receipt.invocation_digest != pool_digest(invocation.model_dump(mode="json"))
        ):
            raise KubernetesIdentityChanged("The volume receipt belongs to another native invocation")
        await self.verify_namespace(guard)
        response = await self._request("GET", self._volume_path(receipt.name), guard)
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise KubernetesIdentityChanged("The owned volume identity cannot be observed")
        value = response.json()
        self._validate_volume_plan(value, planned)
        if value["metadata"].get("uid") != receipt.uid:
            raise KubernetesIdentityChanged("The owned volume UID changed")
        return cast(dict[str, Any], value)

    async def bind_volume(
        self,
        invocation: RunnerInvocation,
        receipt: KubernetesVolumeReceipt,
        guard: Guard,
        *,
        bound: KubernetesBoundVolumeReceipt | None = None,
    ) -> KubernetesBoundVolumeReceipt | None:
        value = await self._current_volume(invocation, receipt, guard)
        if value is None:
            raise KubernetesIdentityChanged("The created claim disappeared before its volume was confirmed")
        phase = value.get("status", {}).get("phase", "Pending")
        if phase == "Pending" and bound is None:
            return None
        if phase != "Bound" or not value["spec"].get("volumeName"):
            raise KubernetesIdentityChanged("The created claim has no confirmed volume binding")
        planned = volume_claim(self.authority, invocation, receipt.purpose)
        if receipt.spec_digest not in {pool_digest(planned["spec"]), pool_digest(value["spec"])}:
            raise KubernetesIdentityChanged("The claim changed beyond its native volume assignment")
        name = value["spec"]["volumeName"]
        response = await self._request("GET", "/api/v1/persistentvolumes/" + name, guard)
        if response.status_code != 200:
            raise KubernetesIdentityChanged("The assigned volume identity cannot be confirmed")
        volume = response.json()
        if not isinstance(volume, dict) or not isinstance(volume.get("spec"), dict):
            raise KubernetesIdentityChanged("The assigned volume has no native specification")
        spec = volume["spec"]
        permitted = {
            "storageClassName",
            "capacity",
            "accessModes",
            "volumeMode",
            "claimRef",
            "csi",
            "nodeAffinity",
            "persistentVolumeReclaimPolicy",
            "mountOptions",
            "volumeAttributesClassName",
        }
        if (
            volume.get("apiVersion") != "v1"
            or volume.get("kind") != "PersistentVolume"
            or not isinstance(volume.get("metadata"), dict)
            or volume["metadata"].get("name") != name
            or volume["metadata"].get("deletionTimestamp") is not None
            or volume.get("status", {}).get("phase") != "Bound"
            or not isinstance(spec.get("csi"), dict)
            or not spec["csi"].get("driver")
            or spec.keys() - permitted
            or spec.get("storageClassName") != planned["spec"]["storageClassName"]
            or spec.get("accessModes") != ["ReadWriteOncePod"]
            or spec.get("volumeMode") != "Filesystem"
            or spec.get("persistentVolumeReclaimPolicy") != "Delete"
            or not isinstance(spec.get("claimRef"), dict)
            or any(
                spec["claimRef"].get(k) != v
                for k, v in {"uid": receipt.uid, "name": receipt.name, "namespace": receipt.namespace}.items()
            )
        ):
            raise KubernetesIdentityChanged("The CSI volume does not belong to this exact native claim")
        current = KubernetesBoundVolumeReceipt(
            claim=receipt,
            claim_spec_digest=pool_digest(value["spec"]),
            volume_name=name,
            volume_uid=volume["metadata"]["uid"],
            volume_spec_digest=pool_digest(spec),
        )
        if bound is not None and current != bound:
            raise KubernetesIdentityChanged("The admitted claim or CSI volume binding changed")
        after = await self._current_volume(invocation, receipt, guard)
        if (
            after is None
            or pool_digest(after["spec"]) != current.claim_spec_digest
            or after.get("status", {}).get("phase") != "Bound"
        ):
            raise KubernetesIdentityChanged("The claim changed during native volume inspection")
        return current

    def _job_path(self, name: str | None = None) -> str:
        policy = self.authority.profile.kubernetes
        assert policy is not None
        return "/apis/batch/v1/namespaces/" + policy.namespace + "/jobs" + ("/" + name if name else "")

    async def create_parked_job(self, invocation: RunnerInvocation, guard: Guard) -> KubernetesJobReceipt:
        return await self._create_job(invocation, command_job(self.authority, invocation), "command", guard)

    async def create_transfer_job(self, invocation: RunnerInvocation, guard: Guard) -> KubernetesJobReceipt:
        return await self._create_job(invocation, transfer_job(self.authority, invocation), "transfer", guard)

    async def _create_job(
        self,
        invocation: RunnerInvocation,
        planned: dict[str, Any],
        phase: Literal["command", "transfer"],
        guard: Guard,
    ) -> KubernetesJobReceipt:
        await self.verify_namespace(guard)
        response = await self._request("POST", self._job_path(), guard, body=planned)
        if response.status_code != 201:
            raise KubernetesEffectUnknown("Kubernetes Job creation requires observation; never recreate blindly")
        try:
            value = response.json()
            if not _contains_plan(value, planned):
                raise ValueError("The admitted Job changed its requested plan")
            return KubernetesJobReceipt(
                phase=phase,
                name=planned["metadata"]["name"],
                namespace=planned["metadata"]["namespace"],
                uid=value["metadata"]["uid"],
                execution_id=invocation.execution_id,
                invocation_digest=pool_digest(invocation.model_dump(mode="json")),
                profile_revision=self.authority.profile.revision,
                spec_digest=pool_digest(value["spec"]),
            )
        except (ValueError, KeyError, TypeError):
            raise KubernetesEffectUnknown("Created Job identity or plan is unconfirmed; preserve custody") from None

    async def observe_job(
        self, invocation: RunnerInvocation, receipt: KubernetesJobReceipt, guard: Guard
    ) -> dict[str, Any] | None:
        policy = self.authority.profile.kubernetes
        assert policy is not None
        self.authority.profile.validate_invocation(invocation)
        if (
            receipt.profile_revision != self.authority.profile.revision
            or receipt.namespace != policy.namespace
            or receipt.execution_id != invocation.execution_id
            or receipt.invocation_digest != pool_digest(invocation.model_dump(mode="json"))
            or receipt.name != resource_name(invocation, receipt.phase)
        ):
            raise KubernetesIdentityChanged("The owned Job belongs to a different profile or namespace")
        await self.verify_namespace(guard)
        response = await self._request("GET", self._job_path(receipt.name), guard)
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise KubernetesIdentityChanged("Job absence or current identity could not be established")
        value = response.json()
        if (
            not isinstance(value, dict)
            or not isinstance(value.get("metadata"), dict)
            or value["metadata"].get("uid") != receipt.uid
            or value["metadata"].get("name") != receipt.name
            or value["metadata"].get("namespace") != receipt.namespace
            or (value["metadata"].get("labels") or {}).get("forge.fireflyframework.org/execution")
            != receipt.execution_id
            or (value["metadata"].get("labels") or {}).get("forge.fireflyframework.org/phase") != receipt.phase
            or (value["metadata"].get("annotations") or {}).get("forge.fireflyframework.org/invocation")
            != receipt.invocation_digest
            or pool_digest(value.get("spec")) != receipt.spec_digest
        ):
            raise KubernetesIdentityChanged("The native Job UID or frozen specification changed")
        return value

    def _pod_path(self, namespace: str, name: str | None = None) -> str:
        return "/api/v1/namespaces/" + namespace + "/pods" + ("/" + name if name else "")

    def _validate_pod(self, value: Any, job: dict[str, Any], receipt: KubernetesJobReceipt) -> dict[str, Any]:
        policy = self.authority.profile.kubernetes
        assert policy is not None
        if not isinstance(value, dict):
            raise KubernetesIdentityChanged("The owned Pod observation is invalid")
        metadata, spec = value.get("metadata"), value.get("spec")
        if not isinstance(metadata, dict) or not isinstance(spec, dict):
            raise KubernetesIdentityChanged("The owned Pod identity is unavailable")
        owners = metadata.get("ownerReferences")
        if (
            metadata.get("namespace") != receipt.namespace
            or metadata.get("deletionTimestamp") is not None
            or not isinstance(owners, list)
            or len(owners) != 1
            or not isinstance(owners[0], dict)
            or owners[0].get("apiVersion") != "batch/v1"
            or owners[0].get("kind") != "Job"
            or owners[0].get("name") != receipt.name
            or owners[0].get("uid") != receipt.uid
            or owners[0].get("controller") is not True
            or ("nodeName" in spec and spec["nodeName"] not in policy.node_names)
            or (metadata.get("labels") or {}).get("forge.fireflyframework.org/execution") != receipt.execution_id
            or (metadata.get("labels") or {}).get("forge.fireflyframework.org/phase") != receipt.phase
            or not _contains_plan(spec, job["spec"]["template"]["spec"])
        ):
            raise KubernetesIdentityChanged("The Pod is not the exact admitted Job on a qualified node")
        return value

    async def bind_pod(
        self, invocation: RunnerInvocation, receipt: KubernetesJobReceipt, guard: Guard
    ) -> KubernetesPodReceipt | None:
        job = await self.observe_job(invocation, receipt, guard)
        if job is None:
            raise KubernetesIdentityChanged("The owned Job disappeared before Pod activation")
        response = await self._request(
            "GET",
            self._pod_path(receipt.namespace)
            + "?limit=2&labelSelector=forge.fireflyframework.org%2Fexecution%3D"
            + receipt.execution_id,
            guard,
        )
        if response.status_code != 200:
            raise KubernetesIdentityChanged("The owned Pod selection cannot be established")
        value = response.json()
        if not isinstance(value, dict) or not isinstance(value.get("items"), list):
            raise KubernetesIdentityChanged("The owned Pod selection is invalid")
        items = value["items"]
        if (value.get("metadata") or {}).get("continue") or len(items) > 1:
            raise KubernetesIdentityChanged("Multiple Pods cannot share one native activation")
        if not items:
            return None
        pod = self._validate_pod(items[0], job, receipt)
        if "nodeName" not in pod["spec"]:
            return None
        try:
            return KubernetesPodReceipt(
                name=pod["metadata"]["name"],
                namespace=receipt.namespace,
                uid=pod["metadata"]["uid"],
                job_uid=receipt.uid,
                execution_id=receipt.execution_id,
                invocation_digest=receipt.invocation_digest,
                profile_revision=receipt.profile_revision,
                spec_digest=pool_digest(pod["spec"]),
                node_name=pod["spec"]["nodeName"],
            )
        except (KeyError, TypeError, ValueError):
            raise KubernetesIdentityChanged("The owned Pod identity is invalid") from None

    async def observe_pod(
        self,
        invocation: RunnerInvocation,
        job_receipt: KubernetesJobReceipt,
        receipt: KubernetesPodReceipt,
        guard: Guard,
    ) -> dict[str, Any]:
        job = await self.observe_job(invocation, job_receipt, guard)
        if job is None or (
            receipt.job_uid,
            receipt.execution_id,
            receipt.invocation_digest,
            receipt.profile_revision,
            receipt.namespace,
        ) != (
            job_receipt.uid,
            job_receipt.execution_id,
            job_receipt.invocation_digest,
            job_receipt.profile_revision,
            job_receipt.namespace,
        ):
            raise KubernetesIdentityChanged("The Pod does not belong to this native invocation")
        response = await self._request("GET", self._pod_path(receipt.namespace, receipt.name), guard)
        if response.status_code != 200:
            raise KubernetesIdentityChanged("The activated Pod identity is unavailable; never activate a replacement")
        pod = self._validate_pod(response.json(), job, job_receipt)
        if (
            pod["metadata"].get("uid") != receipt.uid
            or pod["metadata"].get("name") != receipt.name
            or pod["spec"].get("nodeName") != receipt.node_name
            or pool_digest(pod["spec"]) != receipt.spec_digest
        ):
            raise KubernetesIdentityChanged("The activated Pod UID or frozen specification changed")
        response = await self._request(
            "GET",
            self._pod_path(receipt.namespace)
            + "?limit=2&labelSelector=forge.fireflyframework.org%2Fexecution%3D"
            + receipt.execution_id,
            guard,
        )
        if response.status_code != 200:
            raise KubernetesIdentityChanged("The activated Pod selection is unavailable")
        selection = response.json()
        items = selection.get("items")
        if (
            not isinstance(items, list)
            or len(items) != 1
            or selection.get("metadata", {}).get("continue")
            or items[0].get("metadata", {}).get("uid") != receipt.uid
        ):
            raise KubernetesIdentityChanged("The native execution no longer has exactly one activated Pod")
        return pod

    async def delete_job(self, invocation: RunnerInvocation, receipt: KubernetesJobReceipt, guard: Guard) -> None:
        if await self.observe_job(invocation, receipt, guard) is None:
            return
        response = await self._request(
            "DELETE",
            self._job_path(receipt.name),
            guard,
            body={
                "apiVersion": "v1",
                "kind": "DeleteOptions",
                "preconditions": {"uid": receipt.uid},
                "propagationPolicy": "Foreground",
            },
        )
        if response.status_code not in {200, 202}:
            raise KubernetesEffectUnknown("Owned Job deletion remains unconfirmed; preserve its reservation")

    async def collect_logs(
        self,
        invocation: RunnerInvocation,
        job: KubernetesJobReceipt,
        pod: KubernetesPodReceipt,
        sink: Callable[[bytes], Awaitable[None]],
        guard: Guard,
    ) -> None:
        await self.observe_pod(invocation, job, pod, guard)
        self.authority.verify()
        await guard()
        size = 0
        try:
            async with self.client.stream(
                "GET",
                self.authority.api_origin
                + self._pod_path(pod.namespace, pod.name)
                + "/log?container=command&follow=false&timestamps=false",
                headers={"Authorization": "Bearer " + self.authority.token.get_secret_value()},
                follow_redirects=False,
            ) as response:
                if response.status_code != 200:
                    raise KubernetesIdentityChanged("Owned command output is unavailable")
                async for part in response.aiter_bytes(65536):
                    await guard()
                    self.authority.verify()
                    size += len(part)
                    if size > invocation.max_output_bytes:
                        raise KubernetesIdentityChanged("Owned command output exceeds its admitted bound")
                    await sink(part)
                    await guard()
        except httpx.HTTPError:
            raise KubernetesIdentityChanged("Owned command output remains unconfirmed") from None
        await self.observe_pod(invocation, job, pod, guard)

    async def wait_terminated(
        self,
        invocation: RunnerInvocation,
        job: KubernetesJobReceipt,
        pod: KubernetesPodReceipt,
        guard: Guard,
    ) -> int:
        async with asyncio.timeout(self.authority.profile.admission_seconds):
            while True:
                value = await self.observe_pod(invocation, job, pod, guard)
                statuses = value.get("status", {}).get("containerStatuses", [])
                if len(statuses) == 1 and statuses[0].get("name") == job.phase:
                    terminated = statuses[0].get("state", {}).get("terminated")
                    if isinstance(terminated, dict) and type(terminated.get("exitCode")) is int:
                        return int(terminated["exitCode"])
                await asyncio.sleep(0.1)

    async def remove_drained_job(
        self,
        invocation: RunnerInvocation,
        job: KubernetesJobReceipt,
        pod: KubernetesPodReceipt,
        guard: Guard,
    ) -> None:
        # The lifecycle calls this only after retained PID1 drain or exact transfer termination.
        await self.observe_pod(invocation, job, pod, guard)
        await self.delete_job(invocation, job, guard)
        await self.confirm_removed_job(invocation, job, pod, guard)

    async def confirm_removed_job(
        self,
        invocation: RunnerInvocation,
        job: KubernetesJobReceipt,
        pod: KubernetesPodReceipt,
        guard: Guard,
    ) -> None:
        async with asyncio.timeout(self.authority.profile.admission_seconds):
            while True:
                current = await self.observe_job(invocation, job, guard)
                response = await self._request("GET", self._pod_path(pod.namespace, pod.name), guard)
                if response.status_code == 200 and response.json().get("metadata", {}).get("uid") != pod.uid:
                    raise KubernetesIdentityChanged("A replacement Pod cannot complete owned cleanup")
                if response.status_code not in {200, 404}:
                    raise KubernetesIdentityChanged("Owned Pod removal is unconfirmed")
                selected = await self._request(
                    "GET",
                    self._pod_path(pod.namespace)
                    + "?limit=2&labelSelector=forge.fireflyframework.org%2Fexecution%3D"
                    + invocation.execution_id,
                    guard,
                )
                if selected.status_code != 200:
                    raise KubernetesIdentityChanged("Owned Pod drain selection is unavailable")
                listing = selected.json()
                if (
                    not isinstance(listing.get("items"), list)
                    or listing.get("metadata", {}).get("continue")
                    or any(item.get("metadata", {}).get("uid") != pod.uid for item in listing["items"])
                ):
                    raise KubernetesIdentityChanged("An additional Pod retains the execution identity")
                if current is None and response.status_code == 404 and listing["items"] == []:
                    return
                await asyncio.sleep(0.1)

    async def remove_volume(
        self,
        invocation: RunnerInvocation,
        volume: KubernetesBoundVolumeReceipt,
        guard: Guard,
    ) -> None:
        await self.bind_volume(invocation, volume.claim, guard, bound=volume)
        response = await self._request(
            "DELETE",
            self._volume_path(volume.claim.name),
            guard,
            body={
                "apiVersion": "v1",
                "kind": "DeleteOptions",
                "preconditions": {"uid": volume.claim.uid},
            },
        )
        if response.status_code not in {200, 202}:
            raise KubernetesEffectUnknown("Owned claim deletion remains unconfirmed")
        await self.confirm_removed_volume(volume, guard)

    async def confirm_removed_volume(self, volume: KubernetesBoundVolumeReceipt, guard: Guard) -> None:
        async with asyncio.timeout(self.authority.profile.admission_seconds):
            while True:
                pvc = await self._request("GET", self._volume_path(volume.claim.name), guard)
                pv = await self._request("GET", "/api/v1/persistentvolumes/" + volume.volume_name, guard)
                for result, uid in ((pvc, volume.claim.uid), (pv, volume.volume_uid)):
                    if result.status_code not in {200, 404} or (
                        result.status_code == 200 and result.json().get("metadata", {}).get("uid") != uid
                    ):
                        raise KubernetesIdentityChanged("Owned volume removal or its retained identity is unconfirmed")
                if pvc.status_code == pv.status_code == 404:
                    return
                await asyncio.sleep(0.1)


def _contains_plan(current: Any, planned: Any) -> bool:
    if isinstance(planned, dict):
        if not isinstance(current, dict):
            return False
        if "image" in planned and "command" in planned:
            defaults = {"terminationMessagePath": "/dev/termination-log", "terminationMessagePolicy": "File"}
            if any(
                key not in planned and (key not in defaults or value != defaults[key]) for key, value in current.items()
            ):
                return False
        if "containers" in planned and any(
            key in current and key not in planned for key in ("initContainers", "ephemeralContainers", "hostAliases")
        ):
            return False
        return all(
            _omitted_false(planned, key)
            if key not in current
            else (
                _exact_value(current[key], value)
                if key in {"securityContext", "env", "volumeMounts", "volumes"}
                else _contains_plan(current[key], value)
            )
            for key, value in planned.items()
        )
    if isinstance(planned, list):
        return (
            isinstance(current, list)
            and len(current) == len(planned)
            and all(_contains_plan(left, right) for left, right in zip(current, planned, strict=True))
        )
    return type(current) is type(planned) and bool(current == planned)


def _exact_value(current: Any, planned: Any) -> bool:
    if type(current) is not type(planned):
        return False
    if isinstance(planned, dict):
        return not current.keys() - planned.keys() and all(
            _omitted_false(planned, key) if key not in current else _exact_value(current[key], value)
            for key, value in planned.items()
        )
    if isinstance(planned, list):
        return len(current) == len(planned) and all(
            _exact_value(left, right) for left, right in zip(current, planned, strict=True)
        )
    return bool(current == planned)


def _omitted_false(planned: dict[str, Any], key: str) -> bool:
    if planned[key] is not False:
        return False
    if "containers" in planned:
        return key in {"hostNetwork", "hostPID", "hostIPC", "shareProcessNamespace"}
    return key == "readOnly" and ("mountPath" in planned or "claimName" in planned)
