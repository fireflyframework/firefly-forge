# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""The reviewed PyFly successor cannot acquire an older runtime's measured authority."""

import ast
import hashlib
from dataclasses import replace
from pathlib import Path

import pytest

from firefly_forge import source_edit_qualification as qualification


def test_pyfly21_successor_requires_a_distinct_source_epoch():
    assert qualification.REGISTRY_VERSION == 33
    assert qualification.LOADER_PROFILE == "codex-exec-restricted-source/v34"


def test_all_twenty_nine_measured_profile_literals_remain_exact():
    path = Path(qualification.__file__).with_name("source_edit_supported_profiles.py")
    source = path.read_text()
    node = next(
        item
        for item in ast.parse(source).body
        if isinstance(item, ast.AnnAssign) and getattr(item.target, "id", None) == "PROFILES"
    )
    assert len(node.value.elts) in (29, 30)
    prefix = "\0".join(ast.get_source_segment(source, item) for item in node.value.elts[:29])
    assert (
        hashlib.sha256(prefix.encode()).hexdigest()
        == "8d37e55cc93bb8a9d6e2156c44bc1a2d62aa94d55973ad7c04af54711c58e562"
    )


@pytest.mark.parametrize("index", range(29))
def test_all_measured_predecessors_remain_historical_without_current_authority(index):
    profile = qualification._profiles()[index]
    seal = qualification.historical_profile_seal(profile)
    assert seal["registry_version"] == profile.version == index + 4
    assert seal["loader_profile"] == profile.loader_profile == f"codex-exec-restricted-source/v{index + 5}"
    assert seal["profile_digest"] == qualification.digest(qualification.profile_payload(profile))
    with pytest.raises(ValueError, match="qualified"):
        qualification.require_profile(profile)


def test_measured_v33_cannot_be_relabelled_as_a_qualified_v34():
    profile = qualification._profiles()[28]
    assert profile.version == 32 and profile.loader_profile == "codex-exec-restricted-source/v33"
    forged = replace(profile, version=qualification.REGISTRY_VERSION, loader_profile=qualification.LOADER_PROFILE)
    with pytest.raises(ValueError, match="registered"):
        qualification.require_profile(forged)
    with pytest.raises(ValueError, match="registered provenance"):
        qualification.historical_profile_seal(forged)
