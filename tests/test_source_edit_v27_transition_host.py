# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""New runner protocols retain all qualified core profiles as immutable history."""

import ast
import hashlib
from dataclasses import replace
from pathlib import Path

import pytest

from firefly_forge import source_edit_qualification as qualification


def test_future_runner_namespace_cannot_reuse_the_core_v26_measurement():
    assert qualification.REGISTRY_VERSION >= 26
    if qualification.REGISTRY_VERSION == 26:
        assert qualification.LOADER_PROFILE == "codex-exec-restricted-source/v27"
    else:
        assert qualification.LOADER_PROFILE != "codex-exec-restricted-source/v27"


def test_all_twenty_two_core_profile_literals_remain_exact():
    source = Path(qualification.__file__).with_name("source_edit_supported_profiles.py").read_text()
    node = next(
        node
        for node in ast.parse(source).body
        if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", None) == "PROFILES"
    )
    assert len(node.value.elts) >= 22
    prefix = "\0".join(ast.get_source_segment(source, item) for item in node.value.elts[:22])
    assert (
        hashlib.sha256(prefix.encode()).hexdigest()
        == "3c1bc49c47e29236875d31b72513917f0d17af2dd34c58077a2ae7ea456ebdee"
    )


@pytest.mark.parametrize("index", range(22))
def test_every_retained_core_profile_is_historical_without_current_authority(index):
    profile = qualification._profiles()[index]
    seal = qualification.historical_profile_seal(profile)
    assert seal["registry_version"] == profile.version == index + 4
    assert seal["loader_profile"] == profile.loader_profile == f"codex-exec-restricted-source/v{index + 5}"
    assert seal["profile_digest"] == qualification.digest(qualification.profile_payload(profile))
    with pytest.raises(ValueError, match="qualified"):
        qualification.require_profile(profile)


def test_registered_core_v26_proof_cannot_be_relabelled_as_future_authority():
    profile = qualification._profiles()[21]
    assert profile.proof.artifact_sha256 == "6d44904c32279f4d8889c8ac2d6c64a1dfda984755784be5f140a87c1011ce74"
    with pytest.raises(ValueError, match="registered provenance"):
        qualification.historical_profile_seal(
            replace(profile, version=qualification.REGISTRY_VERSION, loader_profile=qualification.LOADER_PROFILE)
        )
