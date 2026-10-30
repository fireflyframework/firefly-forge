# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""A new post-final epoch preserves every registered predecessor without reusing authority."""

import ast
import hashlib
from dataclasses import replace
from pathlib import Path

import pytest

from firefly_forge import source_edit_qualification as qualification


def test_post_final_verification_requires_a_new_loader_epoch():
    assert qualification.REGISTRY_VERSION == 28
    assert qualification.LOADER_PROFILE == "codex-exec-restricted-source/v29"


def test_all_twenty_four_registered_literals_remain_exact():
    source = Path(qualification.__file__).with_name("source_edit_supported_profiles.py").read_text()
    node = next(
        node
        for node in ast.parse(source).body
        if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", None) == "PROFILES"
    )
    assert len(node.value.elts) in (24, 25)
    prefix = "\0".join(ast.get_source_segment(source, item) for item in node.value.elts[:24])
    assert (
        hashlib.sha256(prefix.encode()).hexdigest()
        == "95993bc735d70a7003b711a445297dbb9eb6785a9aee3e2907853c303f1d6cac"
    )


@pytest.mark.parametrize("index", range(24))
def test_registered_predecessor_has_only_immutable_historical_authority(index):
    profile = qualification._profiles()[index]
    seal = qualification.historical_profile_seal(profile)
    assert seal["registry_version"] == profile.version == index + 4
    assert seal["loader_profile"] == profile.loader_profile == f"codex-exec-restricted-source/v{index + 5}"
    assert seal["profile_digest"] == qualification.digest(qualification.profile_payload(profile))
    with pytest.raises(ValueError, match="qualified"):
        qualification.require_profile(profile)


def test_registered_v28_proof_cannot_be_relabelled_as_current():
    profile = qualification._profiles()[23]
    assert profile.version == 27 and profile.loader_profile == "codex-exec-restricted-source/v28"
    with pytest.raises(ValueError, match="registered provenance"):
        qualification.historical_profile_seal(
            replace(profile, version=qualification.REGISTRY_VERSION, loader_profile=qualification.LOADER_PROFILE)
        )
