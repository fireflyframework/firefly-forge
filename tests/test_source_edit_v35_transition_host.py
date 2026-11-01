# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Current reconciliation requires its own epoch without relabeling prior proof."""

import ast
import hashlib
from dataclasses import replace
from pathlib import Path

import pytest

from firefly_forge import source_edit_qualification as qualification


def test_reconciliation_and_pyfly22_require_a_distinct_epoch():
    assert qualification.REGISTRY_VERSION == 34
    assert qualification.LOADER_PROFILE == "codex-exec-restricted-source/v35"


def test_all_thirty_measured_profile_literals_remain_exact():
    path = Path(qualification.__file__).with_name("source_edit_supported_profiles.py")
    source = path.read_text()
    node = next(
        item
        for item in ast.parse(source).body
        if isinstance(item, ast.AnnAssign) and getattr(item.target, "id", None) == "PROFILES"
    )
    assert len(node.value.elts) >= 30
    prefix = "\0".join(ast.get_source_segment(source, item) for item in node.value.elts[:30])
    assert hashlib.sha256(prefix.encode()).hexdigest() == (
        "f35ebdbb07729bd96810c9b903e755f1b669d94870bf81572f04658a4595cbdf"
    )


@pytest.mark.parametrize("index", range(30))
def test_measured_predecessors_remain_provenance_without_current_authority(index):
    profile = qualification._profiles()[index]
    seal = qualification.historical_profile_seal(profile)
    assert seal["registry_version"] == profile.version == index + 4
    assert seal["loader_profile"] == profile.loader_profile == f"codex-exec-restricted-source/v{index + 5}"
    assert seal["profile_digest"] == qualification.digest(qualification.profile_payload(profile))
    with pytest.raises(ValueError, match="qualified"):
        qualification.require_profile(profile)


def test_measured_v34_cannot_be_relabelled_as_qualified_v35():
    profile = qualification._profiles()[29]
    assert profile.version == 33 and profile.loader_profile == "codex-exec-restricted-source/v34"
    forged = replace(profile, version=qualification.REGISTRY_VERSION, loader_profile=qualification.LOADER_PROFILE)
    with pytest.raises(ValueError, match="registered"):
        qualification.require_profile(forged)
    with pytest.raises(ValueError, match="registered provenance"):
        qualification.historical_profile_seal(forged)
