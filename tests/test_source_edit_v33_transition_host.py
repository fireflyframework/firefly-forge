# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Native checkpoint compatibility cannot inherit a measured predecessor's authority."""

import ast
import hashlib
from pathlib import Path

import pytest

from firefly_forge import source_edit_qualification as qualification


def test_native_checkpoint_compatibility_requires_a_distinct_source_epoch():
    assert qualification.REGISTRY_VERSION >= 32
    if qualification.REGISTRY_VERSION == 32:
        assert qualification.LOADER_PROFILE == "codex-exec-restricted-source/v33"
    else:
        assert qualification.LOADER_PROFILE != "codex-exec-restricted-source/v33"


def test_all_twenty_eight_measured_profile_literals_remain_exact():
    source = Path(qualification.__file__).with_name("source_edit_supported_profiles.py").read_text()
    node = next(
        item
        for item in ast.parse(source).body
        if isinstance(item, ast.AnnAssign) and getattr(item.target, "id", None) == "PROFILES"
    )
    assert len(node.value.elts) >= 28
    prefix = "\0".join(ast.get_source_segment(source, item) for item in node.value.elts[:28])
    assert (
        hashlib.sha256(prefix.encode()).hexdigest()
        == "b8586809b91a2fa545af08ed65339d886eeb600cdf2e23516195303ac6a400ae"
    )


@pytest.mark.parametrize("index", range(28))
def test_measured_predecessors_remain_provenance_without_current_recovery_authority(index):
    profile = qualification._profiles()[index]
    seal = qualification.historical_profile_seal(profile)
    assert seal["registry_version"] == profile.version == index + 4
    assert seal["loader_profile"] == profile.loader_profile == f"codex-exec-restricted-source/v{index + 5}"
    assert seal["profile_digest"] == qualification.digest(qualification.profile_payload(profile))
    with pytest.raises(ValueError, match="qualified"):
        qualification.require_profile(profile)
