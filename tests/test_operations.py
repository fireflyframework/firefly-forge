# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Deployment checks execute real commands against a verified Git tree."""

import asyncio
import sys

import pytest

from firefly_forge.engine import Engine
from firefly_forge.models import RepositoryInput, RunInput
from firefly_forge.process import checked


async def verified_run(store, tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    await checked(["git", "init", "-b", "main"], source)
    await checked(["git", "config", "user.name", "Test"], source)
    await checked(["git", "config", "user.email", "test@example.invalid"], source)
    (source / "README.md").write_text("An actual product source tree\n")
    await checked(["git", "add", "."], source)
    await checked(["git", "commit", "-m", "Initial source"], source)
    repository = await store.add_repository(RepositoryInput(name="Product", url=str(source)))
    run = await store.create_run(RunInput(repository_id=repository["id"], brief="Add a verified service"))
    coordinator = Engine(store, tmp_path / "workspaces")
    workspace = coordinator.workspaces / run["id"]
    await checked(["git", "clone", str(source), str(workspace)], tmp_path)
    base = await checked(["git", "rev-parse", "HEAD"], workspace)
    (workspace / "service.py").write_text("def status():\n    return 'ok'\n")
    await checked([sys.executable, "-c", "from service import status; assert status() == 'ok'"], workspace)
    _, _, tree = await coordinator._snapshot(workspace, base)
    await store.claim(run["id"])
    await store.update(
        run["id"],
        base_revision=base,
        source_tree=tree,
        final_verification=[
            {"argv": [sys.executable, "-c", "from service import status; assert status() == 'ok'"], "exit_code": 0}
        ],
    )
    await store.finish(run["id"], "succeeded")
    return coordinator, await store.get_run(run["id"]), repository


async def wait_for(manager, identifier):
    async with asyncio.timeout(10):
        while True:
            job = await manager.store.get_deployment(identifier)
            if job["status"] not in {"queued", "running"}:
                return job
            await asyncio.sleep(0.05)


async def test_deploys_isolated_verified_source_and_rolls_back_with_retained_evidence(store, tmp_path):
    from firefly_forge.operations import DeploymentManager, DeploymentTarget

    coordinator, run, repository = await verified_run(store, tmp_path)
    manager = DeploymentManager(store, coordinator)
    marker = tmp_path / "deployed.txt"
    target = DeploymentTarget(
        name="Local acceptance target",
        repository_id=repository["id"],
        commands=[
            {
                "argv": [
                    sys.executable,
                    "-c",
                    "from pathlib import Path; assert not Path('.git').exists(); from service import status; "
                    f"Path({str(marker)!r}).write_text(status())",
                ]
            }
        ],
        verification=[
            {
                "argv": [
                    sys.executable,
                    "-c",
                    f"from pathlib import Path; assert Path({str(marker)!r}).read_text() == 'ok'",
                ]
            }
        ],
        rollback_commands=[
            {
                "argv": [
                    sys.executable,
                    "-c",
                    f"from pathlib import Path; Path({str(marker)!r}).write_text('rolled back')",
                ]
            }
        ],
        rollback_verification=[
            {
                "argv": [
                    sys.executable,
                    "-c",
                    f"from pathlib import Path; assert Path({str(marker)!r}).read_text() == 'rolled back'",
                ]
            }
        ],
    )
    await manager.configure_target("local", target)
    job = await manager.create(run["id"], "local")
    assert not marker.exists()
    await manager.start()
    try:
        result = await wait_for(manager, job["id"])
        assert result["status"] == "succeeded", result
        assert marker.read_text() == "ok"
        assert len(result["evidence"]) == 2
        from firefly_forge.journal import Journal

        journal = Journal(store)
        entries = await journal.entries(run["id"])
        assert journal.verify_chain(entries)
        assert [entry["kind"] for entry in entries if entry["task_id"].startswith("deploy-")] == [
            "intent",
            "receipt",
            "intent",
            "receipt",
        ]
        assert (coordinator.workspaces / run["id"] / "service.py").read_text() == "def status():\n    return 'ok'\n"
        # Rollback has its own commands; a forward health check need not describe the previous version.
        rollback = await manager.rollback(result["id"])
        rolled_back = await wait_for(manager, rollback["id"])
        assert rolled_back["status"] == "succeeded"
        assert marker.read_text() == "rolled back"
        assert rolled_back["parent_id"] == result["id"]
        assert (await store.get_run(run["id"]))["status"] == "succeeded"
    finally:
        await manager.stop()


async def test_rejects_tampered_source_and_wrong_project_before_deploying(store, tmp_path):
    from firefly_forge.operations import DeploymentManager, DeploymentTarget

    coordinator, run, repository = await verified_run(store, tmp_path)
    manager = DeploymentManager(store, coordinator)
    command = {"argv": [sys.executable, "-c", "print('verified')"]}
    await manager.configure_target(
        "local",
        DeploymentTarget(name="Local", repository_id=repository["id"], commands=[command], verification=[command]),
    )
    (coordinator.workspaces / run["id"] / "service.py").write_text("raise RuntimeError('unverified')\n")
    with pytest.raises(ValueError, match="Source changed"):
        await manager.create(run["id"], "local")
    assert await store.deployments() == []


async def test_failed_deployment_check_never_reports_success(store, tmp_path):
    from firefly_forge.operations import DeploymentManager, DeploymentTarget

    coordinator, run, repository = await verified_run(store, tmp_path)
    manager = DeploymentManager(store, coordinator)
    await manager.configure_target(
        "local",
        DeploymentTarget(
            name="Local",
            repository_id=repository["id"],
            commands=[{"argv": [sys.executable, "-c", "print('deploy attempted')"]}],
            verification=[{"argv": [sys.executable, "-c", "raise SystemExit(7)"]}],
        ),
    )
    job = await manager.create(run["id"], "local")
    await manager.start()
    try:
        result = await wait_for(manager, job["id"])
        assert result["status"] == "failed"
        assert result["evidence"][-1]["exit_code"] == 7
    finally:
        await manager.stop()


async def test_target_rejects_ui_saved_coding_and_knowledge_credentials(store, tmp_path):
    from firefly_forge.configuration import ProviderConfiguration
    from firefly_forge.knowledge import KnowledgeConfiguration, KnowledgeManager
    from firefly_forge.operations import DeploymentManager, DeploymentTarget

    coordinator, _, repository = await verified_run(store, tmp_path)
    store.configuration.update("bridge", ProviderConfiguration(values={"token": "private-ui-bridge-token"}))
    knowledge = KnowledgeManager(store, coordinator)
    knowledge.configure(KnowledgeConfiguration(values={"api_key": "private-ui-knowledge-key"}))
    coordinator.knowledge = knowledge
    manager = DeploymentManager(store, coordinator)
    for secret in ("private-ui-bridge-token", "private-ui-knowledge-key"):
        with pytest.raises(ValueError, match="must not contain credentials"):
            await manager.configure_target(
                "local",
                DeploymentTarget(
                    name="Local",
                    repository_id=repository["id"],
                    commands=[{"argv": [sys.executable, "-c", f"print({secret!r})"]}],
                    verification=[{"argv": [sys.executable, "-c", "assert True"]}],
                ),
            )
    assert manager.targets() == []
