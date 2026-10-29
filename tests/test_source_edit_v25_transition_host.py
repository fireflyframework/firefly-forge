# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Checkpoint continuation requires fresh authority and retains all prior measurements."""

import ast
import hashlib
from dataclasses import replace
from pathlib import Path

import pytest
from test_source_edit_v14_transition_host import declared_names

from firefly_forge import source_edit_qualification as q
from firefly_forge.agentic_checkpoint_invocation import runtime_contract
from firefly_forge.source_budget_allowances import admission_contract_digest

CHECKPOINT_ADMISSION_MODULES = (
    "checkpoint_continuations.py",
    "checkpoint_continuation_proof.py",
    "checkpoint_continuation_models.py",
    "checkpoint_continuation_controllers.py",
)
HISTORICAL_LITERAL_HASHES = (
    "1a734559ca2868a6f01cdd61ea681cfca63c826007a31f03c869431ca98df425",
    "fee3cf34a6853ef10c38a94f4a6ebdac4731bc90fb8ccd3df1f7748f56f0961a",
    "7c93f951c1f36e7d0b4c111d07424c73e327df1817b1732acc28f8fda23bc8c7",
    "c917f87856c2a9318f3b7e292e2bb5e94958aa1973db90748822d39379ae72b2",
    "781ced480f13044383068988b2287c0c7f9edead39dc899c658bf05008876d10",
    "ece092f39404041ded98469da7daabfea785ea4aac13c5bd3298611bd2869975",
    "ea4cf6b3d6d027d078d4983adb521b0dd0a11e9a8b257d8d012d4660f0f16b77",
    "a42df6838e21f574d2764788f8d151b2a0f1ca541da1da28ddd7aedb984ab720",
    "7791027fb6990d6430d1775ac967857fac9119e032535e7d4645c0bfe77f24a1",
    "5c570b5b3c41b917f3f1d18a5302a0f943367df0411da9ebe88d78e8d73dd813",
    "709f8f22be99dc007f418125075c380c3da4e32381ea21dece5840fc0d563bcc",
    "de1b0ad34a866d5f37331194cd1f4c7ee1d7ffd2dd758f551c9e75a82e6eb09a",
    "b26625fd26d4871494986328254be70a54e41c806e093dff400e99725450b69e",
    "b627c19e119244d9a430b7bbe63211689a7c8c08882862a8132e89cf3d9c97e3",
    "5f33b9495b863f48e7d8cb3591340712705e4fbb4bbf399ffe6e8c2872b14fd4",
    "ef4002d50907643efd173023e06348588087c6bcf87548fc82682f013dfbdbd9",
    "676383f959b133470517b823869a2e922ef7f905f6caa9b8495f8198f2b2a642",
    "ce68e56e9e3c02d3694cb2c98cfd1a1619a47c99750d4d13319be9bcb88f6a8a",
    "f48887a1281f0061f6a8f92b62ad95147191db1ee1181d95a48ee28d5be5558c",
    "ebf81cccd8149e07dbe6c2ff5bc79bbae5c098dba5fc168d0a1556a3001f17ab",
)


def test_v25_has_distinct_measurement_markers():
    assert q.REGISTRY_VERSION >= 24
    if q.REGISTRY_VERSION == 24:
        assert q.LOADER_PROFILE == "codex-exec-restricted-source/v25"
    else:
        assert q.LOADER_PROFILE != "codex-exec-restricted-source/v25"


def test_v24_measurement_is_historical_without_current_authority():
    old = next(p for p in q._profiles() if p.loader_profile == "codex-exec-restricted-source/v24")
    seal = q.historical_profile_seal(old)
    assert seal["registry_version"] == 23
    assert seal["profile_digest"] == "9a362fb2496541547ae30f3680f21b6c423b75bf9865b644990792e3efa68014"
    assert seal["proof_sha256"] == "357409068e28a4033292c49295da042ec4c94b71cfa159a17be15f81796c9c17"
    assert old.proof.contract_digest == "7c43af783e4501b48571df89286537da3092eb3edac79ef032990904e32e150b"
    with pytest.raises(ValueError, match="qualified"):
        q.require_profile(old)
    with pytest.raises(ValueError, match="registered provenance"):
        q.historical_profile_seal(replace(old, loader_profile=q.LOADER_PROFILE))


def test_all_twenty_historical_literal_segments_remain_exact():
    source = Path(q.__file__).with_name("source_edit_supported_profiles.py").read_text()
    node = next(
        n
        for n in ast.parse(source).body
        if isinstance(n, ast.AnnAssign) and getattr(n.target, "id", None) == "PROFILES"
    )
    assert len(node.value.elts) >= 20
    assert (
        tuple(
            hashlib.sha256(ast.get_source_segment(source, item).encode()).hexdigest() for item in node.value.elts[:20]
        )
        == HISTORICAL_LITERAL_HASHES
    )
    assert [(p.version, p.loader_profile) for p in q._profiles()[:20]] == [
        (v, f"codex-exec-restricted-source/v{v + 1}") for v in range(4, 24)
    ]


def test_checkpoint_authority_modules_are_explicit_material_inputs():
    root = Path(q.__file__).parent
    admission = declared_names(root / "source_budget_allowances.py", "admission_contract_digest", 35)
    runtime = declared_names(root / "agentic_checkpoint_invocation.py", "runtime_contract", 21)
    assert admission[-len(CHECKPOINT_ADMISSION_MODULES) :] == CHECKPOINT_ADMISSION_MODULES
    for name in (*CHECKPOINT_ADMISSION_MODULES, "pending_checkpoint_reconstruction.py"):
        assert admission.count(name) == runtime.count(name) == 1
    assert len(q.LOADER_MODULES) == len(set(q.LOADER_MODULES)) == 22


@pytest.mark.parametrize("name", CHECKPOINT_ADMISSION_MODULES)
def test_each_checkpoint_authority_mutation_revokes_runtime_and_admission(monkeypatch, name):
    before_runtime, before_admission, before_loader = runtime_contract(), admission_contract_digest(), q.loader_hashes()
    read = Path.read_bytes
    monkeypatch.setattr(
        Path, "read_bytes", lambda path: read(path) + b"\nchanged\n" if path.name == name else read(path)
    )
    assert runtime_contract() != before_runtime
    assert admission_contract_digest() != before_admission
    assert q.loader_hashes() == before_loader
