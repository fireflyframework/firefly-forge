# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Frozen project transfer identities, independent of archive implementation."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ProjectBundleIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    protocol: Literal["forge-project-bundle-v1"] = "forge-project-bundle-v1"
    archive_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    archive_bytes: int = Field(ge=0, le=269_484_032, strict=True)
    source_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    source_archive_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    source_archive_bytes: int = Field(ge=0, le=268_435_456, strict=True)
    git_digest: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    git_archive_digest: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    git_archive_bytes: int = Field(default=0, ge=0, le=268_435_456, strict=True)
