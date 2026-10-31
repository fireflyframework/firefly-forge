# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Immutable namespace and isolation selection; credentials remain controller-owned."""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


def cpu_quantity(value: float) -> str:
    amount = Decimal(str(value))
    if not amount.is_finite() or not 0 < amount <= 2**63 - 1 or amount * 1000 != (amount * 1000).to_integral_value():
        raise ValueError("Kubernetes CPU must be a positive exact supported number of millicores")
    units = int(amount * 1000)
    suffixes = ("m", "", "k", "M", "G", "T", "P", "E")
    index = 0
    while index < len(suffixes) - 1 and units % 1000 == 0:
        units //= 1000
        index += 1
    return str(units) + suffixes[index]


def memory_quantity(value: int) -> str:
    if type(value) is not int or not 0 < value * 1048576 <= 2**63 - 1:
        raise ValueError("Kubernetes memory must be a positive exact supported binary capacity")
    suffixes = ("Mi", "Gi", "Ti", "Pi", "Ei")
    index = 0
    while index < len(suffixes) - 1 and value % 1024 == 0:
        value //= 1024
        index += 1
    return str(value) + suffixes[index]


def kubernetes_name(value: str) -> str:
    if len(value) > 253 or any(
        not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in value.split(".")
    ):
        raise ValueError("An exact Kubernetes DNS resource name is required")
    return value


class KubernetesPoolPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    namespace: str = Field(max_length=63)
    namespace_uid: UUID
    service_account: str
    storage_class: str
    runtime_class: str
    node_names: tuple[str, ...] = Field(min_length=1)
    network_policy_name: str
    network_policy_uid: UUID
    uid: int = Field(gt=0, strict=True)
    gid: int = Field(gt=0, strict=True)
    workspace_storage_mb: int = Field(gt=0, strict=True)

    _names = field_validator("namespace", "service_account", "storage_class", "runtime_class", "network_policy_name")(
        kubernetes_name
    )

    @field_validator("namespace")
    @classmethod
    def namespace_label(cls, value: str) -> str:
        if "." in value:
            raise ValueError("A Kubernetes namespace must be one DNS label")
        return value

    @field_validator("node_names")
    @classmethod
    def exact_nodes(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        for value in values:
            kubernetes_name(value)
        if len(set(values)) != len(values):
            raise ValueError("The qualified node selection contains duplicates")
        return values


class KubernetesJobReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    phase: Literal["command", "transfer"] = "command"

    name: str
    namespace: str
    uid: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,256}$")
    execution_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    invocation_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    profile_revision: str = Field(pattern=r"^[a-f0-9]{64}$")
    spec_digest: str = Field(pattern=r"^[a-f0-9]{64}$")

    _names = field_validator("name", "namespace")(kubernetes_name)


class KubernetesVolumeReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    purpose: Literal["input", "workspace"]
    name: str
    namespace: str
    uid: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,256}$")
    execution_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    invocation_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    profile_revision: str = Field(pattern=r"^[a-f0-9]{64}$")
    spec_digest: str = Field(pattern=r"^[a-f0-9]{64}$")

    _names = field_validator("name", "namespace")(kubernetes_name)


class KubernetesBoundVolumeReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    claim: KubernetesVolumeReceipt
    claim_spec_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    volume_name: str
    volume_uid: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,256}$")
    volume_spec_digest: str = Field(pattern=r"^[a-f0-9]{64}$")

    _name = field_validator("volume_name")(kubernetes_name)


class KubernetesPodReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    namespace: str
    uid: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,256}$")
    job_uid: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,256}$")
    execution_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    invocation_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    profile_revision: str = Field(pattern=r"^[a-f0-9]{64}$")
    spec_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    node_name: str

    _names = field_validator("name", "namespace", "node_name")(kubernetes_name)
