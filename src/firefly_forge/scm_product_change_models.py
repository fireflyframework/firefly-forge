# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Explicit whole-product SCM editing, separate from reply and resume capabilities."""

from __future__ import annotations

import json
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator

from firefly_forge.product_lifecycle import FeatureChangeInput
from firefly_forge.scm_authority_models import Digest, NativeID, StrictContract, VerifiedSCMReferenceV1


class SCMProductChangeGrantInputV1(StrictContract):
    version: Literal[1]
    request_id: NativeID
    expected_previous_digest: Digest | None
    expected_project_binding_digest: Digest
    expected_binding_request_id: NativeID
    expected_binding_digest: Digest
    component_ids: list[Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{0,63}$")]] = Field(min_length=1, max_length=50)
    allowed_actions: list[Literal["product_change"]] = Field(min_length=1, max_length=1)
    active: bool
    acknowledge_product_definition_edit: Literal[True]

    @field_validator("acknowledge_product_definition_edit", mode="before")
    @classmethod
    def explicit_edit(cls, value: object) -> object:
        if value is not True:
            raise ValueError("Explicitly consent to editing the whole functional product definition")
        return value

    @model_validator(mode="after")
    def unique_scope(self) -> SCMProductChangeGrantInputV1:
        if len(set(self.component_ids)) != len(self.component_ids):
            raise ValueError("Whole-product component scope must be unique")
        return self


class SCMProductChangeCommandV1(StrictContract):
    version: Literal[1]
    action: Literal["product_change"]
    project_id: NativeID
    expected_project_binding_digest: Digest
    expected_grant_request_id: NativeID
    expected_grant_digest: Digest
    change: FeatureChangeInput

    @field_validator("change", mode="before")
    @classmethod
    def strict_change(cls, value: object) -> FeatureChangeInput:
        if isinstance(value, FeatureChangeInput):
            value = value.model_dump(mode="json", warnings=False)
        return FeatureChangeInput.model_validate(value, strict=True)

    @model_validator(mode="after")
    def utf8_budget(self) -> SCMProductChangeCommandV1:
        if len(json.dumps(self.change.model_dump(mode="json"), ensure_ascii=False).encode()) > 80000:
            raise ValueError("An SCM product change cannot exceed 80,000 UTF-8 bytes")
        return self


class SCMProductChangeGrantReceiptV1(StrictContract):
    version: Literal[1]
    event_id: NativeID
    project_id: NativeID
    repository_id: NativeID
    binding_key: Digest
    native_user_id: NativeID
    revision: int = Field(ge=1, le=50)
    previous_digest: Digest | None
    prepared_at: str = Field(min_length=1, max_length=50)
    administering_user_id: NativeID
    request: SCMProductChangeGrantInputV1
    request_digest: Digest
    digest: Digest


class SCMProductChangeReceiptV1(StrictContract):
    version: Literal[1]
    event_id: NativeID
    project_id: NativeID
    repository_id: NativeID
    native_user_id: NativeID
    authentication_method: Literal["scm_mapping"]
    binding_request_id: NativeID
    binding_digest: Digest
    grant_request_id: NativeID
    grant_digest: Digest
    verified_receipt_id: NativeID
    verified_reference: VerifiedSCMReferenceV1
    request: SCMProductChangeCommandV1
    request_digest: Digest
    product_revision: int = Field(ge=1)
    product_digest: Digest
    prepared_at: str = Field(min_length=1, max_length=50)
    digest: Digest
