# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Version targets come from immutable dependency manifests, never moving workspace text."""

import json

import pytest


def test_exact_lock_targets_support_parallel_versions_and_refuse_ambiguous_ranges():
    from firefly_forge.framework_knowledge_targets import manifest_target

    assert manifest_target("pyfly", {"uv.lock": b'[[package]]\nname="pyfly"\nversion="26.10.7"\n'}) == "26.10.7"
    assert (
        manifest_target("pyfly", {"pyproject.toml": b'[project]\ndependencies=["pyfly[data]==26.10.11"]\n'})
        == "26.10.11"
    )
    assert (
        manifest_target(
            "larafly",
            {
                "composer.lock": json.dumps(
                    {"packages": [{"name": "fireflyframework/larafly", "version": "v26.10.4"}]}
                ).encode()
            },
        )
        == "26.10.4"
    )
    for files in (
        {},
        {"pyproject.toml": b'[project]\ndependencies=["pyfly>=26.10.1"]'},
        {
            "uv.lock": b'[[package]]\nname="pyfly"\nversion="26.10.7"\n',
            "pyproject.toml": b'[project]\ndependencies=["pyfly==26.10.11"]',
        },
    ):
        with pytest.raises(ValueError, match="exact|ambiguous|conflict"):
            manifest_target("pyfly", files)


async def test_git_manifest_reads_bind_original_commit_despite_dirty_workspace(tmp_path):
    import subprocess

    from firefly_forge.framework_knowledge_targets import git_manifests

    def git(*args):
        return subprocess.check_output(["git", *args], cwd=tmp_path).decode().strip()

    git("init", "-q")
    git("config", "user.name", "Fixture")
    git("config", "user.email", "fixture@example.test")
    (tmp_path / "pyproject.toml").write_text('[project]\ndependencies=["pyfly==26.10.7"]\n')
    git("add", "pyproject.toml")
    git("commit", "-qm", "Original target")
    commit = git("rev-parse", "HEAD")
    (tmp_path / "pyproject.toml").write_text('[project]\ndependencies=["pyfly==26.10.11"]\n')
    values = await git_manifests(tmp_path, commit, "pyfly", ".")
    assert b"26.10.7" in values["pyproject.toml"] and b"26.10.11" not in values["pyproject.toml"]
    with pytest.raises(ValueError):
        await git_manifests(tmp_path, "HEAD", "pyfly", ".")


def test_framework_upgrade_is_explicit_and_legacy_input_wire_stays_unchanged():
    from pydantic import ValidationError

    from firefly_forge.models import RunInput

    assert "framework_target" not in RunInput(repository_id="a" * 32, brief="Build").model_dump()
    value = RunInput(
        repository_id="a" * 32,
        brief="Upgrade",
        operation="maintain",
        framework_target={"framework_version": "26.10.11", "upgrade_from": "26.10.7"},
    )
    assert value.framework_target.upgrade_from == "26.10.7"
    with pytest.raises(ValidationError):
        RunInput(
            repository_id="a" * 32,
            brief="Build",
            framework_target={"framework_version": "26.10.11", "upgrade_from": "26.10.7"},
        )


def test_maintenance_uses_frozen_manifest_and_explicit_upgrade_matches_original():
    from firefly_forge.framework_knowledge_targets import select_target

    files = {"uv.lock": b'[[package]]\nname="pyfly"\nversion="26.10.7"\n'}
    assert select_target("pyfly", "maintain", files, None) == ("26.10.7", None)
    assert select_target("pyfly", "build", {}, None) == ("26.10.11", None)
    assert select_target("pyfly", "maintain", files, {"framework_version": "26.10.11", "upgrade_from": "26.10.7"}) == (
        "26.10.11",
        "26.10.7",
    )
    with pytest.raises(ValueError):
        select_target("pyfly", "maintain", files, {"framework_version": "26.10.11", "upgrade_from": None})
    with pytest.raises(ValueError):
        select_target("pyfly", "maintain", files, {"framework_version": "26.10.11", "upgrade_from": "26.10.6"})


def test_framework_api_check_uses_actual_installed_exports_and_exact_version(tmp_path):
    import importlib.metadata
    import os
    import subprocess
    import sys

    from firefly_forge.framework_knowledge_conformance import conformance_commands

    version = importlib.metadata.version("pyfly")
    (tmp_path / "service.py").write_text("from pyfly.container import service\n")
    argv = conformance_commands({"framework": "pyfly", "framework_version": version})[0]
    argv = argv[2:]  # Existing retained Python stands in for the product's uv environment.
    positive = subprocess.run(
        [sys.executable, *argv[1:]], cwd=tmp_path, env=os.environ.copy(), capture_output=True, text=True
    )
    assert positive.returncode == 0, positive.stderr
    (tmp_path / "service.py").write_text("from pyfly.container import ImaginaryServiceExport\n")
    negative = subprocess.run(
        [sys.executable, *argv[1:]], cwd=tmp_path, env=os.environ.copy(), capture_output=True, text=True
    )
    assert negative.returncode != 0 and "ImaginaryServiceExport" in negative.stderr
    mismatch = conformance_commands({"framework": "pyfly", "framework_version": "1.0.0"})[0][2:]
    assert (
        subprocess.run(
            [sys.executable, *mismatch[1:]], cwd=tmp_path, env=os.environ.copy(), capture_output=True
        ).returncode
        != 0
    )


def test_build_never_silently_upgrades_existing_framework_declarations():
    from firefly_forge.framework_knowledge_targets import select_target

    existing = {"pyproject.toml": b'[project]\ndependencies=["pyfly==26.10.7"]'}
    with pytest.raises(ValueError, match="maintain|upgrade"):
        select_target("pyfly", "build", existing, None, supported_default="26.10.11")
    assert select_target(
        "pyfly", "build", {"pyproject.toml": b'[project]\nname="new-product"'}, None, supported_default="26.10.11"
    ) == ("26.10.11", None)
    ambiguous = {"pyproject.toml": b'[project]\ndependencies=["pyfly>=26.10.1"]'}
    with pytest.raises(ValueError, match="exact|ambiguous"):
        select_target("pyfly", "build", ambiguous, None, supported_default="26.10.11")
