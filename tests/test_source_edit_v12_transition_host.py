# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Complete-source enumeration and paging require a fresh measured native profile."""

import ast
import hashlib
from pathlib import Path

import pytest

from firefly_forge import source_edit_qualification as q
from firefly_forge.journal import digest
from firefly_forge.source_budget_allowances import admission_contract_digest

HISTORICAL_SEALS = (
    "17320f82c0f2f9ab4b65c7e3bd8b89741a3716a8896dd5f674f6409ac1490c3c",
    "20e13d34f87314b12fa9a25a0c6411e75e8eb70083a083d743c7fa5487cff76a",
    "28f152b48aa9ff37c940ae300fd47f4af5ecc0053860b1980b79fa39a20886b6",
    "ab6b1582f5d364e9341e5d25e0f8dd343c3033d0f2ed885e025f7b38ebb64a97",
    "4c7d2eba0af54ab48ef33fbe2018e797fcb5cb7adc41a9846722c22dd944f3c8",
    "da8231dc4b6bddb14cde5c619416be63c27ea45b316e02c6a24a84e2fab5777b",
    "61b578c54ad97e2ded38ce0e1d6a1f145b08c52cdebb0293e6de8b57615623af",
)


def _declared_admission_names():
    tree = ast.parse(Path(q.__file__).with_name("source_budget_allowances.py").read_text())
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "admission_contract_digest")
    return next(ast.literal_eval(n) for n in ast.walk(fn) if isinstance(n, ast.Tuple) and len(n.elts) >= 34)


def test_v12_retains_its_history_and_keeps_seven_earlier_profiles():
    profiles = q._profiles()
    assert len(profiles) in {10, 11}
    assert [(p.version, p.loader_profile) for p in profiles[:7]] == [
        (n, f"codex-exec-restricted-source/v{n + 1}") for n in range(4, 11)
    ]
    for p, expected in zip(profiles[:7], HISTORICAL_SEALS, strict=True):
        assert q.historical_profile_seal(p)["profile_digest"] == digest(q.profile_payload(p)) == expected
        with pytest.raises(ValueError):
            q.require_profile(p)


def test_source_identity_helper_is_explicit_in_loader_and_admission():
    names = _declared_admission_names()
    assert len(names) == len(set(names)) and len(names) >= 35
    assert names[32:35] == (
        "sandbox_command_slots.py",
        "agentic_checkpoint_boundary.py",
        "checkpoint_source_identity.py",
    )
    assert len(q.LOADER_MODULES) == len(set(q.LOADER_MODULES)) == 22
    assert q.LOADER_MODULES[-1] == "checkpoint_source_identity.py"
    root = Path(q.__file__).parent
    assert admission_contract_digest() == digest(
        {n: hashlib.sha256((root / n).read_bytes()).hexdigest() for n in names}
    )


def test_new_source_prompt_versions_are_selected_without_rewriting_history():
    names = q.LOADER_MODULES
    for family in ("list", "search"):
        assert f"resources/prompts/v1/coding-runtime/source-{family}-1.0.2.yaml" in names
        assert f"resources/prompts/v1/coding-runtime/source-{family}-1.0.1.yaml" not in names
    assert "resources/prompts/v1/coding-runtime/source-read-1.0.1.yaml" in names


def test_source_helper_mutation_invalidates_both_current_contracts(monkeypatch):
    assert "checkpoint_source_identity.py" in _declared_admission_names()
    before_admission, before_loader = admission_contract_digest(), q.loader_hashes()
    read = Path.read_bytes
    monkeypatch.setattr(
        Path, "read_bytes", lambda p: read(p) + b"\nchanged\n" if p.name == "checkpoint_source_identity.py" else read(p)
    )
    assert admission_contract_digest() != before_admission
    assert q.loader_hashes() != before_loader


@pytest.mark.parametrize("name", ["workspace_tools.py", "checkpoint_source_identity.py"])
def test_retained_graph_contract_binds_source_inventory_and_selection_bytes(monkeypatch, name):
    from firefly_forge.agentic_checkpoint_invocation import runtime_contract

    before = runtime_contract()
    read = Path.read_bytes
    monkeypatch.setattr(Path, "read_bytes", lambda p: read(p) + b"\nchanged\n" if p.name == name else read(p))
    assert runtime_contract() != before
