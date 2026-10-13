# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Typed reference-grid and functional-process contracts with deterministic artifacts."""

from __future__ import annotations

import html
import json
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pyfly.container import service

from firefly_forge.functional_features import feature_contract

if TYPE_CHECKING:
    from firefly_forge.collaboration import ProductDefinition

Data = dict[str, Any]


class DesktopGrid(BaseModel):
    model_config = ConfigDict(extra="forbid")
    viewport_width: int = Field(default=1440, ge=1024, le=1920, strict=True)
    viewport_height: int = Field(default=900, ge=900, le=1600, strict=True)
    columns: int = Field(default=12, ge=6, le=16, strict=True)
    gutter: int = Field(default=24, ge=0, le=48, strict=True)
    margin: int = Field(default=32, ge=12, le=64, strict=True)


class MobileGrid(BaseModel):
    model_config = ConfigDict(extra="forbid")
    viewport_width: int = Field(default=390, ge=320, le=600, strict=True)
    viewport_height: int = Field(default=844, ge=844, le=1400, strict=True)
    columns: int = Field(default=4, ge=2, le=6, strict=True)
    gutter: int = Field(default=16, ge=0, le=32, strict=True)
    margin: int = Field(default=20, ge=12, le=40, strict=True)


class ReferenceShell(BaseModel):
    model_config = ConfigDict(extra="forbid")
    header_height: int = Field(default=76, ge=0, le=128, strict=True)
    sidebar_width: int = Field(default=248, ge=0, le=360, strict=True)
    footer_height: int = Field(default=64, ge=0, le=100, strict=True)
    max_content_width: int = Field(default=1440, ge=640, le=1920, strict=True)

    @model_validator(mode="after")
    def readable_regions(self) -> ReferenceShell:
        for value, minimum in [(self.header_height, 48), (self.sidebar_width, 160), (self.footer_height, 40)]:
            if value and value < minimum:
                raise ValueError("Shell regions must be disabled (0) or large enough for readable content")
        return self


class VisualDesign(BaseModel):
    model_config = ConfigDict(extra="forbid")
    renderer: Literal["product-reference-template-v2"] = "product-reference-template-v2"
    geometry_configurable: Literal[True] = True
    baseline: Literal[2, 4, 8] = 4
    desktop: DesktopGrid = Field(default_factory=DesktopGrid)
    mobile: MobileGrid = Field(default_factory=MobileGrid)
    shell: ReferenceShell = Field(default_factory=ReferenceShell)

    @model_validator(mode="before")
    @classmethod
    def previous_contract(cls, value: Any) -> Any:
        if isinstance(value, dict) and value.get("renderer") == "fixed-reference-template-v1":
            return {**value, "renderer": "product-reference-template-v2", "geometry_configurable": True}
        return value

    @model_validator(mode="after")
    def readable_geometry(self) -> VisualDesign:
        grids: list[tuple[str, DesktopGrid | MobileGrid]] = [("desktop", self.desktop), ("mobile", self.mobile)]
        for viewport, grid in grids:
            rail = self.shell.sidebar_width if viewport == "desktop" else 0
            content = min(grid.viewport_width - rail - 2 * grid.margin, self.shell.max_content_width)
            column = (content - (grid.columns - 1) * grid.gutter) / grid.columns
            if content < (600 if viewport == "desktop" else 240) or column < (36 if viewport == "desktop" else 24):
                raise ValueError("Grid columns, gutters and shell must leave readable product content")
            if grid.viewport_height - self.shell.header_height - self.shell.footer_height < (
                740 if viewport == "desktop" else 680
            ):
                raise ValueError("Reference canvas must leave readable content between header and footer")
        return self

    atomic_hierarchy: tuple[
        Literal["atoms"], Literal["molecules"], Literal["organisms"], Literal["templates"], Literal["pages"]
    ] = ("atoms", "molecules", "organisms", "templates", "pages")
    states: tuple[Literal["default"], Literal["loading"], Literal["empty"], Literal["error"], Literal["focus"]] = (
        "default",
        "loading",
        "empty",
        "error",
        "focus",
    )
    accessibility: tuple[
        Literal["semantic-controls"],
        Literal["keyboard"],
        Literal["visible-focus"],
        Literal["contrast"],
        Literal["status-announcements"],
    ] = ("semantic-controls", "keyboard", "visible-focus", "contrast", "status-announcements")
    guidance: list[str] = Field(default_factory=list, max_length=30)

    @field_validator("guidance")
    @classmethod
    def bounded_guidance(cls, values: list[str]) -> list[str]:
        if any(not value.strip() or len(value) > 2000 for value in values):
            raise ValueError("Design guidance requires bounded nonempty entries")
        return [value.strip() for value in values]


class ProcessStep(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    action: str = Field(min_length=1, max_length=2000)
    owner: str = Field(min_length=1, max_length=400)
    inputs: list[str] = Field(default_factory=list, max_length=20)
    outputs: list[str] = Field(default_factory=list, max_length=20)
    failures: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("inputs", "outputs", "failures")
    @classmethod
    def bounded_entries(cls, values: list[str]) -> list[str]:
        if any(not value.strip() or len(value) > 2000 for value in values):
            raise ValueError("Process entries require bounded nonempty text")
        return [value.strip() for value in values]


class ProcessWorkflow(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    name: str = Field(min_length=1, max_length=400)
    actor: str = Field(min_length=1, max_length=400)
    trigger: str = Field(min_length=1, max_length=2000)
    journey_refs: list[str] = Field(default_factory=list, max_length=50)
    steps: list[ProcessStep] = Field(min_length=1, max_length=12)
    acceptance_refs: list[str] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def unique_steps(self) -> ProcessWorkflow:
        if len({step.id for step in self.steps}) != len(self.steps):
            raise ValueError("Process step IDs must be unique within their workflow")
        return self


def validate_process_references(processes: list[ProcessWorkflow], journeys: list[str], acceptance_count: int) -> None:
    if len({workflow.id for workflow in processes}) != len(processes):
        raise ValueError("Process workflow IDs must be unique")
    accepted = {f"acceptance-{index + 1}" for index in range(acceptance_count)}
    for workflow in processes:
        if set(workflow.journey_refs) - set(journeys) or set(workflow.acceptance_refs) - accepted:
            raise ValueError("Process references must identify actual customer journeys and acceptance IDs")


@service
class VisualDesignService:
    def geometry(self, spec: VisualDesign, viewport: str) -> Data:
        grid = spec.desktop if viewport == "desktop" else spec.mobile
        sidebar = spec.shell.sidebar_width if viewport == "desktop" else 0
        available = grid.viewport_width - sidebar - 2 * grid.margin
        content = min(available, spec.shell.max_content_width)
        return {
            "viewport_width": grid.viewport_width,
            "columns": grid.columns,
            "gutter": grid.gutter,
            "margin": grid.margin,
            "viewport_height": grid.viewport_height,
            "content_x": sidebar + grid.margin + (available - content) / 2,
            "content_width": content,
            "column_width": (content - (grid.columns - 1) * grid.gutter) / grid.columns,
        }

    def contract(self, product: ProductDefinition) -> Data:
        return {
            **product.visual_design.model_dump(mode="json"),
            "scope": "diagram_document"
            if product.architecture and product.architecture.frontend == "none"
            else "product_ui",
        }

    def functional_contract(self, product: ProductDefinition) -> Data:
        missing = []
        for journey in product.journey:
            if not any(journey in process.journey_refs for process in product.processes):
                missing.append({"journey": journey, "field": "process"})
        for process in product.processes:
            if not process.acceptance_refs:
                missing.append({"process_id": process.id, "field": "acceptance_refs"})
            for step in process.steps:
                for field in ["inputs", "outputs", "failures"]:
                    if not getattr(step, field):
                        missing.append({"process_id": process.id, "step_id": step.id, "field": field})
        return {
            "status": "pending" if missing else "ready",
            "feature_contract": feature_contract(product),
            "pending_fields": missing,
            "customer": product.customer,
            "problem": product.problem,
            "journeys": product.journey,
            "processes": [item.model_dump(mode="json") for item in product.processes],
            "acceptance": {f"acceptance-{index + 1}": value for index, value in enumerate(product.acceptance)},
            "security_requirements": product.security_requirements,
            "nonfunctional_requirements": product.nonfunctional_requirements,
        }

    def grid_svg(self, contract: Data, font: str = "Manrope", palette: Data | None = None) -> str:
        spec = VisualDesign.model_validate({key: value for key, value in contract.items() if key != "scope"})
        colors = palette or {"bg": "#f3f1eb", "text": "#272820", "primary": "#ffb34a", "line": "#855414"}
        family = html.escape(font + ",sans-serif")
        parts = [
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1440 420" role="img">'
            "<title>Reference layout grid</title>"
            "<desc>Product-specific renderer grid; document canvas for API-only products.</desc>"
            '<rect width="1440" height="420" fill="' + html.escape(colors["bg"]) + '"/>'
        ]
        for viewport, x, y, target in [("desktop", 24, 60, 1000), ("mobile", 1080, 60, 336)]:
            grid = self.geometry(spec, viewport)
            scale = target / grid["viewport_width"]
            label = f"{viewport.title()}: {grid['columns']} columns, {grid['gutter']} px gutters"
            parts.append(
                f'<text x="{x}" y="{y - 20}" fill="{html.escape(colors["text"])}" '
                f'font-family="{family}" font-size="18">'
                f"{html.escape(label)}</text>"
            )
            parts.append(f'<g transform="translate({x} {y}) scale({scale})">')
            for column in range(grid["columns"]):
                left = grid["content_x"] + column * (grid["column_width"] + grid["gutter"])
                parts.append(
                    f'<rect x="{left}" y="0" width="{grid["column_width"]}" height="360" '
                    f'fill="{html.escape(colors["primary"])}" fill-opacity="0.35" '
                    f'stroke="{html.escape(colors["line"])}"/>'
                )
            parts.append("</g>")
        parts.append(
            f'<text x="24" y="390" fill="{html.escape(colors["text"])}" font-family="{family}" font-size="16">'
            f"{spec.baseline} px baseline · Header {spec.shell.header_height} px · "
            f"Desktop rail {spec.shell.sidebar_width} px · Footer {spec.shell.footer_height} px · "
            "Product-specific geometry</text></svg>"
        )
        return "".join(parts)

    def files(self, bundle: Data) -> dict[str, str]:
        grid = bundle["requirements"]["grid"]
        result = {
            "docs/design/layout-contract.json": json.dumps(grid, indent=2),
            "docs/design/grid.svg": self.grid_svg(grid, str(bundle["font_family"]), bundle.get("grid_palette")),
            "docs/design/functional-contract.json": json.dumps(bundle["functional_contract"], indent=2),
        }
        if bundle["method"] == "process_map":
            functional = bundle["functional_contract"]
            result["docs/design/functional-contract.json"] = json.dumps(functional, indent=2)
            lines = [
                "# Functional process reference",
                "",
                f"Definition status: {functional['status']}.",
                "",
                "This is a service-process document, not an application interface.",
                "",
            ]
            for workflow in functional["processes"]:
                lines.extend(
                    [
                        "## " + workflow["name"],
                        "",
                        "Actor: " + workflow["actor"],
                        "",
                        "Trigger: " + workflow["trigger"],
                        "",
                    ]
                )
                for number, step in enumerate(workflow["steps"], 1):
                    lines.extend(
                        [
                            f"{number}. {step['action']} (owner: {step['owner']})",
                            "   - Inputs: " + ("; ".join(step["inputs"]) or "Pending definition"),
                            "   - Outputs: " + ("; ".join(step["outputs"]) or "Pending definition"),
                            "   - Failure paths: " + ("; ".join(step["failures"]) or "Pending definition"),
                        ]
                    )
                lines.extend(
                    [
                        "",
                        "Acceptance references: " + (", ".join(workflow["acceptance_refs"]) or "Pending definition"),
                        "",
                    ]
                )
            if functional["pending_fields"]:
                lines.extend(["## Pending definitions", "", json.dumps(functional["pending_fields"], indent=2)])
            result["docs/design/processes.md"] = "\n".join(lines) + "\n"
        return result
