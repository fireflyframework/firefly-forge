# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""The V11 codec profile and its decoder dependency remain immutable history."""

import ast
import hashlib
from pathlib import Path

import pytest
from test_source_edit_v10_transition_host import HISTORICAL

from firefly_forge import source_edit_qualification as q
from firefly_forge.journal import digest
from firefly_forge.source_budget_allowances import admission_contract_digest


def test_v11_profile_keeps_its_exact_seal_without_current_authority():
    profiles = q._profiles()
    assert len(profiles) in {8, 9}
    for profile in profiles[:7]:
        with pytest.raises(ValueError):
            q.require_profile(profile)
    for profile, (_, _, expected) in zip(profiles[:5], HISTORICAL, strict=True):
        assert q.historical_profile_seal(profile)["profile_digest"] == expected
    assert q.historical_profile_seal(profiles[5])["profile_digest"] == (
        "da8231dc4b6bddb14cde5c619416be63c27ea45b316e02c6a24a84e2fab5777b"
    )
    assert q.historical_profile_seal(profiles[6])["profile_digest"] == (
        "61b578c54ad97e2ded38ce0e1d6a1f145b08c52cdebb0293e6de8b57615623af"
    )


def test_decoder_is_explicit_admission_dependency34_and_every_byte_is_bound(monkeypatch):
    root = Path(q.__file__).parent
    tree = ast.parse((root / "source_budget_allowances.py").read_text())
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "admission_contract_digest")
    names = next(ast.literal_eval(n) for n in ast.walk(fn) if isinstance(n, ast.Tuple) and len(n.elts) >= 33)
    assert len(names[:34]) == len(set(names[:34])) == 34
    assert names[32:34] == ("sandbox_command_slots.py", "agentic_checkpoint_boundary.py")
    before = admission_contract_digest()
    assert before == digest({name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in names})
    read = Path.read_bytes
    monkeypatch.setattr(
        Path,
        "read_bytes",
        lambda p: read(p) + b"\nchanged\n" if p.name == "agentic_checkpoint_boundary.py" else read(p),
    )
    assert admission_contract_digest() != before
