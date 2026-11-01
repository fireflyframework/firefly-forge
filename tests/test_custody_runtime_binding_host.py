# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Changed custody or execution-authority code invalidates the measured cut."""

from pathlib import Path

import pytest

from firefly_forge import agentic_checkpoint_invocation, linked_verifications, source_budget_allowances

MODULES = [
    "object_storage.py",
    "runner_artifact_publications.py",
    "runner_object_artifacts.py",
    "source_archive.py",
    "source_archive_index.py",
    "source_archive_scan_models.py",
    "source_archive_service.py",
    "source_explorer.py",
    "coding_check_grants.py",
    "coding_check_models.py",
    "git_linked_verification.py",
    "scm_linked_verification.py",
    "scm_linked_verification_models.py",
    "scm_linked_verification_records.py",
    "workspace_tools.py",
]


@pytest.mark.parametrize("name", MODULES)
@pytest.mark.parametrize("boundary", ["runtime", "admission"])
def test_changed_custody_authority_requires_a_new_measured_cut(name, boundary, monkeypatch):
    fingerprint = (
        agentic_checkpoint_invocation.runtime_contract
        if boundary == "runtime"
        else source_budget_allowances.admission_contract_digest
    )
    target = Path(agentic_checkpoint_invocation.__file__).parent / name
    original = Path.read_bytes
    before = fingerprint()

    def changed(path):
        data = original(path)
        return data + b"\n# Controlled changed authority implementation\n" if path == target else data

    with monkeypatch.context() as mutation:
        mutation.setattr(Path, "read_bytes", changed)
        assert fingerprint() != before
    assert fingerprint() == before


@pytest.mark.parametrize(
    "name", ["scm_linked_verification.py", "scm_linked_verification_models.py", "scm_linked_verification_records.py"]
)
def test_changed_scm_consent_requires_a_new_verification_policy(name, monkeypatch):
    target = Path(linked_verifications.__file__).parent / name
    original = Path.read_bytes
    before = linked_verifications._policy_material_digest()

    def changed(path):
        data = original(path)
        return data + b"\n# Controlled changed consent implementation\n" if path == target else data

    with monkeypatch.context() as mutation:
        mutation.setattr(Path, "read_bytes", changed)
        assert linked_verifications._policy_material_digest() != before
    assert linked_verifications._policy_material_digest() == before
