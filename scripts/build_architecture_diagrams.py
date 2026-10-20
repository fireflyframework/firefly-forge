# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
# https://www.apache.org/licenses/LICENSE-2.0
"""Render documentation diagrams with the repository's licensed Manrope font."""

import base64
import html
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "docs/diagrams"
FONT = base64.b64encode((ROOT / "docs/assets/fonts/manrope-latin-wght-normal.woff2").read_bytes()).decode()
LICENSE = html.escape((ROOT / "docs/assets/fonts/OFL.txt").read_text())
PAPER, INK, MUTED, BORDER, ACCENT = "#f3f1eb", "#272820", "#60645b", "#d8d4ca", "#b57921"

# First-party geometry follows the existing local-stack.svg icon vocabulary.
ICONS = {
    "components": '<rect x="1" y="1" width="8" height="8" rx="2"/>'
    '<rect x="15" y="1" width="8" height="8" rx="2"/>'
    '<rect x="8" y="15" width="8" height="8" rx="2"/>'
    '<path d="M5 9v3h14V9M12 12v3"/>',
    "database": '<ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M3 5v14c0 4 18 4 18 0V5M3 12c0 4 18 4 18 0"/>',
    "identity": '<path d="M12 2 3 6v6c0 5 5 8 9 10 4-2 9-5 9-10V6Z"/><path d="m8 12 3 3 5-6"/>',
}

DIAGRAMS = {
    "architecture-components": {
        "icon": "components",
        "title": "Native services behind the factory",
        "subtitle": "Clients submit contracts; the execution leader owns effects.",
        "cards": [
            ("Operator surfaces", "Studio, CLI, ForgeClient and 162 factory MCP tools use authenticated contracts."),
