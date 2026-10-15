# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
# https://www.apache.org/licenses/LICENSE-2.0
"""Qualify generated assets, embedded font rights and the documented MCP count."""

import ast
import re
import unittest
from xml.etree import ElementTree

from scripts import build_architecture_diagrams as diagrams

NS = {"svg": "http://www.w3.org/2000/svg"}


class ArchitectureDiagramTests(unittest.TestCase):
    def test_assets_match_renderer_and_retain_accessible_self_contained_rights(self):
        for name, diagram in diagrams.DIAGRAMS.items():
            for narrow in (False, True):
                with self.subTest(diagram=name, narrow=narrow):
                    filename = name + ("-narrow" if narrow else "") + ".svg"
                    actual = (diagrams.OUTPUT / filename).read_text()
                    expected = (
                        diagrams.sequence(diagram)
                        if "steps" in diagram and not narrow
                        else diagrams.cards(diagram, narrow)
                    )
                    self.assertEqual(actual, expected, "Refresh stale diagram assets")
                    svg = ElementTree.fromstring(actual)
                    self.assertEqual(svg.get("role"), "img")
                    self.assertTrue(svg.find("svg:title", NS).text)
                    self.assertTrue(svg.find("svg:desc", NS).text)
                    self.assertIn("SIL OPEN FONT LICENSE", svg.find("svg:metadata", NS).text)
                    self.assertIn("data:font/woff2;base64,", actual)
                    self.assertFalse(svg.findall(".//svg:script", NS))
                    self.assertFalse(svg.findall(".//svg:foreignObject", NS))
                    self.assertFalse(any(key.startswith("on") for node in svg.iter() for key in node.attrib))
        self.assertEqual(
            (diagrams.OUTPUT / "architecture.svg").read_bytes(),
            (diagrams.OUTPUT / "architecture-components.svg").read_bytes(),
        )

    def test_standalone_learning_assets_identify_pending_contracts(self):
        for name in ("learning-lookup", "learning-feedback"):
            for suffix in ("", "-narrow"):
                asset = ElementTree.fromstring((diagrams.OUTPUT / (name + suffix + ".svg")).read_text())
                self.assertIn("under implementation", asset.find("svg:desc", NS).text)

    def test_documented_operator_tool_count_matches_actual_registrations(self):
        source = ast.parse((diagrams.ROOT / "src/firefly_forge/mcp_server.py").read_text())
        tools = [
            node
            for node in ast.walk(source)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and any(
                isinstance(decorator, ast.Call)
                and isinstance(decorator.func, ast.Attribute)
                and decorator.func.attr == "tool"
                and isinstance(decorator.func.value, ast.Name)
                and decorator.func.value.id == "server"
                for decorator in node.decorator_list
            )
        ]
        architecture = (diagrams.ROOT / "docs/architecture.md").read_text()
        documented = re.search(r"\| Factory MCP \| (\d+) authenticated operator tools", architecture)
        self.assertIsNotNone(documented, "Document the authenticated factory MCP surface")
        self.assertEqual(len(tools), int(documented.group(1)), "Update the documented factory MCP surface")
        self.assertIn(f"{len(tools)} factory MCP tools", diagrams.DIAGRAMS["architecture-components"]["cards"][0][1])
        prerequisites = (diagrams.ROOT / "docs/tooling-prerequisites.md").read_text()
        self.assertIn(f"has {len(tools)} operator tools", prerequisites)


if __name__ == "__main__":
    unittest.main()
