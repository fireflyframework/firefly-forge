# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Real Git preparation and source admission without a database or coding model."""

import subprocess

import pytest
from test_bridge_inflight_cancel import RUN, MemoryStore

from firefly_forge.engine import Engine
from firefly_forge.providers import ProviderError


def git(workspace, *args):
    return subprocess.run(
        ["git", *args], cwd=workspace, capture_output=True, text=True, timeout=10, check=True
    ).stdout.strip()


@pytest.mark.parametrize("name", ["node_modules", ".venv", "vendor"])
@pytest.mark.parametrize("tracked", [False, True])
async def test_native_preparation_excludes_runtime_aliases_but_refuses_tracked_private_paths(
    tmp_path, monkeypatch, name, tracked
):
    store = MemoryStore(tmp_path / "memory")
    store.run.data.update(resume_existing=False, git_metadata_digest=None)
    external = tmp_path / "retained-dependencies"
    external.mkdir()
    (external / "marker").write_text("Retained dependency; never source\n")
    observed = []
    baselines = []

    async def clone(repository, workspace, branch, **kwargs):
        workspace.mkdir()
        git(workspace, "init", "--quiet")
        git(workspace, "config", "user.name", "Fictional test author")
        git(workspace, "config", "user.email", "fixture@example.test")
        (workspace / "README.md").write_text("Original source\n")
        git(workspace, "add", "README.md")
        git(workspace, "commit", "--quiet", "-m", "Create fixture source")
        baselines.append(git(workspace, "rev-parse", "HEAD"))
        return baselines[-1]

    class Worker:
        async def execute(self, workspace, *args):
            component = workspace / "component"
            component.mkdir()
            alias = component / name
            alias.symlink_to(external, target_is_directory=True)
            (component / "app.py").write_text("VALUE = 1\n")
            if tracked:
                git(workspace, "add", "--force", "--", str(alias.relative_to(workspace)))
                with pytest.raises(ValueError, match="private credentials or worker metadata"):
                    await engine._snapshot(workspace, baselines[-1])
                observed.append("tracked-private-refused")
            else:
                files, patch, _ = await engine._snapshot(workspace, baselines[-1])
                observed.append(files)
                assert patch.artifact.file_count == 1
                assert "Retained dependency" not in patch.preview
            raise ProviderError("Controlled stop after real Git source admission")

    monkeypatch.setattr("firefly_forge.engine.clone", clone)
    engine = Engine(store, tmp_path / "workspaces", lambda _: Worker())
    await engine.execute_run(RUN)
    assert observed == (["tracked-private-refused"] if tracked else [["component/app.py"]]), store.run.data
    assert (external / "marker").read_text() == "Retained dependency; never source\n"
