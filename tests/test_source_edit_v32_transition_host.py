# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Neutral unrequested QA requires a fresh epoch without rebinding measured evidence."""

import ast
import hashlib
from dataclasses import replace
from pathlib import Path

import pytest

from firefly_forge import source_edit_qualification as qualification


def test_neutral_unrequested_qa_requires_a_distinct_source_epoch():
    assert qualification.REGISTRY_VERSION >= 31
    if qualification.REGISTRY_VERSION == 31:
        assert qualification.LOADER_PROFILE == "codex-exec-restricted-source/v32"
    else:
        assert qualification.LOADER_PROFILE != "codex-exec-restricted-source/v32"


def test_all_twenty_seven_measured_profile_literals_remain_exact():
    source = Path(qualification.__file__).with_name("source_edit_supported_profiles.py").read_text()
    node = next(
        item
        for item in ast.parse(source).body
        if isinstance(item, ast.AnnAssign) and getattr(item.target, "id", None) == "PROFILES"
    )
    assert len(node.value.elts) >= 27
    prefix = "\0".join(ast.get_source_segment(source, item) for item in node.value.elts[:27])
    assert (
        hashlib.sha256(prefix.encode()).hexdigest()
        == "e2b2d3bf8518a38f92b395879778c00f4775d9c761b838db9cace827a4430767"
    )


@pytest.mark.parametrize("index", range(27))
def test_measured_predecessors_remain_provenance_without_current_recovery_authority(index):
    profile = qualification._profiles()[index]
    seal = qualification.historical_profile_seal(profile)
    assert seal["registry_version"] == profile.version == index + 4
    assert seal["loader_profile"] == profile.loader_profile == f"codex-exec-restricted-source/v{index + 5}"
    assert seal["profile_digest"] == qualification.digest(qualification.profile_payload(profile))
    with pytest.raises(ValueError, match="qualified"):
        qualification.require_profile(profile)


def test_measured_v31_profile_cannot_be_relabelled_as_current_recovery_authority():
    profile = qualification._profiles()[26]
    assert profile.version == 30 and profile.loader_profile == "codex-exec-restricted-source/v31"
    forged = replace(profile, version=qualification.REGISTRY_VERSION, loader_profile=qualification.LOADER_PROFILE)
    with pytest.raises(ValueError, match="registered"):
        qualification.require_profile(forged)
    with pytest.raises(ValueError, match="registered provenance"):
        qualification.historical_profile_seal(forged)
