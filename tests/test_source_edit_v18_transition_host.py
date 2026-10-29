# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Original-selection readmission needs fresh V18 source authority."""

import pytest

from firefly_forge import source_edit_qualification as q

SELECTION_ADMISSION_MODULES = (
    "original_selection_controllers.py",
    "original_selection_models.py",
    "original_selection_proof.py",
    "original_selection_readmissions.py",
    "original_selection_runtime.py",
    "configuration.py",
    "model_profiles.py",
    "task_routing.py",
)


def test_v18_preserves_thirteen_historical_profiles_without_current_authority():
    assert q.REGISTRY_VERSION > 16
    assert q.LOADER_PROFILE != "codex-exec-restricted-source/v17"
    profiles = q._profiles()
    assert len(profiles) >= 13
    old = profiles[12]
    assert (old.version, old.loader_profile, old.profile_id) == (
        16,
        "codex-exec-restricted-source/v17",
        "codex-0163-darwin-arm64-source-v17",
    )
    seal = q.historical_profile_seal(old)
    assert seal["profile_digest"] == "56da5abc8b82f62dd3b42887230332b267c408d9a92f69c062e8e54ffb0493bc"
    assert seal["proof_sha256"] == "836a46e45b37d6ced3ef4b6fc77e8cbf461dbce90bd682264afcf8dc0aaec399"
    for profile in profiles[:13]:
        with pytest.raises(ValueError, match="qualified"):
            q.require_profile(profile)


@pytest.mark.parametrize("name", SELECTION_ADMISSION_MODULES)
def test_selection_module_change_invalidates_source_admission(tmp_path, monkeypatch, name):
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
    (tmp_path / name).write_text("Changed authorizing original-selection implementation")
    assert admission.admission_contract_digest() != before


def test_v18_material_inputs_preserve_explicit_selection_dependencies():
    from pathlib import Path

    from test_source_edit_v14_transition_host import declared_names

    root = Path(q.__file__).parent
    admission = declared_names(root / "source_budget_allowances.py", "admission_contract_digest", 71)
    runtime = declared_names(root / "agentic_checkpoint_invocation.py", "runtime_contract", 45)
    assert admission[71:79] == SELECTION_ADMISSION_MODULES
    selection_runtime = (
        "original_selection_readmissions.py",
        "original_selection_models.py",
        "original_selection_proof.py",
        "original_selection_runtime.py",
        "original_selection_controllers.py",
        "configuration.py",
        "model_profiles.py",
        "task_routing.py",
        "verification_handoffs.py",
    )
    assert tuple(name for name in runtime if name in selection_runtime) == selection_runtime
    assert len(admission) == len(set(admission)) >= 79
    assert len(runtime) == len(set(runtime)) >= 54
    if q.REGISTRY_VERSION == 17:
        assert len(admission) == 79 and len(runtime) == 54
    assert len(q.LOADER_MODULES) == len(set(q.LOADER_MODULES)) >= 22
