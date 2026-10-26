# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Versioned preparation preserves historical literal provenance without current authority."""

import ast
import hashlib
from pathlib import Path

import pytest

from firefly_forge import source_edit_qualification as qualification
from firefly_forge import source_edit_supported_profiles as supported


def test_current_streaming_loader_requires_a_new_v9_measurement():
    assert qualification.REGISTRY_VERSION == 18
    assert qualification.LOADER_PROFILE == "codex-exec-restricted-source/v19"
    assert len(qualification.LOADER_MODULES) == 22


def test_three_historical_literals_and_provenance_remain_exact(monkeypatch):
    source = Path(supported.__file__).read_text()
    module = ast.parse(source)
    node = next(
        n
        for n in module.body
        if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name) and n.target.id == "PROFILES"
    )
    actual = [hashlib.sha256(ast.get_source_segment(source, n).encode()).hexdigest() for n in node.value.elts[:3]]
    assert actual == EXPECTED_LITERAL_HASHES
    old = [p for p in qualification._profiles() if p.version in {4, 5, 6}]
    assert [(p.version, p.loader_profile) for p in old] == [
        (4, "codex-exec-restricted-source/v5"),
        (5, "codex-exec-restricted-source/v6"),
        (6, "codex-exec-restricted-source/v7"),
    ]
    for profile in old:
        assert qualification.historical_profile_seal(profile)["proof_sha256"] == profile.proof.artifact_sha256
        with pytest.raises(ValueError):
            qualification.require_profile(profile)
        # One executable can also have a separately qualified current profile.
        with monkeypatch.context() as historical_catalog:
            historical_catalog.setattr(qualification, "_profiles", lambda: tuple(old))
            with pytest.raises(ValueError):
                qualification.qualified_profile(
                    {"sha256": profile.executable_sha256, "size": profile.executable_size}, profile.model
                )


EXPECTED_LITERAL_HASHES = [
    "1a734559ca2868a6f01cdd61ea681cfca63c826007a31f03c869431ca98df425",
    "fee3cf34a6853ef10c38a94f4a6ebdac4731bc90fb8ccd3df1f7748f56f0961a",
    "7c93f951c1f36e7d0b4c111d07424c73e327df1817b1732acc28f8fda23bc8c7",
]
