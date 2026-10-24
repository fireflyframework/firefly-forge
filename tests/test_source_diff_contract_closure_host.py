# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Benign local module changes must invalidate the reviewed streaming contract."""

import ast
from pathlib import Path

import pytest

from firefly_forge import source_budget_allowances as subject

STREAMING_MODULES = (
    "source_snapshot.py",
    "source_diff_admission.py",
    "git_diff_artifacts.py",
    "git_diff_models.py",
    "connection_oauth.py",
    "oauth_credential_fingerprints.py",
    "secrets.py",
    "git_workspace.py",
    "sandbox.py",
    "source_index_sanitation.py",
    "workspace_reconciliation.py",
    "checkpoint_test_models.py",
    "checkpoint_test_sources.py",
    "source_catalog.py",
    "verification_source_preparation.py",
    "verification_models.py",
    "verification_handoffs.py",
)


@pytest.fixture
def modules(tmp_path, monkeypatch):
    syntax = ast.parse(Path(subject.__file__).read_text())
    function = next(n for n in syntax.body if isinstance(n, ast.FunctionDef) and n.name == "admission_contract_digest")
    names = next(
        ast.literal_eval(n)
        for n in ast.walk(function)
        if isinstance(n, ast.Tuple) and all(isinstance(v, ast.Constant) and isinstance(v.value, str) for v in n.elts)
    )
    for name in set(names) | set(STREAMING_MODULES):
        (tmp_path / name).write_text("public local module fixture: " + name + "\n")
    monkeypatch.setattr(subject, "Path", lambda _file: tmp_path / "source_budget_allowances.py")
    return tmp_path


@pytest.mark.parametrize("name", STREAMING_MODULES)
def test_changed_streaming_dependency_invalidates_reviewed_contract(modules, name):
    reviewed = subject.admission_contract_digest()
    module = modules / name
    module.write_text(module.read_text() + "public implementation revision\n")
    assert subject.admission_contract_digest() != reviewed


def test_unrelated_file_does_not_change_the_explicit_admission_contract(modules):
    reviewed = subject.admission_contract_digest()
    (modules / "unrelated_public_note.txt").write_text("public local fixture\n")
    assert subject.admission_contract_digest() == reviewed


def test_missing_required_streaming_module_cannot_produce_an_admission_digest(modules):
    (modules / "source_snapshot.py").unlink()
    with pytest.raises(FileNotFoundError):
        subject.admission_contract_digest()
