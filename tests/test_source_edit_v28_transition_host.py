# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""A new post-final epoch preserves every registered predecessor without reusing authority."""

import ast
import hashlib
from dataclasses import replace
from pathlib import Path

import pytest

from firefly_forge import source_edit_qualification as qualification


def test_post_final_verification_requires_a_new_loader_epoch():
    assert qualification.REGISTRY_VERSION >= 29
    if qualification.REGISTRY_VERSION == 29:
        assert qualification.LOADER_PROFILE == "codex-exec-restricted-source/v30"


def test_all_twenty_five_registered_literals_remain_exact():
    source = Path(qualification.__file__).with_name("source_edit_supported_profiles.py").read_text()
    node = next(
        node
        for node in ast.parse(source).body
        if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", None) == "PROFILES"
    )
    assert len(node.value.elts) in (25, 26)
    prefix = "\0".join(ast.get_source_segment(source, item) for item in node.value.elts[:25])
    assert (
        hashlib.sha256(prefix.encode()).hexdigest()
        == "b7b2a5034b3bf0ef750b1e631819f56285574151421a272723fa08926a7ebd10"
    )


@pytest.mark.parametrize("index", range(25))
def test_registered_predecessor_has_only_immutable_historical_authority(index):
    profile = qualification._profiles()[index]
    seal = qualification.historical_profile_seal(profile)
    assert seal["registry_version"] == profile.version == index + 4
    assert seal["loader_profile"] == profile.loader_profile == f"codex-exec-restricted-source/v{index + 5}"
    assert seal["profile_digest"] == qualification.digest(qualification.profile_payload(profile))
    with pytest.raises(ValueError, match="qualified"):
        qualification.require_profile(profile)


def test_registered_v29_proof_cannot_be_relabelled_as_current():
    profile = qualification._profiles()[24]
    assert profile.version == 28 and profile.loader_profile == "codex-exec-restricted-source/v29"
    with pytest.raises(ValueError, match="registered provenance"):
        qualification.historical_profile_seal(
            replace(profile, version=qualification.REGISTRY_VERSION, loader_profile=qualification.LOADER_PROFILE)
        )
