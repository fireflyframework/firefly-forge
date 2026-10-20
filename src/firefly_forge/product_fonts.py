# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Licensed local Manrope assets and portable outlined product wordmarks."""

from __future__ import annotations

import base64
import html
from functools import lru_cache
from pathlib import Path

from fontTools.pens.svgPathPen import SVGPathPen  # type: ignore[import-untyped]
from fontTools.pens.transformPen import TransformPen  # type: ignore[import-untyped]
from fontTools.ttLib import TTFont  # type: ignore[import-untyped]
from fontTools.varLib.instancer import instantiateVariableFont  # type: ignore[import-untyped]


def _directory() -> Path:
    packaged = Path(__file__).parent / "fonts" / "manrope"
    return packaged if packaged.is_dir() else Path(__file__).resolve().parents[2] / "assets" / "fonts" / "manrope"


@lru_cache(maxsize=1)
def manrope_style() -> str:
    encoded = base64.b64encode((_directory() / "manrope-latin-wght-normal.woff2").read_bytes()).decode()
    return (
        '@font-face { font-family: "Manrope"; font-style: normal; font-weight: 200 800; '
        "font-display: swap; src: url(data:font/woff2;base64," + encoded + ') format("woff2"); }'
    )


def manrope_license() -> str:
    return (_directory() / "OFL.txt").read_text()


@lru_cache(maxsize=128)
def manrope_wordmark(name: str, color: str) -> str:
    with TTFont(_directory() / "manrope-latin-wght-normal.woff2") as font:
        instantiateVariableFont(font, {"wght": 700}, inplace=True)
        glyphs, cmap = font.getGlyphSet(), font.getBestCmap()
        scale = 56 / font["head"].unitsPerEm
        advance = 8.0
        paths: list[str] = []
        for character in name:
            glyph_name = cmap.get(ord(character))
            if glyph_name is None:
                raise ValueError("The bundled Manrope subset cannot render this name; import an outlined product logo")
            pen = SVGPathPen(glyphs)
            glyphs[glyph_name].draw(TransformPen(pen, (scale, 0, 0, -scale, advance, 68)))
            paths.append('<path d="' + pen.getCommands() + '"/>')
            advance += font["hmtx"][glyph_name][0] * scale
        return (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 '
            + str(round(advance + 8, 3))
            + ' 96"><title>'
            + html.escape(name)
            + '</title><desc>Manrope product wordmark</desc><g fill="'
            + html.escape(color)
            + '">'
            + "".join(paths)
            + "</g></svg>"
        )


@lru_cache(maxsize=3)
def _manrope_advances(weight: int) -> tuple[dict[int, float], float]:
    with TTFont(_directory() / "manrope-latin-wght-normal.woff2") as font:
        instantiateVariableFont(font, {"wght": weight}, inplace=True)
        units = font["head"].unitsPerEm
        return {code: font["hmtx"][glyph][0] / units for code, glyph in font.getBestCmap().items()}, 1.0


def manrope_text_width(value: str, size: int, weight: int = 400) -> float:
    """Use bundled advances with shaping slack; unsupported glyphs reserve one em."""
    advances, fallback = _manrope_advances(weight)
    return sum(advances.get(ord(char), fallback) for char in value) * size * 1.04
