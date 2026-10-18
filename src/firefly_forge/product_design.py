# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Immutable customer-journey wireframes and explicit review of their saved references."""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import tempfile
import unicodedata
import xml.etree.ElementTree as ET
from contextlib import suppress
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from pyfly.container import service

from firefly_forge.product_branding import BrandInput, ProductBranding, generated_tokens, safe_logo, validate_tokens
from firefly_forge.product_fonts import manrope_license, manrope_style, manrope_text_width
from firefly_forge.store import Store, identifier
from firefly_forge.visual_design import VisualDesignService

if TYPE_CHECKING:
    from firefly_forge.collaboration import ProductDefinition

Data = dict[str, Any]
MAX_BUNDLE_BYTES = 2_000_000
MAX_DESIGN_FILE_BYTES = 10_000_000
VARIANTS = {
    "desktop_svg": ("desktop", "light"),
    "desktop_dark_svg": ("desktop", "dark"),
    "mobile_svg": ("mobile", "light"),
    "mobile_dark_svg": ("mobile", "dark"),
}


class DesignReview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    reviewed: Literal[True]
    method: Literal["wireframe", "process_map"] = "wireframe"
    screen_ids: list[str] = Field(min_length=1, max_length=50)
    viewports: list[Literal["desktop", "mobile"]] = Field(min_length=2, max_length=2)
    themes: list[Literal["light", "dark"]] = Field(min_length=2, max_length=2)


def _digest(value: Data) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    ).hexdigest()


def definition_digest(definition: ProductDefinition) -> str:
    return _digest(definition.model_dump(exclude={"design_review", "ai_origin"}))


def _lines(value: str, columns: int, maximum: int) -> list[str]:
    if any(ord(char) < 32 and char not in "\n\r\t" or 0xD800 <= ord(char) <= 0xDFFF for char in value):
        raise ValueError("Design labels require XML-compatible Unicode")
    result: list[str] = []
    current = ""
    used = 0

    def character_width(char: str) -> int:
        return 2 if unicodedata.east_asian_width(char) in {"W", "F"} or char in "ABCDEFGHIJKLMNOPQRSTUVWXYZmw@%" else 1

    for char in " ".join(value.split()):
        width = character_width(char)
        if used + width > columns:
            split = current.rfind(" ")
            if split > len(current) // 3:
                result.append(current[:split].rstrip())
                current = current[split + 1 :]
            else:
                result.append(current.rstrip())
                current = ""
            used = sum(character_width(character) for character in current)
            if len(result) == maximum:
                result[-1] = result[-1].rstrip()[:-1] + "…"
                return result
        current += char
        used += width
    if current:
        result.append(current)
    return result or [""]


def _text(
    value: str, x: int, y: int, *, columns: int = 54, maximum: int = 2, size: int = 16, weight: int = 400, fill: str
) -> str:
    lines = _lines(value, columns, maximum)
    return (
        '<text font-size="'
        + str(size)
        + '" font-weight="'
        + str(weight)
        + '" fill="'
        + html.escape(fill)
        + '">'
        + "".join(
            f'<tspan x="{x}" y="{y + index * (size + 8)}">{html.escape(line)}</tspan>'
            for index, line in enumerate(lines)
        )
        + "</text>"
    )


def _rect(x: int, y: int, width: int, height: int, fill: str, stroke: str = "none", radius: int = 12) -> str:
    return f'<rect x="{x}" y="{y}" width="{width}" height="{height}" rx="{radius}" fill="{fill}" stroke="{stroke}"/>'


def _palette(tokens: Data, theme: str, custom: bool) -> dict[str, str]:
    css, colors = validate_tokens(tokens)
    background = colors[f"color-{theme}-background"]
    primary = colors.get(f"color-{theme}-primary", [1, 0.702, 0.29, 1])
    components = [primary[index] * primary[3] + background[index] * (1 - primary[3]) for index in range(3)]
    linear = [value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4 for value in components]
    luminance = sum(value * weight for value, weight in zip(linear, (0.2126, 0.7152, 0.0722), strict=True))
    return {
        "bg": css[f"color-{theme}-background"],
        "text": css[f"color-{theme}-text"],
        "primary": css.get(f"color-{theme}-primary", "#ffb34a"),
        "surface": css[f"color-{theme}-background"] if custom else "#ffffff" if theme == "light" else "#1f201c",
        "primary_ink": "#ffffff" if luminance < 0.179 else "#000000" if custom else "#10110f",
        "accent": css[f"color-{theme}-text"] if custom else "#855414" if theme == "light" else "#ffb34a",
        "line": "#d8d4ca" if theme == "light" else "#474a42",
        "muted": css[f"color-{theme}-text"],
    }


def _logo(branding: Data | None, theme: str) -> str:
    if not branding:
        return ""
    svg = str(branding["files"]["logo-dark.svg" if theme == "dark" else "logo.svg"])
    root = ET.fromstring(safe_logo(svg))
    root.set("x", "24")
    root.set("y", "18")
    root.set("width", "184")
    root.set("height", "40")
    root.set("preserveAspectRatio", "xMinYMid meet")
    return ET.tostring(root, encoding="unicode")


def _reference_canvas(
    product: ProductDefinition,
    branding: Data | None,
    tokens: Data,
    *,
    index: int,
    viewport: str,
    theme: str,
    process: bool,
) -> tuple[list[str], Data, dict[str, str], int, int]:
    grid = VisualDesignService().geometry(product.visual_design, viewport)
    width, height = grid["viewport_width"], grid["viewport_height"]
    shell = product.visual_design.shell
    palette = _palette(tokens, theme, branding is not None)
    font = str(tokens.get("typography", {}).get("family", {}).get("$value", "Manrope"))
    title = product.journey[index]
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" role="img">',
        "<title>"
        + html.escape(title)
        + (" service-process reference" if process else " wireframe reference")
        + "</title>",
        "<desc>"
        + (
            "Functional definition pending. "
            if process and VisualDesignService().functional_contract(product)["status"] == "pending"
            else ""
        )
        + (
            "Document canvas, not an application interface."
            if process
            else "Customer journey reference, not a finished product interface."
        )
        + "</desc>",
        '<g font-family="' + html.escape(font) + ', sans-serif">',
        _rect(0, 0, width, height, palette["bg"], radius=0),
    ]
    if shell.header_height:
        parts.append(_rect(0, 0, width, shell.header_height, palette["surface"], palette["line"], radius=0))
        logo = _logo(branding, theme)
        if logo:
            root = ET.fromstring(logo)
            root.set("y", str(max(4, (shell.header_height - 40) // 2)))
            parts.append(ET.tostring(root, encoding="unicode"))
        else:
            name = str((branding or {}).get("name", product.customer))
            if not process:
                lines = _reference_lines(name, width - 48, 18, 700, custom_font=font != "Manrope")
                parts.append(
                    _reference_text(
                        lines if len(lines) == 1 else ["Product reference"],
                        24,
                        shell.header_height // 2 + 6,
                        18,
                        700,
                        palette["text"],
                    )
                )
            else:
                parts.append(
                    _text(
                        name,
                        24,
                        shell.header_height // 2 + 6,
                        columns=max(16, min(32, (width - 48) // 20)),
                        maximum=1,
                        size=18,
                        weight=700,
                        fill=palette["text"],
                    )
                )
    footer = height - shell.footer_height
    if shell.footer_height:
        parts.append(_rect(0, footer, width, shell.footer_height, palette["surface"], palette["line"], radius=0))
        parts.append(
            _text(
                "API process document" if process else "Functional reference",
                int(grid["content_x"]),
                footer + min(29, shell.footer_height - 12),
                columns=max(20, int(grid["content_width"]) // 8),
                maximum=1,
                size=12,
                fill=palette["text"],
            )
        )
    if viewport == "desktop" and shell.sidebar_width:
        parts.append(
            _rect(
                0,
                shell.header_height,
                shell.sidebar_width,
                footer - shell.header_height,
                palette["surface"],
                palette["line"],
                radius=0,
            )
        )
        rail_columns = max(12, (shell.sidebar_width - 48) // 7)
        parts.append(
            _text(
                "Journey index" if process else "Customer journeys",
                24,
                shell.header_height + 50,
                columns=rail_columns,
                maximum=1,
                size=14,
                weight=700,
                fill=palette["text"],
            )
        )
        shown = list(range(min(7, len(product.journey))))
        if index not in shown:
            shown[-1] = index
        for position, step in enumerate(shown):
            y = shell.header_height + 82 + position * 64
            if step == index:
                parts.append(_rect(16, y - 20, shell.sidebar_width - 32, 52, palette["primary"]))
            parts.append(
                _text(
                    str(step + 1) + ". " + product.journey[step] if process else f"Journey {step + 1}",
                    28,
                    y + 3,
                    columns=rail_columns,
                    maximum=2,
                    size=13,
                    fill=palette["primary_ink"] if step == index else palette["text"],
                )
            )
    return parts, grid, palette, shell.header_height, footer


def _panel_text(
    parts: list[str],
    value: str,
    x: int,
    y: int,
    width: int,
    *,
    size: int,
    fill: str,
    maximum: int = 2,
    weight: int = 400,
) -> None:
    parts.append(
        _text(
            value,
            x,
            y,
            columns=max(8, width // max(6, int(size * 0.58))),
            maximum=maximum,
            size=size,
            weight=weight,
            fill=fill,
        )
    )


def _journey_contract(product: ProductDefinition, index: int, *, process: bool = False) -> Data:
    journey = product.journey[index]
    linked = [item for item in product.features if journey in item.journey_refs]
    sources = [item for item in product.processes if journey in item.journey_refs] if process else linked
    acceptance_ids = list(dict.fromkeys(reference for item in sources for reference in item.acceptance_refs))
    return {
        "journey_index": index,
        "journey": journey,
        "feature_ids": [item.id for item in linked],
        "expected_behaviors": [
            {"feature_id": item.id, **behavior.model_dump(mode="json")}
            for item in linked
            for behavior in item.behaviors
        ],
        "acceptance_ids": acceptance_ids,
        "acceptance_criteria": {
            reference: product.acceptance[int(reference.split("-")[1]) - 1] for reference in acceptance_ids
        },
        "primary_action": "Review linked behavior",
        "states": ["default", "loading", "empty", "error"],
        "reference_scope": "functional_guidance",
    }


def _reference_lines(value: str, width: int, size: int, weight: int, *, custom_font: bool) -> list[str]:
    # Reuse XML-character validation before measuring text; no ellipsis is introduced.
    _lines(value, max(1, len(value) * 2), 1)
    result: list[str] = []
    current = ""
    for word in " ".join(value.split()).split(" "):
        candidate = (current + " " + word).strip()
        measure = len(candidate) * size if custom_font else manrope_text_width(candidate, size, weight)
        if current and measure > width:
            result.append(current)
            current = ""
        for char in word:
            candidate = current + char
            measure = len(candidate) * size if custom_font else manrope_text_width(candidate, size, weight)
            if current and measure > width:
                result.append(current)
                current = ""
            current += char
        current += " "
    if current.strip():
        result.append(current.strip())
    return result or [""]


def _reference_text(lines: list[str], x: int, y: int, size: int, weight: int, fill: str) -> str:
    return (
        f'<text font-size="{size}" font-weight="{weight}" fill="{html.escape(fill)}">'
        + "".join(
            f'<tspan x="{x}" y="{y + index * (size + 6)}">{html.escape(line.strip())}</tspan>'
            for index, line in enumerate(lines)
        )
        + "</text>"
    )


def _frame(
    product: ProductDefinition, branding: Data | None, tokens: Data, *, index: int, viewport: str, theme: str
) -> str:
    parts, grid, palette, header, footer = _reference_canvas(
        product, branding, tokens, index=index, viewport=viewport, theme=theme, process=False
    )
    left, content = int(grid["content_x"]), int(grid["content_width"])
    custom_font = tokens.get("typography", {}).get("family", {}).get("$value", "Manrope") != "Manrope"
    contract = _journey_contract(product, index)
    inset = product.visual_design.baseline * 5
    text = palette["text"]
    y = header + 40
    parts.append(_rect(left, y - 21, content, 32, palette["primary"], radius=4))
    parts.append(_reference_text(["Functional guide"], left + 12, y, 13, 700, palette["primary_ink"]))
    title_size = 26 if viewport == "desktop" else 22
    title = _reference_lines(product.journey[index], content, title_size, 700, custom_font=custom_font)
    if len(title) * (title_size + 6) > (footer - y) // 3:
        title_size = 16
        title = _reference_lines(
            f"Journey {index + 1}. Full title in interaction-contract.json",
            content,
            title_size,
            700,
            custom_font=custom_font,
        )
    y += 52
    parts.append(_reference_text(title, left, y, title_size, 700, text))
    top = y + (len(title) - 1) * (title_size + 6) + 28
    bottom = footer - 88
    if viewport == "desktop":
        span = max(1, min(grid["columns"] - 1, round(grid["columns"] * 2 / 3)))
        primary = int(span * grid["column_width"] + (span - 1) * grid["gutter"])
        aside_x, aside_width = left + primary + grid["gutter"], content - primary - grid["gutter"]
    else:
        primary, aside_x, aside_width = content, left, content

    blocks: list[tuple[str, str]] = []
    for feature in product.features:
        if feature.id not in contract["feature_ids"]:
            continue
        blocks.append(("Feature", feature.name))
        for behavior in feature.behaviors:
            blocks.extend([("Given", behavior.given), ("When", behavior.when), ("Then", behavior.then)])
            blocks.extend(("Failure path", failure) for failure in behavior.failure_paths)
    if not blocks:
        blocks = [
            (
                "Feature mapping pending",
                "Link this journey to explicit features and expected behavior before implementation.",
            )
        ]
    criteria = [(reference, value) for reference, value in contract["acceptance_criteria"].items()]
    if not criteria:
        criteria = [("Acceptance mapping pending", "No linked acceptance criteria are supplied for this journey.")]

    def panel(x: int, width: int, entries: list[tuple[str, str]]) -> bool:
        parts.append(_rect(x, top, width, max(1, bottom - top), palette["surface"], palette["line"]))
        cursor = top + inset + 14
        for label, value in entries:
            lines = _reference_lines(value, width - 2 * inset, 14, 400, custom_font=custom_font)
            labels = _reference_lines(label, width - 2 * inset, 13, 700, custom_font=custom_font)
            height = len(labels) * 19 + len(lines) * 20 + 16
            if cursor + height > bottom - inset:
                return True
            parts.append(_reference_text(labels, x + inset, cursor, 13, 700, text))
            cursor += len(labels) * 19 + 5
            parts.append(_reference_text(lines, x + inset, cursor, 14, 400, text))
            cursor += len(lines) * 20 + 11
        return False

    if viewport == "desktop":
        overflow = panel(left, primary, blocks)
        overflow = (
            panel(aside_x, aside_width, [("Linked acceptance", "Criteria for this journey only."), *criteria])
            or overflow
        )
    else:
        overflow = panel(
            left,
            primary,
            [*blocks, ("Linked acceptance", ", ".join(contract["acceptance_ids"]) or "Pending"), *criteria],
        )
    note = "More in interaction-contract.json" if overflow else "Full reference: interaction-contract.json"
    for value in [note, f"journey-{index + 1:02}: full behavior and acceptance"]:
        lines = _reference_lines(value, content, 12, 400, custom_font=custom_font)
        parts.append(_reference_text(lines, left, footer - 64, 12, 400, text))
        footer += len(lines) * 18
    parts.append("</g></svg>")
    return "".join(parts)


def _process_frame(
    product: ProductDefinition, branding: Data | None, tokens: Data, *, index: int, viewport: str, theme: str
) -> str:
    parts, grid, palette, header, footer = _reference_canvas(
        product, branding, tokens, index=index, viewport=viewport, theme=theme, process=True
    )
    left, content = int(grid["content_x"]), int(grid["content_width"])
    text, surface, line = palette["text"], palette["surface"], palette["line"]
    journey = product.journey[index]
    linked = [workflow for workflow in product.processes if journey in workflow.journey_refs]
    workflow = linked[0] if linked else None
    top = header + 112
    _panel_text(parts, "Functional process reference", left, header + 40, content, size=13, fill=text, maximum=1)
    _panel_text(
        parts,
        journey,
        left,
        header + 86,
        content,
        size=28 if viewport == "desktop" else 22,
        fill=text,
        maximum=1,
        weight=700,
    )
    if workflow is None:
        parts.append(_rect(left, top + 24, content, 280, surface, line))
        for value, offset, size, weight in [
            ("Functional definition pending", 64, 20, 700),
            ("Define the actor, trigger and ordered service steps for this journey.", 138, 13, 400),
            ("Inputs, outputs, failure paths and acceptance references are not yet approved.", 220, 13, 400),
        ]:
            _panel_text(
                parts, value, left + 20, top + offset, content - 40, size=size, weight=weight, fill=text, maximum=3
            )
    else:
        gap = grid["gutter"]
        span = max(1, min(grid["columns"] - 1, round(grid["columns"] * 2 / 3)))
        primary = int(span * grid["column_width"] + (span - 1) * gap) if viewport == "desktop" else content
        parts.append(_rect(left, top, primary, 100, surface, line))
        _panel_text(
            parts,
            "Actor: " + workflow.actor,
            left + 16,
            top + 33,
            primary - 32,
            size=13,
            weight=700,
            fill=text,
            maximum=1,
        )
        _panel_text(
            parts, "Trigger: " + workflow.trigger, left + 16, top + 67, primary - 32, size=13, fill=text, maximum=1
        )
        _panel_text(parts, workflow.name, left, top + 125, primary, size=13, weight=700, fill=text, maximum=1)
        shown = workflow.steps if viewport == "desktop" else workflow.steps[:5]
        columns = 2 if viewport == "desktop" and len(shown) > 6 else 1
        inset = product.visual_design.baseline * 6 if viewport == "desktop" else 0
        node_width = (primary - 2 * inset - gap * (columns - 1)) // columns
        node_top = top + 148
        rows = min(6, len(shown))
        pitch = min(80 if viewport == "desktop" else 84, (footer - node_top - 12) // rows)
        for number, step in enumerate(shown):
            column, row = divmod(number, 6) if columns == 2 else (0, number)
            x, y = left + inset + column * (node_width + gap), node_top + row * pitch
            parts.append(_rect(x, y, node_width, 72, surface, line))
            for value, offset, size, weight, maximum in [
                (str(number + 1) + ". " + step.action, 23, 13, 700, 2),
                ("Owner: " + step.owner, 56, 11, 400, 1),
                ("Output: " + ("; ".join(step.outputs) or "Pending definition"), 69, 10, 400, 1),
            ]:
                _panel_text(
                    parts,
                    value,
                    x + 12,
                    y + offset,
                    node_width - 24,
                    size=size,
                    weight=weight,
                    fill=text,
                    maximum=maximum,
                )
            if number + 1 < len(shown) and (columns == 1 or row < 5):
                center = x + node_width // 2
                parts.append(
                    f'<path d="M{center} {y + 72}v3m-3 0l3 3 3-3" fill="none" '
                    f'stroke="{palette["accent"]}" stroke-width="2"/>'
                )
        if viewport == "desktop":
            if columns == 2:
                first = left + inset + node_width // 2
                last = left + inset + node_width + gap + node_width // 2
                bottom = node_top + 5 * pitch + 72
                parts.append(
                    f'<path d="M{first} {bottom}v8H{left + primary - 8}V{node_top - 8}H{last}v8" '
                    f'fill="none" stroke="{palette["accent"]}" stroke-width="2"/>'
                )
            aside, aside_width = left + primary + gap, content - primary - gap
            for heading, panel_y, entries in [
                (
                    "Acceptance references",
                    top,
                    [
                        reference + ": " + product.acceptance[int(reference.split("-")[1]) - 1]
                        for reference in workflow.acceptance_refs[:4]
                    ],
                ),
                (
                    "Failure paths",
                    top + 316,
                    [step.id + ": " + failure for step in workflow.steps for failure in step.failures][:4],
                ),
            ]:
                parts.append(_rect(aside, panel_y, aside_width, 292, surface, line))
                _panel_text(
                    parts,
                    heading,
                    aside + 20,
                    panel_y + 36,
                    aside_width - 40,
                    size=16,
                    weight=700,
                    fill=text,
                    maximum=1,
                )
                for number, value in enumerate(entries):
                    _panel_text(
                        parts,
                        value,
                        aside + 20,
                        panel_y + 75 + number * 48,
                        aside_width - 40,
                        size=12,
                        fill=text,
                        maximum=2,
                    )
        else:
            _panel_text(
                parts,
                f"Overview: {len(shown)} of {len(workflow.steps)} steps. Full contract retained.",
                left,
                footer - 16,
                content,
                size=11,
                fill=text,
                maximum=1,
            )
    parts.append("</g></svg>")
    return "".join(parts)


def design_files(bundle: Data) -> dict[str, str]:
    files: dict[str, str] = {}
    for screen in bundle["screens"]:
        if not re.fullmatch(r"journey-[0-9]{2}", screen["id"]):
            raise ValueError("Invalid design screen identifier")
        for field, (viewport, theme) in VARIANTS.items():
            svg = screen[field]
            if bundle.get("font_css"):
                end = svg.index(">") + 1
                svg = svg[:end] + "<style>" + bundle["font_css"] + "</style>" + svg[end:]
            files[f"docs/design/reference-{screen['id']}-{viewport}-{theme}.svg"] = svg
    files["docs/design/interaction-contract.json"] = json.dumps(
        {
            "method": bundle["method"],
            "requirements": bundle["requirements"],
            "screens": [
                {"id": screen["id"], "title": screen["title"], "interaction_contract": screen["interaction_contract"]}
                for screen in bundle["screens"]
            ],
        },
        indent=2,
    )
    if bundle["requirements"].get("grid"):
        files.update(VisualDesignService().files(bundle))
    if bundle.get("font_css"):
        files["docs/design/FONT-LICENSE.txt"] = manrope_license()
        if "docs/design/grid.svg" in files:
            grid = files["docs/design/grid.svg"]
            end = grid.index(">") + 1
            files["docs/design/grid.svg"] = grid[:end] + "<style>" + bundle["font_css"] + "</style>" + grid[end:]
    return files


@service
class ProductDesign:
    def __init__(self, store: Store) -> None:
        self.store = store
        self.directory = store.configuration.directory / "design-previews"

    async def preview(self, repository_id: str, definition: ProductDefinition) -> Data:
        await self.store.repository(repository_id)
        branding = await ProductBranding(self.store).current(repository_id)
        tokens = (
            branding["tokens"]
            if branding
            else generated_tokens(
                BrandInput(
                    name="Customer workspace",
                    primary="#ffb34a",
                    background="#f3f1eb",
                    text="#272820",
                    dark_background="#10110f",
                    dark_text="#f3f1eb",
                )
            )
        )
        requirements: Data = {
            name: getattr(definition, name)
            for name in ["acceptance", "user_stories", "security_requirements", "nonfunctional_requirements"]
        }
        design = VisualDesignService()
        requirements["grid"] = design.contract(definition)
        method = "process_map" if requirements["grid"]["scope"] == "diagram_document" else "wireframe"
        functional = design.functional_contract(definition)
        screens: list[Data] = []
        for index, journey in enumerate(definition.journey):
            screen: Data = {
                "id": f"journey-{index + 1:02}",
                "title": journey,
                "interaction_contract": {
                    **_journey_contract(definition, index, process=method == "process_map"),
                    "primary_action": "Review functional contract"
                    if method == "process_map"
                    else "Review linked behavior",
                },
            }
            for field, (viewport, theme) in VARIANTS.items():
                renderer = _process_frame if method == "process_map" else _frame
                screen[field] = renderer(definition, branding, tokens, index=index, viewport=viewport, theme=theme)
            screens.append(screen)
        bundle: Data = {
            "repository_id": repository_id,
            "method": method,
            "functional_definition_status": functional["status"],
            "functional_contract": functional,
            "feature_contract": functional["feature_contract"],
            "grid_palette": _palette(tokens, "light", branding is not None),
            "definition_digest": definition_digest(definition),
            "brand_digest": branding["digest"] if branding else None,
            "font_family": tokens.get("typography", {}).get("family", {}).get("$value", "Manrope"),
            "font_css": manrope_style()
            if tokens.get("typography", {}).get("family", {}).get("$value", "Manrope") == "Manrope"
            else "",
            "requirements": requirements,
            "screens": screens,
            "coverage": {"journeys": len(screens), "screen_count": len(screens)},
        }
        bundle["artifact_paths"] = list(design_files(bundle))
        bundle["digest"] = _digest(bundle)
        payload = json.dumps(bundle, ensure_ascii=True, sort_keys=True).encode()
        if len(payload) > MAX_BUNDLE_BYTES:
            raise ValueError("Design bundle exceeds 2 MB")
        path = self.directory / identifier(repository_id).hex / (bundle["digest"] + ".json")
        with self.store.configuration.lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".preview-", delete=False) as file:
                    temporary = file.name
                    file.write(payload)
                    file.flush()
                    os.fsync(file.fileno())
                with suppress(FileExistsError):
                    os.link(temporary, path)
            finally:
                if temporary:
                    os.unlink(temporary)
        return await self.load(repository_id, bundle["digest"])

    async def load(self, repository_id: str, digest: str) -> Data:
        await self.store.repository(repository_id)
        if not re.fullmatch(r"[a-f0-9]{64}", digest):
            raise ValueError("Invalid design digest")
        path = self.directory / identifier(repository_id).hex / (digest + ".json")
        if not path.is_file() or path.is_symlink():
            raise KeyError("Saved design bundle not found")
        if path.stat().st_size > MAX_BUNDLE_BYTES:
            raise ValueError("Design bundle exceeds 2 MB")
        bundle: Data = json.loads(path.read_bytes())
        signed = {key: value for key, value in bundle.items() if key != "digest"}
        if bundle.get("repository_id") != repository_id or bundle.get("digest") != digest or _digest(signed) != digest:
            raise ValueError("Saved design bundle digest does not match its contents")
        return bundle

    async def validate_review(self, repository_id: str, definition: ProductDefinition) -> Data | None:
        review = definition.design_review
        if review is None:
            return None
        bundle = await self.load(repository_id, review.digest)
        if bundle["definition_digest"] != definition_digest(definition):
            raise ValueError("Design review belongs to a different product definition")
        branding = await ProductBranding(self.store).current(repository_id)
        if bundle["brand_digest"] != (branding["digest"] if branding else None):
            raise ValueError("Product brand changed after design review")
        if (
            review.method != bundle["method"]
            or set(review.screen_ids) != {screen["id"] for screen in bundle["screens"]}
            or len(review.screen_ids) != len(bundle["screens"])
            or set(review.viewports) != {"desktop", "mobile"}
            or set(review.themes) != {"light", "dark"}
        ):
            raise ValueError("Review every journey, both viewports, and both themes before saving")
        if bundle["method"] == "process_map" and bundle["functional_definition_status"] != "ready":
            raise ValueError(
                "Functional definition is pending; define linked processes and their contracts before review"
            )
        return bundle
