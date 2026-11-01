# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""A reviewed Vitest physical override preserves logical scope and selected inputs."""

import json

import pytest
from test_checkpoint_source_identity_host import git, repository

from firefly_forge import engine, verification_scope


@pytest.fixture
def vitest_workspace(tmp_path):
    root = tmp_path / "project"
    (root / "frontend/node_modules/vitest").mkdir(parents=True)
    (root / "frontend/package.json").write_text(
        json.dumps({"scripts": {"test": "node node_modules/vitest/vitest.mjs run --configLoader runner"}})
    )
    (root / "frontend/node_modules/vitest/package.json").write_text(json.dumps({"name": "vitest", "version": "4.1.11"}))
    (root / "frontend/node_modules/vitest/vitest.mjs").write_text("// Controlled manifest fixture; not executed\n")
    return root


def test_qualified_no_cache_plan_is_visible_and_never_rewrites_logical_command(vitest_workspace):
    argv = ["/tools/npm", "--prefix", "frontend", "run", "test"]
    physical = verification_scope.vitest_no_cache_argv(argv, vitest_workspace)
    assert physical == [*argv, "--", "--no-cache"]
    assert argv == ["/tools/npm", "--prefix", "frontend", "run", "test"]
    assert verification_scope.wrap_build_argv(physical)[3:] == physical
    build = ["/tools/npm", "--prefix", "frontend", "run", "build"]
    assert verification_scope.vitest_no_cache_argv(build, vitest_workspace) == build


@pytest.mark.parametrize("tool", ["ruff", "mypy"])
def test_isolated_python_tool_module_is_qualified_without_allowing_arbitrary_code(vitest_workspace, tool):
    for flags in (["-I"], ["-B", "-I"], ["-I", "-B"]):
        assert (
            verification_scope.build_check_kind(["python", *flags, "-m", tool, "--version"], vitest_workspace)
            == "python_static"
        )
    for tail in (["-I", "-c", "import ruff"], ["-I", "-m", "foreign"], ["-I", "-I", "-m", tool]):
        assert verification_scope.build_check_kind(["python", *tail], vitest_workspace) is None


@pytest.mark.parametrize(
    "change", ["version", "tool", "pretest", "bundle_loader", "shell", "missing_entry", "foreign_prefix"]
)
def test_unknown_version_or_script_is_refused_before_any_qa_effect(vitest_workspace, change):
    root = vitest_workspace
    pkg = root / "frontend/package.json"
    value = json.loads(pkg.read_text())
    if change in {"version", "tool"}:
        (root / "frontend/node_modules/vitest/package.json").write_text(
            json.dumps(
                {
                    "name": "foreign" if change == "tool" else "vitest",
                    "version": "4.0.0" if change == "version" else "4.1.11",
                }
            )
        )
    elif change == "pretest":
        value["scripts"]["pretest"] = "node other.js"
    elif change == "bundle_loader":
        value["scripts"]["test"] = "node node_modules/vitest/vitest.mjs run"
    elif change == "shell":
        value["scripts"]["test"] += " && node extra.js"
    elif change == "missing_entry":
        (root / "frontend/node_modules/vitest/vitest.mjs").unlink()
    pkg.write_text(json.dumps(value))
    argv = ["npm", "--prefix", "../foreign" if change == "foreign_prefix" else "frontend", "run", "test"]
    with pytest.raises(ValueError, match="Vitest|cache|script|scope"):
        verification_scope.vitest_no_cache_argv(argv, root)


def test_fresh_workspace_excludes_generated_cache_but_keeps_tracked_cache_selected(tmp_path):
    root = repository(tmp_path)
    (root / ".cache").mkdir()
    (root / ".cache/tracked.txt").write_text("Retained supplied file\n")
    git(root, "add", ".cache/tracked.txt")
    git(root, "commit", "-qm", "Freeze supplied cache-named source")
    engine._write_workspace_exclusions(root)
    (root / ".cache/results.json").write_text("Controlled generated output\n")
    assert ".cache/tracked.txt" in git(root, "ls-files")
    assert ".cache/results.json" not in git(root, "ls-files", "--others", "--exclude-standard")


async def test_successor_build_scope_qualifies_full_effective_commands_without_changing_legacy(
    vitest_workspace, monkeypatch
):
    from types import SimpleNamespace

    from firefly_forge import source_budget_allowances, verification_models
    from firefly_forge import verification_scope_attestation as attestation
    from firefly_forge.sandbox_models import SandboxInput, capture_sandbox

    async def current():
        pass

    async def location(_):
        return None

    async def dependency(*args, **kwargs):
        return SimpleNamespace(digest="a" * 64, assert_current=current)

    monkeypatch.setattr(attestation, "current_native_location", location)
    monkeypatch.setattr(attestation, "runtime_contract", lambda: "b" * 64)
    monkeypatch.setattr(attestation, "runtime_environment", lambda: {"PATH": "/tools:/usr/bin:/bin"})
    monkeypatch.setattr(attestation, "_roots", lambda *args: [vitest_workspace / "frontend/node_modules"])
    monkeypatch.setattr(attestation, "prepare_dependency_capture", dependency)
    monkeypatch.setattr(attestation, "prepare_public_source_cut", dependency)
    monkeypatch.setattr(source_budget_allowances, "admission_contract_digest", lambda: "c" * 64)
    monkeypatch.setattr(
        verification_models, "verification_build_declarations", lambda run: [SimpleNamespace(command_digest="d" * 64)]
    )
    store = SimpleNamespace(
        path=vitest_workspace.parent / "state" / "store.json",
        configuration=SimpleNamespace(settings_generation=object(), load_snapshot=lambda _: {}),
    )
    store.path.parent.mkdir(mode=0o700)
    run = dict(
        id="e" * 32,
        commands=[dict(argv=["legacy-check"], timeout=10)],
        config_revision="fixture",
        invalid_read_continuity=dict(request_id="f" * 32),
        sandbox_snapshot=capture_sandbox(SandboxInput()),
    )
    intent = dict(
        id="1" * 32,
        workspace=str(vitest_workspace),
        source_digest="2" * 64,
        commands_digest="3" * 64,
        scope_digest="4" * 64,
        reusable_build_declaration_digest="5" * 64,
    )
    journal = SimpleNamespace(source_catalog_budget=None)
    engine_case = SimpleNamespace(sandbox=SimpleNamespace(credential_registry=dict))
    assert (
        await attestation.prepare_build_scope(store, journal, engine_case, run, intent, dict(digest="6" * 64), current)
        is None
    )
    commands = [dict(argv=["npm", "--prefix", "frontend", "run", "test"], timeout=10)]
    prepared = await attestation.prepare_build_scope(
        store,
        journal,
        engine_case,
        run,
        intent,
        dict(digest="6" * 64),
        current,
        scratch_identity="7" * 64,
        output_policy="vitest_no_cache_v1",
        fresh_commands=commands,
    )
    assert prepared is not None
    assert prepared.witness.checks[0].argv == commands[0]["argv"]
    assert prepared.witness.checks[0].wrapped_argv[-7:] == [*commands[0]["argv"], "--", "--no-cache"]
    assert prepared.witness.commands_digest == attestation.digest({"commands": commands})
    assert run["commands"] == [dict(argv=["legacy-check"], timeout=10)]
