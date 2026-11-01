# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Append-only credential evidence for an unchanged retained source publication."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from firefly_forge.runner_pool_models import pool_digest


class SourceCredentialScanInputV1(BaseModel):
    model_config = ConfigDict(
        extra="forbid", strict=True, frozen=True, hide_input_in_errors=True, revalidate_instances="always"
    )
    version: Literal[1] = 1
    expected_manifest: str = Field(pattern=r"^[a-f0-9]{64}$")
    expected_credential_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    acknowledge_retained_bytes_only: Literal[True]
    acknowledge_no_source_change: Literal[True]

    @field_validator("acknowledge_retained_bytes_only", "acknowledge_no_source_change", mode="before")
    @classmethod
    def explicit(cls, value: Any) -> bool:
        if value is not True:
            raise ValueError("Explicit retained-byte scan acknowledgments must be true")
        return True

    @field_validator("version", mode="before")
    @classmethod
    def exact_version(cls, value: Any) -> int:
        if type(value) is not int or value != 1:
            raise ValueError("Use the exact retained-byte scan version")
        return value


class SourceCredentialScanReceiptV1(BaseModel):
    model_config = ConfigDict(
        extra="forbid", strict=True, frozen=True, hide_input_in_errors=True, revalidate_instances="always"
    )
    protocol: Literal["forge-source-credential-scan-v1"]
    request_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    snapshot_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    tenant_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    project_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    run_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    actor_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    reference_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    source_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    storage_binding: str = Field(pattern=r"^[a-f0-9]{64}$")
    credential_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    request_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    content_chain_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    file_count: int = Field(ge=0)
    byte_count: int = Field(ge=0)
    no_execution_grant: Literal[True]
    digest: str = Field(pattern=r"^[a-f0-9]{64}$")

    @field_validator("no_execution_grant", mode="before")
    @classmethod
    def no_grant(cls, value: Any) -> bool:
        if value is not True:
            raise ValueError("A retained-byte scan cannot grant execution")
        return True

    @model_validator(mode="after")
    def integrity(self) -> SourceCredentialScanReceiptV1:
        if pool_digest(self.model_dump(mode="json", exclude={"digest"})) != self.digest:
            raise ValueError("Retained-byte credential scan receipt changed")
        return self


class SourceCredentialScanViewV1(BaseModel):
    model_config = ConfigDict(
        extra="forbid", strict=True, frozen=True, hide_input_in_errors=True, revalidate_instances="always"
    )
    status: Literal["pending", "scanned"]
    request_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    snapshot_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    run_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    request_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    receipt: SourceCredentialScanReceiptV1 | None

    @model_validator(mode="after")
    def exact_receipt(self) -> SourceCredentialScanViewV1:
        if (self.status == "pending") != (self.receipt is None):
            raise ValueError("Credential scan state differs from its exact receipt")
        if self.receipt is not None and (
            self.receipt.request_id != self.request_id
            or self.receipt.snapshot_id != self.snapshot_id
            or self.receipt.run_id != self.run_id
            or self.receipt.request_sha256 != self.request_sha256
        ):
            raise ValueError("Credential scan view differs from its retained receipt")
        return self
