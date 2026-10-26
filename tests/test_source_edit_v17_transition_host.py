# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Checkpoint-mode adoption requires distinct measured V17 admission authority."""

import pytest

from firefly_forge import source_edit_qualification as q


def test_v17_preserves_twelve_historical_profiles_without_current_authority():
    assert q.REGISTRY_VERSION > 15
    assert q.LOADER_PROFILE != "codex-exec-restricted-source/v16"
    profiles = q._profiles()
    assert len(profiles) >= 12
    old = profiles[11]
    assert (old.version, old.loader_profile, old.profile_id) == (
        15,
        "codex-exec-restricted-source/v16",
        "codex-0163-darwin-arm64-source-v16",
    )
    seal = q.historical_profile_seal(old)
    assert seal["profile_digest"] == "c169f88b575b40b063c8c474e45f5457c22a04ba5996fab1361b234bec7dbecc"
    assert seal["proof_sha256"] == "d84eeb3e7cb25a4cfd87ddb2311fe126fa9a2e6ab7d06c469b9dbe28a3624e0e"
    for profile in profiles[:12]:
        with pytest.raises(ValueError, match="qualified"):
            q.require_profile(profile)


@pytest.mark.parametrize(
    "name",
    [
        "checkpoint_mode_adoptions.py",
        "checkpoint_mode_controllers.py",
        "checkpoint_mode_custody.py",
        "checkpoint_mode_handoff_custody.py",
        "checkpoint_mode_models.py",
        "checkpoint_mode_proof.py",
        "checkpoint_mode_runtime.py",
    ],
)
def test_mode_module_change_invalidates_source_admission(tmp_path, monkeypatch, name):
    import ast
    from pathlib import Path

    from firefly_forge import source_budget_allowances as admission

    module = ast.parse(Path(admission.__file__).read_text())
    function = next(
        node for node in module.body if isinstance(node, ast.FunctionDef) and node.name == "admission_contract_digest"
    )
    dependencies = next(
        ast.literal_eval(node)
        for node in ast.walk(function)
        if isinstance(node, ast.Tuple)
        and node.elts
        and all(isinstance(value, ast.Constant) and isinstance(value.value, str) for value in node.elts)
    )
    for dependency in (*dependencies, name):
        (tmp_path / dependency).write_text("Controlled admission input: " + dependency)
    monkeypatch.setattr(admission, "__file__", str(tmp_path / "source_budget_allowances.py"))
    before = admission.admission_contract_digest()
    (tmp_path / name).write_text("Changed authorizing mode implementation")
    assert admission.admission_contract_digest() != before
