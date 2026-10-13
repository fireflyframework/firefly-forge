# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Explicit journey-to-feature behavior contracts for architecture, implementation and QA."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

if TYPE_CHECKING:
    from firefly_forge.collaboration import ProductDefinition


class ExpectedBehavior(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    id: str = Field(pattern=r"^[a-z][a-z0-9-]{0,63}$")
    given: str = Field(min_length=1, max_length=2000)
    when: str = Field(min_length=1, max_length=2000)
    then: str = Field(min_length=1, max_length=2000)
    failure_paths: list[str] = Field(min_length=1, max_length=20)

    @field_validator("failure_paths")
    @classmethod
    def bounded_failures(cls, values: list[str]) -> list[str]:
        if any(not value.strip() or len(value) > 2000 for value in values):
            raise ValueError("Failure paths require bounded nonempty expected behavior")
        return [value.strip() for value in values]


class FunctionalFeature(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    id: str = Field(pattern=r"^[a-z][a-z0-9-]{0,63}$")
    name: str = Field(min_length=1, max_length=200)
    journey_refs: list[str] = Field(
        min_length=1,
        max_length=50,
        description="Exact journey strings from this product definition; do not use numeric journey IDs",
    )
    acceptance_refs: list[str] = Field(
        min_length=1,
        max_length=100,
        description="One-based acceptance IDs: acceptance-1 refers to the first product acceptance criterion",
    )
    component_refs: list[str] = Field(default_factory=list, max_length=50)
    behaviors: list[ExpectedBehavior] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def unique_ids(self) -> FunctionalFeature:
        if len({item.id for item in self.behaviors}) != len(self.behaviors):
            raise ValueError("Behavior IDs must be unique within a feature")
        for values in (self.journey_refs, self.acceptance_refs, self.component_refs):
            if len(set(values)) != len(values) or any(not value.strip() or len(value) > 4000 for value in values):
                raise ValueError("Feature references require unique bounded identifiers")
        return self


def validate_features(features: list[FunctionalFeature], journeys: list[str], acceptance_count: int) -> None:
    if len({item.id for item in features}) != len(features):
        raise ValueError("Feature IDs must be unique within a product")
    acceptance = {f"acceptance-{index + 1}" for index in range(acceptance_count)}
    for item in features:
        if set(item.journey_refs) - set(journeys) or set(item.acceptance_refs) - acceptance:
            raise ValueError(
                "Feature references require exact journey strings and one-based acceptance-N IDs "
                "from this product definition"
            )


def feature_contract(product: ProductDefinition) -> dict[str, Any]:
    journeys = [
        {"journey": journey, "feature_ids": [item.id for item in product.features if journey in item.journey_refs]}
        for journey in product.journey
    ]
    pending = [{"journey": item["journey"], "field": "features"} for item in journeys if not item["feature_ids"]]
    return {
        "version": 1,
        "status": "pending" if pending else "ready",
        "reviewed": product.functional_reviewed,
        "journeys": journeys,
        "features": [item.model_dump(mode="json") for item in product.features],
        "acceptance": {f"acceptance-{index + 1}": value for index, value in enumerate(product.acceptance)},
        "pending_fields": pending,
    }
