# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Explicit local-maintenance path plans; these contracts grant no execution."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from firefly_forge.journal import digest
from firefly_forge.scm_authority_models import Digest, NativeID, StrictContract


def plain_location(value: str) -> str:
    path = Path(value)
    if (
        not path.is_absolute()
        or value != os.path.normpath(value)
        or value != value.strip()
        or value.startswith("//")
        or path.parent == path
        or any(part in {".", ".."} for part in value.split("/"))
        or any(character in value for character in "\x00\r\n")
    ):
        raise ValueError("Native locations require exact non-root absolute paths")
    return value


class LocationPairV1(StrictContract):
    source: str
    target: str

    @field_validator("source", "target")
    @classmethod
    def plain_path(cls, value: str) -> str:
        return plain_location(value)

    @model_validator(mode="after")
    def disjoint(self) -> LocationPairV1:
        source, target = Path(self.source), Path(self.target)
        if source.is_relative_to(target) or target.is_relative_to(source):
            raise ValueError("Relocation roots must be distinct and non-overlapping")
        return self


class WorkspaceLocationV1(LocationPairV1):
    run_id: NativeID


class VerificationRuntimeLocationV1(LocationPairV1):
    run_id: NativeID
    mode: Literal["byte_identical", "verification_runtime_baseline"]
    source_content_digest: Digest | None
    target_content_digest: Digest
    approved_baseline_sha256: Digest | None

    @model_validator(mode="after")
    def explicit_baseline(self) -> VerificationRuntimeLocationV1:
        if self.mode == "byte_identical":
            if self.source_content_digest != self.target_content_digest or self.approved_baseline_sha256 is not None:
                raise ValueError("Byte-identical relocation cannot replace its runtime contents")
        elif self.approved_baseline_sha256 is None:
            raise ValueError("A new verification runtime requires its explicit reviewed baseline")
        return self


class NativeLocationPlanV1(StrictContract):
    version: Literal[1]
    source_storage: str
    target_storage: str
    workspaces: list[WorkspaceLocationV1]
    verification_runtimes: list[VerificationRuntimeLocationV1]

    @field_validator("version", mode="before")
    @classmethod
    def exact_version(cls, value: Any) -> Any:
        if type(value) is not int:
            raise ValueError("Native location version requires an exact integer")
        return value

    @field_validator("source_storage", "target_storage")
    @classmethod
    def plain_path(cls, value: str) -> str:
        return plain_location(value)

    @model_validator(mode="after")
    def exact_namespace(self) -> NativeLocationPlanV1:
        LocationPairV1(source=self.source_storage, target=self.target_storage)
        runs = [workspace.run_id for workspace in self.workspaces]
        if len(runs) != len(set(runs)):
            raise ValueError("Each workspace requires one exact native run")
        for workspace in self.workspaces:
            if (
                Path(workspace.source) != Path(self.source_storage) / "workspaces" / workspace.run_id
                or Path(workspace.target) != Path(self.target_storage) / "workspaces" / workspace.run_id
            ):
                raise ValueError("Workspace relocation must preserve its original native relative namespace")
        runtime_keys = [(runtime.run_id, runtime.source) for runtime in self.verification_runtimes]
        if len(runtime_keys) != len(set(runtime_keys)) or any(
            runtime.run_id not in runs for runtime in self.verification_runtimes
        ):
            raise ValueError("Verification runtime locations require unique selected native workspace bindings")
        return self


class NativeLocationAcknowledgmentV1(StrictContract):
    version: Literal[1]
    request_id: NativeID
    candidate_digest: Digest
    acknowledge_stopped_runtime: Literal[True]
    acknowledge_history_preserved: Literal[True]
    acknowledge_no_execution_grant: Literal[True]

    @field_validator("version", mode="before")
    @classmethod
    def exact_version(cls, value: Any) -> Any:
        if type(value) is not int:
            raise ValueError("Native location version requires an exact integer")
        return value

    @field_validator(
        "acknowledge_stopped_runtime", "acknowledge_history_preserved", "acknowledge_no_execution_grant", mode="before"
    )
    @classmethod
    def exact_acknowledgment(cls, value: Any) -> Any:
        if value is not True:
            raise ValueError("Offline maintenance requires explicit exact acknowledgments")
        return value


class NativeLocationCandidateV1(StrictContract):
    version: Literal[1]
    kind: Literal["native_location_candidate"]
    plan: NativeLocationPlanV1
    postgres_data_directory: str
    owner_uid: int = Field(ge=0, strict=True)
    base_topology_digest: Digest
    database_fingerprint: Digest
    storage_identity_sha256: Digest
    previous_receipt_digest: Digest | None
    preserved_native_digest: Digest
    filesystem_proof_digest: Digest
    execution_authorized: Literal[False]
    digest: Digest

    @field_validator("postgres_data_directory")
    @classmethod
    def postgres_path(cls, value: str) -> str:
        return plain_location(value)

    @field_validator("execution_authorized", mode="before")
    @classmethod
    def maintenance_only(cls, value: Any) -> Any:
        if value is not False:
            raise ValueError("Location maintenance cannot grant execution")
        return value

    @model_validator(mode="after")
    def exact_digest(self) -> NativeLocationCandidateV1:
        if digest(self.model_dump(mode="json", exclude={"digest"})) != self.digest:
            raise ValueError("Native location candidate differs from its exact reviewed input")
        return self


class NativeLocationEventV1(StrictContract):
    version: Literal[1]
    kind: Literal["native_location_intent", "native_location_receipt"]
    sequence: int = Field(ge=1, strict=True)
    request: NativeLocationAcknowledgmentV1
    candidate: NativeLocationCandidateV1
    previous_hash: Digest | None
    intent_digest: Digest | None
    digest: Digest
    signature: Digest


class NativeLocationFileV1(StrictContract):
    path: str
    sha256: Digest | None

    @field_validator("path")
    @classmethod
    def relative_name(cls, value: str) -> str:
        path = Path(value)
        if (
            path.is_absolute()
            or not value
            or value != path.as_posix()
            or any(part in {".", "..", ""} for part in value.split("/"))
            or any(character in value for character in "\x00\r\n")
        ):
            raise ValueError("Retained file declarations require exact contained relative names")
        return value


class NativeLocationFilesV1(StrictContract):
    version: Literal[1]
    required_files: list[NativeLocationFileV1]
    unavailable_files: list[NativeLocationFileV1]

    @model_validator(mode="after")
    def complete_required_inputs(self) -> NativeLocationFilesV1:
        required = {value.path for value in self.required_files}
        unavailable = {value.path for value in self.unavailable_files}
        if (
            len(required) != len(self.required_files)
            or len(unavailable) != len(self.unavailable_files)
            or required & unavailable
            or any(value.sha256 is None for value in self.required_files)
            or not {
                "runtime-storage.identity",
                "configuration/providers.json",
                "configuration/source-budget-policy.json",
            }.issubset(required)
        ):
            raise ValueError("Selected native relocation inputs must be complete and disjoint from unavailable history")
        return self


class NativeLocationProjectionRecordV1(StrictContract):
    version: Literal[1]
    receipt_digest: Digest
    events: list[NativeLocationEventV1] = Field(min_length=2)

    @model_validator(mode="after")
    def complete_projection(self) -> NativeLocationProjectionRecordV1:
        previous = None
        pending = None
        receipt = None
        for sequence, event in enumerate(self.events, 1):
            raw = event.model_dump(mode="json")
            if (
                event.sequence != sequence
                or event.previous_hash != previous
                or digest({key: value for key, value in raw.items() if key not in {"digest", "signature"}})
                != event.digest
            ):
                raise ValueError("Location projection requires its unchanged complete native event chain")
            if event.kind == "native_location_intent":
                if (
                    pending is not None
                    or event.intent_digest is not None
                    or event.candidate.previous_receipt_digest != receipt
                ):
                    raise ValueError("Location projection cannot omit or fork a maintenance transfer")
                pending = event
            else:
                if (
                    pending is None
                    or event.intent_digest != pending.digest
                    or event.request != pending.request
                    or event.candidate != pending.candidate
                ):
                    raise ValueError("Location projection requires each exact completed transfer pair")
                pending, receipt = None, event.digest
            previous = event.digest
        if pending is not None or receipt != self.receipt_digest:
            raise ValueError("Location projection has an unknown or superseded outcome")
        return self
