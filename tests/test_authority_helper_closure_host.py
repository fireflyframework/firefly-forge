# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Delegated authority code must participate in both prospective source contracts."""

import ast
import hashlib
import json
from pathlib import Path

import pytest

from firefly_forge.agentic_checkpoint_invocation import runtime_contract
from firefly_forge.source_budget_allowances import admission_contract_digest

AUTHORITY_MODULES = (
    "knowledge_platform.py",
    "knowledge_ingestion.py",
    "knowledge_queries.py",
    "knowledge_native_sources.py",
    "framework_knowledge_koinon.py",
    "personal_token_controllers.py",
    "personal_tokens.py",
    "personal_token_models.py",
    "personal_token_persistence.py",
    "learning_projection.py",
    "learning_native.py",
    "learning_native_models.py",
    "access_persistence.py",
    "identity.py",
    "identity_persistence.py",
    "product_provenance.py",
    "learning.py",
    "learning_configuration.py",
    "learning_models.py",
    "learning_persistence.py",
    "repair_proposals.py",
    "communication.py",
    "communication_diagnostics.py",
    "collaboration_feedback.py",
    "scm_conversation_models.py",
    "collaboration.py",
    "engine_activity.py",
    "engine_activity_persistence.py",
)
CONTRACTS = (
    (
        runtime_contract,
        "agentic_checkpoint_invocation.py",
        170,
        "86fabd33cc575c6227f84741ce16a5f313ec5e234e8058e3619cc3b434adf2d1",
    ),
    (
        admission_contract_digest,
        "source_budget_allowances.py",
        189,
        "b992675f1701a00501985b183f02d500f59f4cd635a310869cf2ddb97ec36d1c",
    ),
)


def declared_names(filename, function):
    root = Path(__file__).parents[1] / "src/firefly_forge"
    tree = ast.parse((root / filename).read_text())
    owner = next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == function)
    material = next(
        node
        for node in ast.walk(owner)
        if isinstance(node, ast.Tuple)
        and any(isinstance(item, ast.Constant) and item.value == "secrets.py" for item in node.elts)
    )
    return [node.value for node in material.elts]


def validate_materials(names, prefix_length, prefix_digest):
    if len(names) != len(set(names)):
        raise ValueError("Duplicate authority material")
    observed = hashlib.sha256(json.dumps(names[:prefix_length], separators=(",", ":")).encode()).hexdigest()
    if observed != prefix_digest:
        raise ValueError("Historical material prefix changed")
    if not set(AUTHORITY_MODULES) <= set(names):
        raise ValueError("Missing delegated authority material")


@pytest.mark.parametrize("contract,filename,prefix_length,prefix_digest", CONTRACTS)
def test_delegated_materials_are_unique_complete_and_preserve_every_original_entry(
    contract, filename, prefix_length, prefix_digest
):
    validate_materials(declared_names(filename, contract.__name__), prefix_length, prefix_digest)


@pytest.mark.parametrize("contract,filename,prefix_length,prefix_digest", CONTRACTS)
@pytest.mark.parametrize("helper", AUTHORITY_MODULES)
def test_each_delegated_authority_helper_changes_the_actual_contract(
    contract, filename, prefix_length, prefix_digest, helper, monkeypatch
):
    before = contract()
    read = Path.read_bytes

    def changed(path):
        value = read(path)
        return value + b"\n# controlled authority-source mutation" if path.name == helper else value

    monkeypatch.setattr(Path, "read_bytes", changed)
    assert contract() != before


@pytest.mark.parametrize("contract,filename,prefix_length,prefix_digest", CONTRACTS)
@pytest.mark.parametrize(
    "helper", ["knowledge_platform.py", "personal_tokens.py", "learning_native.py", "communication.py"]
)
def test_missing_authority_bytes_cannot_return_an_admission_digest(
    contract, filename, prefix_length, prefix_digest, helper, monkeypatch
):
    read = Path.read_bytes

    def unavailable(path):
        if path.name == helper:
            raise FileNotFoundError("Controlled missing authority source")
        return read(path)

    monkeypatch.setattr(Path, "read_bytes", unavailable)
    with pytest.raises(FileNotFoundError, match="missing authority"):
        contract()


@pytest.mark.parametrize("contract,filename,prefix_length,prefix_digest", CONTRACTS)
@pytest.mark.parametrize("mutation", ["missing", "duplicate"])
def test_material_declaration_regression_rejects_missing_or_duplicate_names(
    contract, filename, prefix_length, prefix_digest, mutation
):
    names = declared_names(filename, contract.__name__)
    names.extend(name for name in AUTHORITY_MODULES if name not in names)
    if mutation == "missing":
        names.remove(AUTHORITY_MODULES[0])
    else:
        names.append(AUTHORITY_MODULES[0])
    with pytest.raises(ValueError, match="Missing" if mutation == "missing" else "Duplicate"):
        validate_materials(names, prefix_length, prefix_digest)
