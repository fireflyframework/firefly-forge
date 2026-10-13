# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Data-only product brand kits, frozen before coding and independent of factory branding."""

from __future__ import annotations

import hashlib
import html
import json
import math
import re
import uuid
import xml.etree.ElementTree as ET
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from pyfly.container import service

from firefly_forge.product_fonts import manrope_license, manrope_style, manrope_wordmark
from firefly_forge.store import Store, identifier

COLOR = r"^#[a-fA-F0-9]{6}$"


class BrandInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=80)
    font_family: str = Field(default="Manrope", pattern=r"^[A-Za-z][A-Za-z0-9 -]{0,79}$")
    primary: str = Field(default="#ffb347", pattern=COLOR)
    background: str = Field(default="#ffffff", pattern=COLOR)
    text: str = Field(default="#292a25", pattern=COLOR)
    dark_background: str = Field(default="#252620", pattern=COLOR)
    dark_text: str = Field(default="#f7f6f2", pattern=COLOR)
    tokens: dict[str, Any] | None = None
    logo_svg: str | None = Field(default=None, max_length=65536)
    logo_dark_svg: str | None = Field(default=None, max_length=65536)


def _color(value: str) -> dict[str, Any]:
    return {
        "$type": "color",
        "$value": {
            "colorSpace": "srgb",
            "components": [int(value[index : index + 2], 16) / 255 for index in (1, 3, 5)],
            "alpha": 1,
            "hex": value.lower(),
        },
    }


def generated_tokens(request: BrandInput) -> dict[str, Any]:
    return {
        "$schema": "https://www.designtokens.org/schemas/2025.10/format.json",
        "color": {
            "light": {
                "primary": _color(request.primary),
                "background": _color(request.background),
                "text": _color(request.text),
            },
            "dark": {
                "primary": _color(request.primary),
                "background": _color(request.dark_background),
                "text": _color(request.dark_text),
            },
        },
        "typography": {"family": {"$type": "fontFamily", "$value": request.font_family}},
        "spacing": {
            f"s{index}": {"$type": "dimension", "$value": {"value": index * 4, "unit": "px"}}
            for index in (1, 2, 3, 4, 6, 8, 12)
        },
        "radius": {"control": {"$type": "dimension", "$value": {"value": 8, "unit": "px"}}},
    }


def validate_tokens(tokens: dict[str, Any]) -> tuple[dict[str, str], dict[str, list[float]]]:
    if len(json.dumps(tokens)) > 65536:
        raise ValueError("Product tokens exceed 64 KB")
    css: dict[str, str] = {}
    colors: dict[str, list[float]] = {}

    def visit(node: dict[str, Any], path: list[str]) -> None:
        if len(path) > 10 or len(css) > 200:
            raise ValueError("Product token structure exceeds supported limits")
        if "$value" in node:
            key, kind, value = "-".join(path), node.get("$type"), node["$value"]
            if not path or key in css:
                raise ValueError("Product token names must be unique CSS variable names")
            if kind == "color":
                if not isinstance(value, dict) or value.get("colorSpace") != "srgb":
                    raise ValueError("Color tokens require an explicit sRGB component object")
                components, alpha = value.get("components"), value.get("alpha", 1)
                if (
                    not isinstance(components, list)
                    or len(components) != 3
                    or any(
                        not isinstance(number, (float, int))
                        or isinstance(number, bool)
                        or not math.isfinite(number)
                        or not 0 <= number <= 1
                        for number in [*components, alpha]
                    )
                ):
                    raise ValueError("Invalid sRGB token components")
                colors[key] = [float(component) for component in components] + [float(alpha)]
                css[key] = f"rgb({' '.join(str(round(component * 255, 3)) for component in components)} / {alpha})"
            elif kind == "dimension":
                if (
                    not isinstance(value, dict)
                    or value.get("unit") not in {"px", "rem"}
                    or not isinstance(value.get("value"), (float, int))
                    or isinstance(value["value"], bool)
                    or not math.isfinite(value["value"])
                    or not 0 <= value["value"] <= 4096
                ):
                    raise ValueError("Dimensions require a finite nonnegative px or rem value")
                css[key] = str(value["value"]) + value["unit"]
            elif (
                kind == "fontFamily" and isinstance(value, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9 -]{0,79}", value)
            ):
                css[key] = f'"{value}", sans-serif'
            else:
                raise ValueError(
                    "Import explicit color, dimension or fontFamily tokens; aliases require resolution first"
                )
            return
        for name, child in node.items():
            if name.startswith("$"):
                continue
            if not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", name) or not isinstance(child, dict):
                raise ValueError("Product token groups require lowercase bounded names and objects")
            visit(child, [*path, name])

    visit(tokens, [])
    for theme in ("light", "dark"):
        background, foreground = [colors.get(f"color-{theme}-{name}") for name in ("background", "text")]
        if not background or not foreground or background[3] != 1 or foreground[3] != 1:
            raise ValueError("Import opaque light/dark background and text semantic color tokens")

        def luminance(components: list[float]) -> float:
            linear = [
                component / 12.92 if component <= 0.04045 else ((component + 0.055) / 1.055) ** 2.4
                for component in components[:3]
            ]
            return sum(component * weight for component, weight in zip(linear, (0.2126, 0.7152, 0.0722), strict=True))

        first, second = sorted((luminance(background), luminance(foreground)))
        if (second + 0.05) / (first + 0.05) < 4.5:
            raise ValueError("Normal text/background contrast must reach 4.5:1 in both themes")
    return css, colors


def safe_logo(svg: str) -> str:
    if "<!" in svg or len(svg.encode()) > 65536:
        raise ValueError("Import a bounded static SVG without declarations or entities")
    try:
        root = ET.fromstring(svg)
    except ET.ParseError as exc:
        raise ValueError("Invalid SVG logo") from exc
    allowed = {
        "svg",
        "g",
        "path",
        "rect",
        "circle",
        "ellipse",
        "line",
        "polyline",
        "polygon",
        "defs",
        "linearGradient",
        "radialGradient",
        "stop",
        "clipPath",
        "title",
        "desc",
        "text",
        "tspan",
    }
    if root.tag.split("}")[-1] != "svg":
        raise ValueError("Import an SVG root element")
    for element in root.iter():
        if element.tag.split("}")[-1] not in allowed:
            raise ValueError("Logo must contain only static vector and text elements")
        for name, value in element.attrib.items():
            name = name.split("}")[-1].lower()
            if (
                name.startswith("on")
                or name in {"href", "style", "src"}
                or "://" in value
                or ("url(" in value.lower() and not re.fullmatch(r"url\(#[A-Za-z0-9_-]+\)", value))
            ):
                raise ValueError("Logo must not execute code or load external resources")
    return svg


@service
class ProductBranding:
    def __init__(self, store: Store) -> None:
        self.store = store

    async def current(self, repository_id: str) -> dict[str, Any] | None:
        await self.store.repository(repository_id)
        path = self.store.configuration.directory / f"product-brand-{identifier(repository_id).hex}.json"
        with self.store.configuration.lock:
            return self.store.configuration._read(path) or None

    async def configure(self, repository_id: str, request: BrandInput) -> dict[str, Any]:
        await self.store.repository(repository_id)
        tokens = request.tokens if request.tokens is not None else generated_tokens(request)
        css, _ = validate_tokens(tokens)
        font = tokens.get("typography", {}).get("family", {}).get("$value", request.font_family)
        if not isinstance(font, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9 -]{0,79}", font):
            raise ValueError("Typography family requires an explicit fontFamily token")

        def wordmark(color: str) -> str:
            if font == "Manrope":
                return manrope_wordmark(request.name, color)
            return (
                f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {max(240, len(request.name) * 38)} 96">'
                f'<title>{html.escape(request.name)}</title><text x="8" y="68" font-family="{html.escape(font)}" '
                f'font-size="56" font-weight="700" fill="{color}">{html.escape(request.name)}</text></svg>'
            )

        logo = safe_logo(request.logo_svg) if request.logo_svg else wordmark(css["color-light-text"])
        dark_logo = (
            safe_logo(request.logo_dark_svg)
            if request.logo_dark_svg
            else logo
            if request.logo_svg
            else wordmark(css["color-dark-text"])
        )
        files = {
            "tokens.json": json.dumps(tokens, indent=2),
            "logo.svg": logo,
            "logo-dark.svg": dark_logo,
            "theme.css": (manrope_style() + "\n" if font == "Manrope" else "")
            + ":root {\n"
            + "\n".join(f"  --{key}: {value};" for key, value in css.items())
            + "\n  --color-background: var(--color-light-background);\n  --color-text: var(--color-light-text);\n}\n"
            '[data-theme="dark"] { --color-background: var(--color-dark-background); '
            "--color-text: var(--color-dark-text); --color-primary: var(--color-dark-primary); }\n"
            ":root { --color-primary: var(--color-light-primary); }\n",
            "brand-guide.md": f"# {request.name} product design\n\n"
            "Build atoms (controls and typography), molecules (labeled fields and feedback), organisms "
            "(navigation, forms and tables), reusable layout templates and complete pages. Use tokens.json "
            "and theme.css for colors, spacing and typography. Include keyboard focus, labeled controls, "
            "loading, empty and error states. Test light/dark, mobile layouts and customer journeys. "
            "Manrope is embedded in theme.css with its font license. Generated Manrope wordmarks use outlined "
            "glyphs, so standalone logos retain their appearance. Install licensed local assets for any other "
            "configured font before using it.\n",
        }
        if font == "Manrope":
            files["FONT-LICENSE.txt"] = manrope_license()
        revision = uuid.uuid4().hex
        data: dict[str, Any] = {
            "repository_id": repository_id,
            "revision": revision,
            "name": request.name,
            "font_family": font,
            "logo_surface": "light" if request.logo_svg and not request.logo_dark_svg else "adaptive",
            "tokens": tokens,
            "files": files,
            "digest": hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest(),
        }
        private = self.store.configuration
        with private.lock:
            private._write(private.revisions / (revision + ".json"), data)
            private._write(private.directory / f"product-brand-{identifier(repository_id).hex}.json", data)
        return data
