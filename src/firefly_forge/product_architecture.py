# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Reviewable architecture contracts derived from supported factory profiles."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pyfly.container import service

from firefly_forge.models import Backend, Frontend
from firefly_forge.profiles import profile_catalog


class ArchitectureComponent(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=100)
    layer: Literal["domain", "application", "data", "web", "frontend", "operations"]
    responsibility: str = Field(min_length=1, max_length=2000)


class ProductArchitecture(BaseModel):
    model_config = ConfigDict(extra="forbid")
    backend: Backend
    frontend: Frontend
    components: list[ArchitectureComponent] = Field(min_length=1)
    decisions: list[str] = Field(default_factory=list)
    nonfunctional_requirements: list[str] = Field(default_factory=list)
    reviewed: bool = False

    @field_validator("decisions", "nonfunctional_requirements")
    @classmethod
    def bounded_entries(cls, entries: list[str]) -> list[str]:
        if any(not entry.strip() or len(entry) > 4000 for entry in entries):
            raise ValueError("Architecture entries must contain 1 to 4000 characters")
        return [entry.strip() for entry in entries]


@service
class ProductArchitectureService:
    def propose(
        self, definition: dict[str, Any], *, backend: Backend | None = None, frontend: Frontend | None = None
    ) -> dict[str, Any]:
        from firefly_forge.collaboration import ProductDefinition

        product = ProductDefinition.model_validate(definition)
        selected_backend: Backend = backend or "pyfly"
        selected_frontend: Frontend = frontend or "react"
        profiles = profile_catalog()
        profile = next(item for item in profiles["backends"] if item["id"] == selected_backend)
        components = [
            ArchitectureComponent(
                name="Customer domain",
                layer="domain",
                responsibility="Typed customer concepts and acceptance rules independent of HTTP and storage.",
            ),
            ArchitectureComponent(
                name="Customer journeys",
                layer="application",
                responsibility="Use cases coordinating the reviewed journey, access checks, and transactions.",
            ),
            ArchitectureComponent(
                name="Persistence adapters",
                layer="data",
                responsibility="Typed repositories, migrations, scoped queries, and durable state transitions.",
            ),
            ArchitectureComponent(
                name="HTTP contracts",
                layer="web",
                responsibility="Validated HTTP contracts, authorization, and controller-to-service delegation.",
            ),
            ArchitectureComponent(
                name="Operations",
                layer="operations",
                responsibility="Health, evidence, deployment checks, recovery, and configuration without secrets.",
            ),
        ]
        if selected_frontend != "none":
            components.append(
                ArchitectureComponent(
                    name="Customer workspace",
                    layer="frontend",
                    responsibility="Atomic design with accessible journeys and complete interaction states.",
                )
            )
        architecture = ProductArchitecture(
            backend=selected_backend,
            frontend=selected_frontend,
            components=components,
            decisions=[
                "Use native framework injection; keep domain logic outside controllers and UI components.",
                "Convert each acceptance criterion into a behavior check and retain verification evidence.",
                "Use reviewed design tokens and an owned brand kit for colors, typography, spacing, and themes.",
            ],
            nonfunctional_requirements=product.nonfunctional_requirements,
        )
        rationale = str(profile["detail"])
        if backend is None:
            rationale = "PyFly is the default for typed Python services and async customer workflows. " + rationale
        elif backend == "larafly":
            rationale = "LaraFly fits a Laravel/PHP team and existing Laravel modules. " + rationale
        return {
            "method": "rule_based",
            "recommendation": {
                "backend": selected_backend,
                "frontend": selected_frontend,
                "rationale": rationale,
                "alternatives": [
                    {"id": item["id"], "name": item["name"], "detail": item["detail"], "source": item["source"]}
                    for item in profiles["backends"]
                    if item["id"] != selected_backend
                ],
            },
            "architecture": architecture.model_dump(),
        }
