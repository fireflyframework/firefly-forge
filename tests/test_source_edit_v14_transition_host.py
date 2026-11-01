# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""V14 binds the whole CRM continuation union without inheriting historical authority."""

import ast
import hashlib
from pathlib import Path

import pytest

from firefly_forge import source_edit_qualification as qualification
from firefly_forge.agentic_checkpoint_invocation import runtime_contract
from firefly_forge.source_budget_allowances import admission_contract_digest

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
)

HISTORICAL_PROFILE_SEALS = (
    "17320f82c0f2f9ab4b65c7e3bd8b89741a3716a8896dd5f674f6409ac1490c3c",
    "20e13d34f87314b12fa9a25a0c6411e75e8eb70083a083d743c7fa5487cff76a",
    "28f152b48aa9ff37c940ae300fd47f4af5ecc0053860b1980b79fa39a20886b6",
    "ab6b1582f5d364e9341e5d25e0f8dd343c3033d0f2ed885e025f7b38ebb64a97",
    "4c7d2eba0af54ab48ef33fbe2018e797fcb5cb7adc41a9846722c22dd944f3c8",
    "da8231dc4b6bddb14cde5c619416be63c27ea45b316e02c6a24a84e2fab5777b",
    "61b578c54ad97e2ded38ce0e1d6a1f145b08c52cdebb0293e6de8b57615623af",
    "eb5be5fec427f70580264fc9a391626d5bdaf6c207818271aca71f72e6466cc7",
    "14c9378d8b7ca4b6274500a92ecd82fc4204dafbd1ed78ac10cf65f510378028",
)

EXPECTED_ADMISSION_NAMES = (
    "source_budget_allowances.py",
    "source_budget_output.py",
    "source_tool_budget.py",
    "source_human_readmission.py",
    "source_result_readmission.py",
    "store.py",
    "native_question_owned_unit.py",
    "provider_recoveries.py",
    "workspace_reconciliations.py",
    "human_clarifications.py",
    "journal.py",
    "source_edit_continuations.py",
    "tool_broker.py",
    "engine.py",
    "source_edit_qualification.py",
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
    "sandbox_command_slots.py",
    "agentic_checkpoint_boundary.py",
    "checkpoint_source_identity.py",
    "agentic_runtime.py",
    "agentic_checkpoint_invocation.py",
    "agent_checkpoint_models.py",
    "agent_checkpoints.py",
    "bridge_completions.py",
    "bridge_completion_models.py",
    "known_worker_completion_models.py",
    "known_worker_completion_proof.py",
    "known_worker_completions.py",
    "native_terminal_completion_models.py",
    "native_terminal_completions.py",
    "native_terminal_restore_proof.py",
    "agent_restores.py",
    "execution_requests.py",
    "execution_request_persistence.py",
    "project_snapshot.py",
    "project_models.py",
    "projects.py",
    "project_persistence.py",
    "models.py",
    "agent_roles.py",
    "project_access.py",
    "credential_text_stream.py",
    "unknown_command_acknowledgements.py",
    "unknown_command_models.py",
    "unknown_command_proof.py",
    "unknown_command_controllers.py",
    "scm_authority_models.py",
    "git_deliveries.py",
)

EXPECTED_RUNTIME_NAMES = (
    "agentic_runtime.py",
    "agent_restores.py",
    "agent_checkpoints.py",
    "agentic_checkpoint_boundary.py",
    "agentic_checkpoint_invocation.py",
    "checkpoint_source_identity.py",
    "source_snapshot.py",
    "source_diff_admission.py",
    "git_workspace.py",
    "workspace_tools.py",
    "providers.py",
    "sandbox.py",
    "sandbox_command_slots.py",
    "journal.py",
    "runner_pool_worker.py",
    "sandbox_models.py",
    "sandbox_persistence.py",
    "runner_backends.py",
    "runner_factory_models.py",
    "runner_pool_models.py",
    "engine.py",
    "known_worker_completion_models.py",
    "known_worker_completion_proof.py",
    "known_worker_completions.py",
    "native_terminal_completion_models.py",
    "native_terminal_completions.py",
    "native_terminal_restore_proof.py",
    "agent_checkpoint_models.py",
    "bridge_completion_models.py",
    "bridge_completions.py",
    "execution_requests.py",
    "execution_request_persistence.py",
    "unknown_command_acknowledgements.py",
    "unknown_command_models.py",
    "unknown_command_proof.py",
    "unknown_command_controllers.py",
    "scm_authority_models.py",
    "git_deliveries.py",
)

CAPACITY_ADMISSION_NAMES = (
    "project_snapshot.py",
    "project_models.py",
    "projects.py",
    "project_persistence.py",
    "models.py",
    "agent_roles.py",
    "project_access.py",
    "credential_text_stream.py",
)

NEW_ADMISSION_NAMES = (
    "agentic_runtime.py",
    "agentic_checkpoint_invocation.py",
    "agent_checkpoint_models.py",
    "agent_checkpoints.py",
    "bridge_completions.py",
    "bridge_completion_models.py",
    "known_worker_completion_models.py",
    "known_worker_completion_proof.py",
    "known_worker_completions.py",
    "native_terminal_completion_models.py",
    "native_terminal_completions.py",
    "native_terminal_restore_proof.py",
    "agent_restores.py",
    "execution_requests.py",
    "execution_request_persistence.py",
    "project_snapshot.py",
    "project_models.py",
    "projects.py",
    "project_persistence.py",
    "models.py",
    "agent_roles.py",
    "project_access.py",
    "credential_text_stream.py",
    "unknown_command_acknowledgements.py",
    "unknown_command_models.py",
    "unknown_command_proof.py",
    "unknown_command_controllers.py",
    "scm_authority_models.py",
    "git_deliveries.py",
)


def _function(tree, name):
    matches = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == name]
    if len(matches) != 1:
        raise ValueError("One exact local function required")
    return matches[0]


def _no_arguments(node):
    args = node.args
    return not (
        args.posonlyargs
        or args.args
        or args.kwonlyargs
        or args.vararg
        or args.kwarg
        or args.defaults
        or args.kw_defaults
        or node.decorator_list
    )


def _call(node, name):
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == name
        and not node.args
        and not node.keywords
    )


def _dictionaries(owner, key):
    return [
        value
        for node in ast.walk(owner)
        if isinstance(node, ast.Dict)
        for item, value in zip(node.keys, node.values, strict=True)
        if isinstance(item, ast.Constant) and item.value == key
    ]


def _module_names(path, function, dictionary_key=None):
    tree = ast.parse(path.read_text())
    owner = _function(tree, function)
    if function == "runtime_contract" and dictionary_key == "adapter" and not _dictionaries(owner, dictionary_key):
        if (
            not _no_arguments(owner)
            or len(owner.body) != 2
            or not isinstance(owner.body[0], ast.Expr)
            or not _call(owner.body[0].value, "_outside_unit")
            or not isinstance(owner.body[1], ast.Return)
            or not _call(owner.body[1].value, "_runtime_material_digest")
        ):
            raise ValueError("Only the exact reviewed guarded runtime delegation is supported")
        owner = _function(tree, "_runtime_material_digest")
        if not _no_arguments(owner):
            raise ValueError("The reviewed private runtime helper must remain local and argument-free")
    if dictionary_key is not None:
        owners = _dictionaries(owner, dictionary_key)
        if len(owners) != 1:
            raise ValueError("One exact runtime adapter dictionary required")
        owner = owners[0]
    values = [
        ast.literal_eval(node)
        for node in ast.walk(owner)
        if isinstance(node, ast.Tuple)
        and node.elts
        and all(
            isinstance(item, ast.Constant) and isinstance(item.value, str) and item.value.endswith(".py")
            for item in node.elts
        )
    ]
    if len(values) != 1:
        raise ValueError("One exact literal module closure required")
    return list(values[0])


def declared_names(path, function, minimum):
    values = _module_names(path, function, "adapter" if function == "runtime_contract" else None)
    if len(values) < minimum or len(values) != len(set(values)):
        raise ValueError("The exact module closure must retain its minimum and unique members")
    return tuple(values)


def test_v14_has_distinct_markers_and_nine_exact_historical_profiles_without_authority():
    assert qualification.REGISTRY_VERSION > 12
    assert qualification.LOADER_PROFILE != "codex-exec-restricted-source/v13"
    profiles = qualification._profiles()
    assert len(profiles) >= len(HISTORICAL_PROFILE_SEALS)
    assert [(p.version, p.loader_profile) for p in profiles[:9]] == [
        (version, f"codex-exec-restricted-source/v{version + 1}") for version in range(4, 13)
    ]
    for profile, fingerprint in zip(profiles[:9], HISTORICAL_PROFILE_SEALS, strict=True):
        assert qualification.historical_profile_seal(profile)["profile_digest"] == fingerprint
        with pytest.raises(ValueError):
            qualification.require_profile(profile)


def test_all_nine_historical_literal_source_segments_remain_byte_exact():
    source = Path(qualification.__file__).with_name("source_edit_supported_profiles.py").read_text()
    node = next(
        n
        for n in ast.parse(source).body
        if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name) and n.target.id == "PROFILES"
    )
    assert len(node.value.elts) >= len(HISTORICAL_LITERAL_HASHES)
    assert (
        tuple(hashlib.sha256(ast.get_source_segment(source, item).encode()).hexdigest() for item in node.value.elts[:9])
        == HISTORICAL_LITERAL_HASHES
    )


def test_final_material_dependencies_are_explicit_without_losing_historical_prefixes():
    root = Path(qualification.__file__).parent
    admission = declared_names(root / "source_budget_allowances.py", "admission_contract_digest", 35)
    runtime = declared_names(root / "agentic_checkpoint_invocation.py", "_runtime_material_digest", 21)
    mode_admission = (
        "checkpoint_mode_adoptions.py",
        "checkpoint_mode_controllers.py",
        "checkpoint_mode_custody.py",
        "checkpoint_mode_handoff_custody.py",
        "checkpoint_mode_models.py",
        "checkpoint_mode_proof.py",
        "checkpoint_mode_runtime.py",
    )
    mode_runtime = (
        "checkpoint_mode_adoptions.py",
        "checkpoint_mode_models.py",
        "checkpoint_mode_proof.py",
        "checkpoint_mode_runtime.py",
        "checkpoint_mode_custody.py",
        "checkpoint_mode_handoff_custody.py",
        "checkpoint_mode_controllers.py",
    )
    protected_admission = (*EXPECTED_ADMISSION_NAMES, *mode_admission)
    protected_runtime = (*EXPECTED_RUNTIME_NAMES[:-2], *mode_runtime, *EXPECTED_RUNTIME_NAMES[-2:])
    assert admission[: len(protected_admission)] == protected_admission
    assert tuple(name for name in runtime if name in protected_runtime) == protected_runtime
    assert len(admission) == len(set(admission)) >= len(protected_admission) == 71
    assert len(runtime) == len(set(runtime)) >= len(protected_runtime) == 45
    assert len(qualification.LOADER_MODULES) == len(set(qualification.LOADER_MODULES)) >= 22


@pytest.mark.parametrize("name", NEW_ADMISSION_NAMES)
def test_each_new_authorizing_module_mutation_revokes_its_material_contract(monkeypatch, name):
    before_admission, before_runtime, before_loader = (
        admission_contract_digest(),
        runtime_contract(),
        qualification.loader_hashes(),
    )
    read = Path.read_bytes
    monkeypatch.setattr(
        Path, "read_bytes", lambda path: read(path) + b"\nchanged\n" if path.name == name else read(path)
    )
    assert admission_contract_digest() != before_admission
    if name not in CAPACITY_ADMISSION_NAMES:
        assert runtime_contract() != before_runtime
    assert qualification.loader_hashes() == before_loader


GUARDED_CLOSURE = (
    "def runtime_contract() -> str:\n    _outside_unit()\n    return _runtime_material_digest()\n\n"
    'def _runtime_material_digest() -> str:\n    return {"adapter": ("one.py", "two.py")}\n'
)


@pytest.mark.parametrize(
    "source", [GUARDED_CLOSURE, 'def runtime_contract():\n    return {"adapter": ("one.py", "two.py")}\n']
)
def test_declared_names_preserves_exact_legacy_and_guarded_module_order(tmp_path, source):
    path = tmp_path / "source.py"
    path.write_text(source)
    assert declared_names(path, "runtime_contract", 2) == ("one.py", "two.py")


@pytest.mark.parametrize(
    "source",
    [
        GUARDED_CLOSURE.replace("    _outside_unit()\n", ""),
        GUARDED_CLOSURE.replace("    _outside_unit()\n", "    different_guard()\n"),
        GUARDED_CLOSURE.replace("_outside_unit()", "_outside_unit(1)"),
        GUARDED_CLOSURE.replace("return _runtime_material_digest()", "return different_helper()"),
        GUARDED_CLOSURE.replace("return _runtime_material_digest()", "return _runtime_material_digest(1)"),
        GUARDED_CLOSURE.replace("return _runtime_material_digest()", "return _runtime_material_digest(flag=True)"),
        GUARDED_CLOSURE.replace("    _outside_unit()\n", "    _outside_unit()\n    mutate()\n"),
        GUARDED_CLOSURE.replace("def runtime_contract()", "def runtime_contract(flag=None)"),
        GUARDED_CLOSURE.replace("def _runtime_material_digest()", "def _runtime_material_digest(flag=None)"),
        GUARDED_CLOSURE + '\ndef _runtime_material_digest():\n    return {"adapter": ("foreign.py",)}\n',
        GUARDED_CLOSURE + '\ndef runtime_contract():\n    return {"adapter": ("foreign.py",)}\n',
        GUARDED_CLOSURE.replace('("one.py", "two.py")', 'tuple(["one.py", "two.py"])'),
        GUARDED_CLOSURE.replace('("one.py", "two.py")', '("one.py", "one.py")'),
        GUARDED_CLOSURE.replace('return {"adapter":', 'other = {"adapter": ("foreign.py",)}\n    return {"adapter":'),
    ],
)
def test_declared_names_refuses_unreviewed_or_ambiguous_runtime_delegation(tmp_path, source):
    path = tmp_path / "source.py"
    path.write_text(source)
    with pytest.raises(ValueError):
        declared_names(path, "runtime_contract", 2)


def test_declared_names_retains_the_selected_minimum(tmp_path):
    path = tmp_path / "source.py"
    path.write_text(GUARDED_CLOSURE)
    with pytest.raises(ValueError, match="minimum"):
        declared_names(path, "runtime_contract", 3)
