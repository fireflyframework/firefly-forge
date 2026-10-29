# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""The tools successor needs fresh authority without rewriting prior measurements."""

import ast
import hashlib
from dataclasses import replace
from pathlib import Path

import pytest
from test_source_edit_v25_transition_host import HISTORICAL_LITERAL_HASHES

from firefly_forge import source_edit_qualification as q


def test_v26_requires_a_distinct_measurement_generation():
    assert q.REGISTRY_VERSION >= 25
    if q.REGISTRY_VERSION == 25:
        assert q.LOADER_PROFILE == "codex-exec-restricted-source/v26"
    else:
        assert q.LOADER_PROFILE != "codex-exec-restricted-source/v26"


def test_v25_retains_historical_provenance_without_current_authority():
    old = next(profile for profile in q._profiles() if profile.loader_profile == "codex-exec-restricted-source/v25")
    seal = q.historical_profile_seal(old)
    assert seal["registry_version"] == 24
    assert seal["profile_digest"] == "2e0a0bee0fd12e4c3772c5b3f96081c1978c85ff442e453543713ca29f62e1af"
    assert seal["proof_sha256"] == "09a8df6fedf67ad2890b7f0d27ffefe15dbb34c7603f471a14621b84c56e11cd"
    assert old.proof.contract_digest == "71bcdbcda62d6425541842f44edbb3a219c9b03470820daa76499469481b093c"
    with pytest.raises(ValueError, match="qualified"):
        q.require_profile(old)
    with pytest.raises(ValueError, match="qualified"):
        q.source_budget_limits(old)
    with pytest.raises(ValueError, match="registered provenance"):
        q.historical_profile_seal(replace(old, loader_profile=q.LOADER_PROFILE))


def test_all_twenty_one_historical_literal_segments_remain_exact():
    source = Path(q.__file__).with_name("source_edit_supported_profiles.py").read_text()
    node = next(
        node
        for node in ast.parse(source).body
        if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", None) == "PROFILES"
    )
    assert len(node.value.elts) >= 21
    expected = (*HISTORICAL_LITERAL_HASHES, "b957f6e4f466a12d4e4fdb968897e9a786a3ffe1cf77cbf67a2dd2da91d5c9fd")
    assert (
        tuple(
            hashlib.sha256(ast.get_source_segment(source, item).encode()).hexdigest() for item in node.value.elts[:21]
        )
        == expected
    )
    assert [(profile.version, profile.loader_profile) for profile in q._profiles()[:21]] == [
        (version, f"codex-exec-restricted-source/v{version + 1}") for version in range(4, 25)
    ]


@pytest.mark.parametrize("index", range(21))
def test_every_retained_profile_refuses_current_admission(index):
    profile = q._profiles()[index]
    assert q.historical_profile_seal(profile)["proof_sha256"] == profile.proof.artifact_sha256
    with pytest.raises(ValueError, match="qualified"):
        q.require_profile(profile)
