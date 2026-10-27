# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Diagram image assets must never enter Flycanon text normalization."""

import pytest

from firefly_forge.framework_knowledge_upstream import source_kind


@pytest.mark.parametrize(
    "path",
    [
        "book/art/figures/12-saga.svg",
        "docs/assets/diagrams/method-interceptor-chain.svg",
        "examples/reference/architecture.svg",
        "docs/assets/diagram.SVG",
    ],
)
def test_svg_images_are_excluded_from_the_official_text_corpus(path):
    assert source_kind(path) is None


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("docs/modules/dependency-injection.md", "document"),
        ("docs/diagrams/container.mmd", "document"),
        ("src/pyfly/container/registry.py", "api"),
        ("packages/container/src/Container.php", "api"),
        ("examples/container/service.py", "example"),
    ],
)
def test_reference_asset_filter_preserves_text_diagrams_docs_api_and_examples(path, expected):
    assert source_kind(path) == expected
