# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""A bounded prompt window retains all required exact-version reference families."""

import pytest


def test_complete_versioned_families_survive_large_evidence_and_project_hits():
    from firefly_forge.framework_knowledge_evidence import evidence_context

    official = [
        {
            "kind": kind,
            "content": kind.upper() + " " + ("x" * 100_000),
            "corpus_ref": "a" * 32,
            "framework_version": "26.10.11",
            "commit": "b" * 40,
            "source_uri": f"https://example.test/{kind}",
        }
        for kind in ["document", "example", "api"]
    ]
    context = evidence_context(official, [{"content": "project " * 100_000}], budget=10_000, excerpt=1000)
    assert len(context.encode()) <= 10_000
    assert all(kind.upper() in context for kind in ["document", "example", "api"])
    assert "26.10.11" in context and "untrusted" in context.lower()
    with pytest.raises(ValueError, match="window|budget"):
        evidence_context(official, [], budget=100, excerpt=1000)


def test_mixed_framework_corpora_cannot_enter_one_official_prompt_window():
    from firefly_forge.framework_knowledge_evidence import evidence_context

    hits = [
        {
            "kind": kind,
            "content": "current",
            "corpus_ref": str(index) * 32,
            "framework_version": "26.10.11",
            "commit": "b" * 40,
        }
        for index, kind in enumerate(["document", "example", "api"])
    ]
    with pytest.raises(ValueError, match="exact|scope|corpus"):
        evidence_context(hits, [])
