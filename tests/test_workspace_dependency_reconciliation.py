# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Real owned Git repositories; no model, service, credential or application repair."""

import importlib
import importlib.util
import os
import subprocess

import pytest
from test_bridge_inflight_cancel import RUN, MemoryStore

from firefly_forge.engine import Engine
from firefly_forge.git_workspace import metadata_digest


def git(workspace, *args):
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(workspace.parent),
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    return subprocess.check_output(
        ["/usr/bin/git", "-c", "core.hooksPath=" + os.devnull, *args],
        cwd=workspace,
        env=env,
        text=True,
        stderr=subprocess.PIPE,
        timeout=10,
    ).strip()


@pytest.fixture
def legacy_workspace(tmp_path):
    root = tmp_path / "workspaces" / RUN
    root.mkdir(parents=True)
    git(root, "init", "--quiet")
    git(root, "config", "user.name", "Fictional source author")
    git(root, "config", "user.email", "fixture@example.test")
    git(root, "remote", "add", "origin", "fictional-source-only")
    (root / "README.md").write_text("Original public source\n")
    git(root, "add", "README.md")
    git(root, "commit", "--quiet", "-m", "Create public fixture baseline")
    baseline = git(root, "rev-parse", "HEAD")
    (root / ".git/info/exclude").write_text("node_modules/\n.venv/\nvendor/\n")
    metadata = metadata_digest(root)
    external = tmp_path / "retained-public-dependencies"
    external.mkdir()
    (external / "marker").write_text("Retained public dependency\n")
    (root / "frontend").mkdir()
    (root / "frontend/app.js").write_text("export const value = 1;\n")
    (root / "frontend/node_modules").symlink_to(external, target_is_directory=True)
    git(root, "add", "--all", "--", ".")
    assert metadata_digest(root) == metadata
    return root, baseline, metadata, external


def implementation():
    assert importlib.util.find_spec("firefly_forge.workspace_reconciliation") is not None, (
        "Native environment reconciliation is unavailable"
    )
    return importlib.import_module("firefly_forge.workspace_reconciliation")


async def test_legacy_retained_workspace_refuses_alias_before_any_provider_dispatch(legacy_workspace, tmp_path):
    root, baseline, metadata, external = legacy_workspace
    store = MemoryStore(tmp_path / "memory")
    store.run.data.update(base_revision=baseline, git_metadata_digest=metadata, resume_existing=True)
    calls = []

    class Worker:
        async def execute(self, *args):
            calls.append(args)
            raise AssertionError("The refused preparation cannot start a provider")

    engine = Engine(store, root.parent, lambda _: Worker())
    await engine.execute_run(RUN)
    assert not calls and store.run.status == "failed"
    assert "private credentials or worker metadata" in store.run.data["error"]
    assert git(root, "ls-files", "--stage", "--", "frontend/node_modules").startswith("120000 ")
    assert (root / "frontend/node_modules").is_symlink()
    assert (external / "marker").read_text() == "Retained public dependency\n"


def test_readonly_dependency_plan_binds_only_the_legacy_staged_alias(legacy_workspace):
    root, baseline, metadata, _ = legacy_workspace
    index = (root / ".git/index").read_bytes()
    excluded = (root / ".git/info/exclude").read_bytes()
    plan = implementation().dependency_plan(root, baseline, metadata, component=".")
    assert plan["aliases"] == ["frontend/node_modules"]
    assert plan["metadata_before"] == metadata
    assert plan["base_revision"] == baseline
    assert (root / ".git/index").read_bytes() == index
    assert (root / ".git/info/exclude").read_bytes() == excluded


def test_native_environment_delta_preserves_source_targets_head_and_private_guard(legacy_workspace):
    root, baseline, metadata, external = legacy_workspace
    module = implementation()
    plan = module.dependency_plan(root, baseline, metadata, component=".")
    before = (root / "frontend/app.js").read_bytes()
    alias = (root / "frontend/node_modules").lstat()
    result = module.apply_dependency_plan(root, plan)
    assert result["metadata_before"] == metadata
    assert result["metadata_after"] == metadata_digest(root) != metadata
    assert git(root, "rev-parse", "HEAD") == baseline
    assert git(root, "ls-files", "--", "frontend/node_modules") == ""
    assert "node_modules" in (root / ".git/info/exclude").read_text().splitlines()
    assert (root / "frontend/node_modules").lstat().st_ino == alias.st_ino
    assert (root / "frontend/app.js").read_bytes() == before
    assert (external / "marker").read_text() == "Retained public dependency\n"


@pytest.mark.parametrize("changed", ["index", "public_source", "alias", "metadata"])
def test_environment_delta_refuses_drift_before_any_native_write(legacy_workspace, changed):
    root, baseline, metadata, _ = legacy_workspace
    module = implementation()
    plan = module.dependency_plan(root, baseline, metadata, component=".")
    if changed == "index":
        git(root, "update-index", "--force-remove", "--", "frontend/node_modules")
    elif changed == "public_source":
        (root / "frontend/app.js").write_text("export const value = 2;\n")
    elif changed == "alias":
        (root / "frontend/node_modules").unlink()
        (root / "frontend/node_modules").symlink_to(root.parent, target_is_directory=True)
    else:
        git(root, "config", "core.filemode", "false")
    index = (root / ".git/index").read_bytes()
    exclude = (root / ".git/info/exclude").read_bytes()
    with pytest.raises(ValueError):
        module.apply_dependency_plan(root, plan)
    assert (root / ".git/index").read_bytes() == index
    assert (root / ".git/info/exclude").read_bytes() == exclude


@pytest.mark.parametrize("changed", ["tracked", "regular", "modern_policy", "metadata", "component", "symlink_parent"])
def test_dependency_plan_cannot_hide_private_source_or_change_scope(legacy_workspace, changed):
    root, baseline, metadata, external = legacy_workspace
    component = "."
    if changed == "tracked":
        git(root, "commit", "--quiet", "-m", "Fixture with a forbidden tracked alias")
        baseline = git(root, "rev-parse", "HEAD")
    elif changed == "regular":
        (root / "frontend/node_modules").unlink()
        (root / "frontend/node_modules").write_text("Private-path regular file must stay refused\n")
        git(root, "add", "--all", "--", ".")
    elif changed == "modern_policy":
        (root / ".git/info/exclude").write_text("node_modules\n.venv\nvendor\n")
        metadata = metadata_digest(root)
    elif changed == "metadata":
        git(root, "config", "core.filemode", "false")
    elif changed == "component":
        component = "backend"
    else:
        shifted = root.parent.parent / "shifted-public-component"
        (root / "frontend").rename(shifted)
        (root / "frontend").symlink_to(shifted, target_is_directory=True)
    with pytest.raises(ValueError):
        implementation().dependency_plan(root, baseline, metadata, component=component)
    assert (external / "marker").read_text() == "Retained public dependency\n"
