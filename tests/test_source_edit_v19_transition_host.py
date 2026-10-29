# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Generation-bound recovery requires distinct measured source authority."""

import pytest

from firefly_forge import source_edit_qualification as q


def test_v19_preserves_fourteen_historical_profiles_without_current_authority():
    assert q.REGISTRY_VERSION > 17
    assert q.LOADER_PROFILE != "codex-exec-restricted-source/v18"
    profiles = q._profiles()
    assert len(profiles) >= 14
    old = profiles[13]
    assert (old.version, old.loader_profile, old.profile_id) == (
        17,
        "codex-exec-restricted-source/v18",
        "codex-0163-darwin-arm64-source-v18",
    )
    seal = q.historical_profile_seal(old)
    assert seal["profile_digest"] == "b365edb48e94142840888092c82384e92ce7d3bcab58b38f6da1bb9c4c25d1f9"
    assert seal["proof_sha256"] == "6027b08c74e2057cda79f1decd20971a1035db28c34e5ec8b4e21bacad837533"
    for profile in profiles[:14]:
        with pytest.raises(ValueError, match="qualified"):
            q.require_profile(profile)


def test_v19_recovery_repository_is_an_explicit_material_input():
    from pathlib import Path

    from test_source_edit_v14_transition_host import declared_names

    root = Path(q.__file__).parent
    admission = declared_names(root / "source_budget_allowances.py", "admission_contract_digest", 79)
    runtime = declared_names(root / "agentic_checkpoint_invocation.py", "runtime_contract", 54)
    assert "persistence.py" in admission and "persistence.py" in runtime
    assert len(admission) == len(set(admission)) >= 80
    assert len(runtime) == len(set(runtime)) >= 55
    if q.REGISTRY_VERSION == 18:
        assert admission[79] == runtime[54] == "persistence.py"
        assert len(admission) == 80 and len(runtime) == 55
    assert len(q.LOADER_MODULES) == len(set(q.LOADER_MODULES)) >= 22
