# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Native knowledge/identity source uses a new epoch without rebinding Root4 evidence."""

import ast
import hashlib
from dataclasses import replace
from pathlib import Path

import pytest

from firefly_forge import source_edit_qualification as qualification


def test_native_knowledge_identity_requires_a_distinct_source_epoch():
    assert qualification.REGISTRY_VERSION == 30
    assert qualification.LOADER_PROFILE == "codex-exec-restricted-source/v31"


def test_all_twenty_six_measured_profile_literals_remain_exact():
    source = Path(qualification.__file__).with_name("source_edit_supported_profiles.py").read_text()
    node = next(
        item
        for item in ast.parse(source).body
        if isinstance(item, ast.AnnAssign) and getattr(item.target, "id", None) == "PROFILES"
    )
    assert len(node.value.elts) >= 26
    prefix = "\0".join(ast.get_source_segment(source, item) for item in node.value.elts[:26])
    assert (
        hashlib.sha256(prefix.encode()).hexdigest()
        == "bba2641e4f219a4784f506fbd21b2000dbec729ed8dcf76a4170f1d434b533cc"
    )


@pytest.mark.parametrize("index", range(26))
def test_measured_predecessors_remain_provenance_and_cannot_grant_native_union_authority(index):
    profile = qualification._profiles()[index]
    seal = qualification.historical_profile_seal(profile)
    assert seal["registry_version"] == profile.version == index + 4
    assert seal["loader_profile"] == profile.loader_profile == f"codex-exec-restricted-source/v{index + 5}"
    assert seal["profile_digest"] == qualification.digest(qualification.profile_payload(profile))
    with pytest.raises(ValueError, match="qualified"):
        qualification.require_profile(profile)


def test_root4_profile_cannot_be_relabelled_as_prospective_native_authority():
    profile = qualification._profiles()[25]
    assert profile.version == 29 and profile.loader_profile == "codex-exec-restricted-source/v30"
    forged = replace(profile, version=qualification.REGISTRY_VERSION, loader_profile=qualification.LOADER_PROFILE)
    with pytest.raises(ValueError, match="registered"):
        qualification.require_profile(forged)
    with pytest.raises(ValueError, match="registered provenance"):
        qualification.historical_profile_seal(forged)
