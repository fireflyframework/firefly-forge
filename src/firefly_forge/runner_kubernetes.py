# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Explicit Kubernetes control identity and parked, credential-free command plans."""

from __future__ import annotations

import os
import ssl
import stat
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from pydantic import SecretStr
from pyfly.container import service

from firefly_forge.runner_kubernetes_models import (
    KubernetesJobReceipt,
    KubernetesPodReceipt,
    cpu_quantity,
    memory_quantity,
)
from firefly_forge.runner_pool_launcher import LAUNCHER, owned_launcher
from firefly_forge.runner_pool_models import RunnerInvocation, RunnerPoolProfile, pool_digest
from firefly_forge.scm_authority_models import exact_origin

Guard = Callable[[], Awaitable[None]]


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
    if kind not in {"command", "input", "workspace"}:
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
                        "seccompProfile": {"type": "RuntimeDefault"},
                    },
                    "containers": [
                        {
                            "name": "command",
                            "image": invocation.image,
                            "imagePullPolicy": "IfNotPresent",
                            "command": [
                                "/app/.venv/bin/python",
                                "-I",
                                "-c",
                                owned_launcher() if invocation.work_duration is not None else LAUNCHER,
                            ],
                            "securityContext": security,
                            "env": [
                                {"name": "FORGE_POD_UID", "valueFrom": {"fieldRef": {"fieldPath": "metadata.uid"}}}
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
                                {"name": "input", "mountPath": "/forge-input", "readOnly": True},
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

    def _job_path(self, name: str | None = None) -> str:
        policy = self.authority.profile.kubernetes
        assert policy is not None
        return "/apis/batch/v1/namespaces/" + policy.namespace + "/jobs" + ("/" + name if name else "")

    async def create_parked_job(self, invocation: RunnerInvocation, guard: Guard) -> KubernetesJobReceipt:
        planned = command_job(self.authority, invocation)
        await self.verify_namespace(guard)
        response = await self._request("POST", self._job_path(), guard, body=planned)
        if response.status_code != 201:
            raise KubernetesEffectUnknown("Kubernetes Job creation requires observation; never recreate blindly")
        try:
            value = response.json()
            if not _contains_plan(value, planned):
                raise ValueError("The admitted Job changed its requested plan")
            return KubernetesJobReceipt(
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
            or receipt.name != resource_name(invocation, "command")
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
            or spec.get("nodeName") not in policy.node_names
            or (metadata.get("labels") or {}).get("forge.fireflyframework.org/execution") != receipt.execution_id
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
        return isinstance(current, dict) and all(
            key in current
            and (
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
        return current.keys() == planned.keys() and all(
            _exact_value(current[key], value) for key, value in planned.items()
        )
    if isinstance(planned, list):
        return len(current) == len(planned) and all(
            _exact_value(left, right) for left, right in zip(current, planned, strict=True)
        )
    return bool(current == planned)
