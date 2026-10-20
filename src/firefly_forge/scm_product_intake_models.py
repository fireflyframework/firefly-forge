# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Explicit first-product intake, separate from existing SCM delegation grants."""

from __future__ import annotations

import json
from typing import Literal

from pydantic import Field, model_validator

from firefly_forge.collaboration import ProductDefinition
from firefly_forge.models import Backend, Frontend
from firefly_forge.project_models import ProjectComponent
from firefly_forge.scm_authority_models import Digest, NativeID, StrictContract


class IntakeComponent(StrictContract):
    id: str = Field(pattern=r"^[a-z][a-z0-9-]{0,63}$")
    name: str = Field(min_length=1, max_length=120)
    responsibility: str = Field(min_length=1, max_length=2000)
    path: str = "."
    kind: Literal["api", "web", "worker", "library", "fullstack"] = "api"
    backend: Backend
    frontend: Frontend

    @model_validator(mode="after")
    def component_contract(self) -> IntakeComponent:
        ProjectComponent.model_validate({**self.model_dump(), "repository_id": "0" * 32})
        return self


class SCMProductIntakeCommandV1(StrictContract):
    version: Literal[1]
    action: Literal["intake"]
    request_id: NativeID
    expected_consent_request_id: NativeID
    expected_consent_digest: Digest
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(min_length=1, max_length=4000)
    component: IntakeComponent
    definition: ProductDefinition

    @model_validator(mode="after")
    def first_definition(self) -> SCMProductIntakeCommandV1:
        definition = self.definition
        if definition.functional_reviewed or any(
            value is not None for value in (definition.ai_origin, definition.architecture, definition.design_review)
        ):
            raise ValueError("Intake retains an unreviewed manual definition without model or review claims")
        for feature in definition.features:
            if set(feature.component_refs) - {self.component.id}:
                raise ValueError("Intake feature references must name its one explicit component")
        for integration in definition.integrations:
            if integration.kind == "owned" and integration.service_ref != self.component.id:
                raise ValueError("Owned intake integration references must name its one explicit component")
        if len(json.dumps(self.model_dump(mode="json"), ensure_ascii=False).encode()) > 80000:
            raise ValueError("Intake cannot exceed 80,000 UTF-8 bytes")
        return self


class SCMProductIntakeConsentInputV1(StrictContract):
    version: Literal[1]
    request_id: NativeID
    expected_previous_digest: Digest | None
    verified_receipt_id: NativeID
    expected_verified_reference_digest: Digest
    active: bool
    acknowledge_product_intake: Literal[True]

    @model_validator(mode="before")
    @classmethod
    def exact_acknowledgment(cls, value: object) -> object:
        if isinstance(value, dict) and value.get("acknowledge_product_intake") is not True:
            raise ValueError("Explicitly opt in to first-project product intake")
        return value
