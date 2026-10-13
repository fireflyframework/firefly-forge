# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
import sys

import pytest
from test_role_execution import fixture_run

from firefly_forge.engine import Engine
from firefly_forge.models import Command, RunInput
from firefly_forge.process import checked
from firefly_forge.providers import AgentResult


@pytest.mark.parametrize("mutation", ["filter", "hook", "attributes"])
async def test_worker_git_metadata_changes_are_blocked_before_git_executes_them(store, tmp_path, mutation):
    run = await fixture_run(store, tmp_path)
    marker = tmp_path / "untrusted-git-executed"

    class Worker:
        async def execute(self, workspace, *args):
            (workspace / "product.py").write_text("stock = 5\n")
            if mutation == "filter":
                command = f'{sys.executable} -c "from pathlib import Path; Path({str(marker)!r}).touch()"'
                config = workspace / ".git/config"
                config.write_text(config.read_text() + f'\n[filter "unsafe"]\n\tclean = {command}\n')
                (workspace / ".gitattributes").write_text("*.py filter=unsafe\n")
            elif mutation == "hook":
                hook = workspace / ".git/hooks/pre-commit"
                hook.write_text(f"#!/bin/sh\ntouch '{marker}'\n")
                hook.chmod(0o700)
            else:
                (workspace / ".git/info/attributes").write_text("*.py -diff\n")
            return AgentResult("Worker changed metadata", "fixture")

    await Engine(store, tmp_path / "workspaces", lambda _: Worker()).execute_run(run["id"])
    result = await store.get_run(run["id"])
    assert result["status"] in {"failed", "needs_reconciliation"}
    assert "Git metadata" in result["error"]
    assert not marker.exists()


async def test_tracked_credential_files_are_rejected_before_project_code_executes(store, tmp_path):
    initial = await fixture_run(store, tmp_path)
    source = tmp_path / "source"
    (source / ".env").write_text("TOKEN=source-private-fixture\n")
    await checked(["git", "add", ".env"], source)
    await checked(
        [
            "git",
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-m",
            "Tracked secret",
        ],
        source,
    )
    marker = tmp_path / "source-code-executed"
    run = await store.create_run(
        RunInput(
            repository_id=initial["repository_id"],
            brief="Test source",
            operation="test",
            skills=[],
            commands=[Command(argv=[sys.executable, "-c", f"from pathlib import Path; Path({str(marker)!r}).touch()"])],
        )
    )
    await Engine(store, tmp_path / "workspaces").execute_run(run["id"])
    result = await store.get_run(run["id"])
    assert result["status"] == "failed" and "tracked private" in result["error"]
    assert not marker.exists() and "source-private-fixture" not in str(result)


async def test_delayed_campaign_child_uses_the_frozen_commit_after_source_branch_advances(store, tmp_path):
    run = await fixture_run(store, tmp_path)
    source = tmp_path / "source"
    frozen = await checked(["git", "rev-parse", "HEAD"], source)
    await store.update(run["id"], campaign_context={"source_base": frozen, "upstream_receipts": []})
    (source / "product.py").write_text("stock = 7\n")
    await checked(["git", "add", "."], source)
    await checked(
        [
            "git",
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-m",
            "Advance main",
        ],
        source,
    )

    class Worker:
        async def execute(self, workspace, *args):
            assert (workspace / "product.py").read_text() == "stock = 3\n"
            (workspace / "product.py").write_text("stock = 5\n")
            return AgentResult("Applied frozen baseline", "fixture")

    await Engine(store, tmp_path / "workspaces", lambda _: Worker()).execute_run(run["id"])
    result = await store.get_run(run["id"])
    assert result["status"] == "succeeded" and result["base_revision"] == frozen


async def test_nested_git_reference_symlink_cannot_redirect_checkpoint_outside_workspace(store, tmp_path):
    run = await fixture_run(store, tmp_path)
    base = await checked(["git", "rev-parse", "HEAD"], tmp_path / "source")
    outside = tmp_path / "outside-reference"
    outside.mkdir()
    reference = outside / run["id"][:12]
    reference.write_text(base + "\n")

    class Worker:
        async def execute(self, workspace, *args):
            refs = workspace / ".git/refs/heads/forge"
            refs.rename(workspace / ".git/refs/heads/original-forge")
            refs.symlink_to(outside, target_is_directory=True)
            (workspace / "product.py").write_text("stock = 5\n")
            return AgentResult("Updated stock", "fixture")

    await Engine(store, tmp_path / "workspaces", lambda _: Worker()).execute_run(run["id"])
    result = await store.get_run(run["id"])
    assert result["status"] in {"failed", "needs_reconciliation"} and "Git metadata" in result["error"]
    assert reference.read_text() == base + "\n"
