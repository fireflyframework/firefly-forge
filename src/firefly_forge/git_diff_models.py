# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Elastic storage budgets and small, host-path-free Git diff references."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]


class DiffContract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True)


class GitDiffBudget(DiffContract):
    max_bytes: int | None = Field(default=None, ge=1)
    min_free_bytes: int = Field(default=1_073_741_824, ge=0)
    timeout_seconds: int = Field(default=120, ge=1)
    chunk_bytes: int = Field(default=65536, ge=1024, le=1_048_576)
    index_page_files: int = Field(default=100, ge=1, le=1000)
    preview_bytes: int = Field(default=65536, ge=0, le=1_048_576)


class GitDiffRefV1(DiffContract):
    version: Literal[1] = 1
    artifact_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    manifest_sha256: Digest
    sha256: Digest
    byte_count: int = Field(ge=0)
    file_count: int = Field(ge=0)
    index_page_count: int = Field(ge=0)


class GitDiffFileV1(DiffContract):
    file_index: int = Field(ge=0)
    path: str = Field(min_length=1)
    offset: int = Field(ge=0)
    byte_count: int = Field(ge=1)
    sha256: Digest


class GitDiffIndexPageV1(DiffContract):
    version: Literal[1] = 1
    artifact_id: str
    manifest_sha256: Digest
    page: int = Field(ge=0)
    next_page: int | None
    files: list[GitDiffFileV1]


class GitDiffPageV1(DiffContract):
    version: Literal[1] = 1
    artifact_id: str
    manifest_sha256: Digest
    file_index: int | None = None
    offset: int = Field(ge=0)
    byte_count: int = Field(ge=0, le=1_048_576)
    total_bytes: int = Field(ge=0)
    next_offset: int | None
    content_base64: str
    content_sha256: Digest
    starts_mid_line: bool
    ends_mid_line: bool
    utf8_complete: bool
