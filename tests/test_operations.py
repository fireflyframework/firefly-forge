# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Deployment checks execute real commands against a verified Git tree."""

import asyncio
import sys
from uuid import uuid4

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
        summary="Verified public fixture change",
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


async def release_fixture(store, tmp_path):
    from firefly_forge.identity import Principal
    from firefly_forge.operations import DeploymentManager, DeploymentTarget
    from firefly_forge.project_access import ProjectAccess
    from firefly_forge.project_models import ProjectDefinition
    from firefly_forge.projects import Projects

    coordinator, run, repository = await verified_run(store, tmp_path)
    project = await Projects(store).configure(
        ProjectDefinition(
            name="Release project",
            description="An independently released service",
            topology="monorepo",
            owners=["Platform"],
            components=[dict(id="service", name="Service", repository_id=repository["id"], responsibility="Service")],
        )
    )
    owner = Principal(uuid4(), "https://identity.example/realms/forge", uuid4().hex)
    builder = Principal(uuid4(), owner.issuer, uuid4().hex)
    access = ProjectAccess(store)
    await access.set_membership(project["id"], owner, "owner")
    await access.set_membership(project["id"], builder, "developer")
    await store.update(run["id"], project_id=project["id"], execution_actor=access.actor_snapshot(builder))
    marker = tmp_path / "release-marker.txt"
    manager = DeploymentManager(store, coordinator)
    await manager.configure_target(
        "local",
        DeploymentTarget(
            name="Release acceptance",
            repository_id=repository["id"],
            commands=[
                {
                    "argv": [
                        sys.executable,
                        "-c",
                        f"from pathlib import Path; Path({str(marker)!r}).write_text('deployed')",
                    ]
                }
            ],
            verification=[
                {"argv": [sys.executable, "-c", f"from pathlib import Path; assert Path({str(marker)!r}).exists()"]}
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
        ),
    )
    return manager, run, project, owner, builder, marker


async def suspend_identity(store, user):
    from firefly_forge.identity_persistence import ForgeIdentityUserRepository

    async with store._unit():
        users = ForgeIdentityUserRepository()
        row = await users.find_by_id(user.id)
        row.enabled = False
        await users.save(row)


async def test_release_uses_owner_instead_of_developer_builder_and_records_actor(store, tmp_path):
    from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied

    manager, run, project, owner, builder, marker = await release_fixture(store, tmp_path)
    context = CURRENT_PRINCIPAL.set(builder)
    try:
        with pytest.raises(AccessDenied):
            await manager.create(run["id"], "local")
    finally:
        CURRENT_PRINCIPAL.reset(context)
    assert await store.deployments() == []
    context = CURRENT_PRINCIPAL.set(owner)
    try:
        job = await manager.create(run["id"], "local")
    finally:
        CURRENT_PRINCIPAL.reset(context)
    assert job["execution_actor"]["user_id"] == owner.id.hex
    assert job["project_id"] == project["id"]
    await manager.execute_deployment(job["id"])
    assert (await store.get_deployment(job["id"]))["status"] == "succeeded"
    assert marker.read_text() == "deployed"
    assert (await store.get_run(run["id"]))["execution_actor"]["user_id"] == builder.id.hex
    entries = await manager.journal.entries(run["id"])
    intents = [entry for entry in entries if entry["kind"] == "intent"]
    assert len(intents) == 2
    assert all(entry["payload"]["actor_id"] == owner.id.hex for entry in intents)


async def test_suspended_queued_release_never_exports_or_executes_source(store, tmp_path):
    from firefly_forge.project_access import CURRENT_PRINCIPAL

    manager, run, _, owner, _, marker = await release_fixture(store, tmp_path)
    context = CURRENT_PRINCIPAL.set(owner)
    try:
        job = await manager.create(run["id"], "local")
    finally:
        CURRENT_PRINCIPAL.reset(context)
    await suspend_identity(store, owner)
    await manager.execute_deployment(job["id"])
    assert (await store.get_deployment(job["id"]))["status"] == "failed"
    assert not marker.exists()
    assert not (manager.workspaces / job["id"]).exists()
    assert await manager.journal.entries(run["id"]) == []


@pytest.mark.parametrize("revocation", ["suspension", "project_role"])
async def test_release_revocation_between_commands_stops_next_effect(store, tmp_path, monkeypatch, revocation):
    import firefly_forge.operations as operations
    from firefly_forge.identity import Principal
    from firefly_forge.project_access import CURRENT_PRINCIPAL, ProjectAccess

    manager, run, project, owner, _, marker = await release_fixture(store, tmp_path)
    access = ProjectAccess(store)
    backup_owner = Principal(uuid4(), owner.issuer, uuid4().hex)
    await access.set_membership(project["id"], backup_owner, "owner")
    context = CURRENT_PRINCIPAL.set(owner)
    try:
        job = await manager.create(run["id"], "local")
    finally:
        CURRENT_PRINCIPAL.reset(context)
    original = operations.execute
    calls = 0

    async def revoke_after_command(*args, **kwargs):
        nonlocal calls
        result = await original(*args, **kwargs)
        calls += 1
        if revocation == "suspension":
            await suspend_identity(store, owner)
        else:
            await access.set_membership(project["id"], owner, "developer")
        return result

    monkeypatch.setattr(operations, "execute", revoke_after_command)
    await manager.execute_deployment(job["id"])
    result = await store.get_deployment(job["id"])
    assert result["status"] == "failed"
    assert calls == 1
    assert marker.read_text() == "deployed"
    assert len(result["evidence"]) == 1
    entries = await manager.journal.entries(run["id"])
    assert [entry["kind"] for entry in entries if entry["task_id"].startswith("deploy-")] == ["intent", "receipt"]


async def test_release_revoked_after_intent_records_known_nonexecution(store, tmp_path, monkeypatch):
    from firefly_forge.project_access import CURRENT_PRINCIPAL

    manager, run, _, owner, _, marker = await release_fixture(store, tmp_path)
    context = CURRENT_PRINCIPAL.set(owner)
    try:
        job = await manager.create(run["id"], "local")
    finally:
        CURRENT_PRINCIPAL.reset(context)
    original = manager.journal.intent

    async def revoke_after_intent(*args, **kwargs):
        result = await original(*args, **kwargs)
        await suspend_identity(store, owner)
        return result

    monkeypatch.setattr(manager.journal, "intent", revoke_after_intent)
    await manager.execute_deployment(job["id"])
    result = await store.get_deployment(job["id"])
    assert result["status"] == "failed"
    assert result["evidence"] == []
    assert not marker.exists()
    entries = [entry for entry in await manager.journal.entries(run["id"]) if entry["task_id"].startswith("deploy-")]
    assert entries[0]["kind"] == "intent"
    assert len(entries) == 2
    assert entries[-1]["kind"] != "receipt"
    assert manager.journal.verify_chain(await manager.journal.entries(run["id"]))


async def test_rollback_captures_current_releaser_and_rechecks_suspension(store, tmp_path):
    from firefly_forge.identity import Principal
    from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied, ProjectAccess

    manager, run, project, owner, builder, marker = await release_fixture(store, tmp_path)
    context = CURRENT_PRINCIPAL.set(owner)
    try:
        job = await manager.create(run["id"], "local")
    finally:
        CURRENT_PRINCIPAL.reset(context)
    await manager.execute_deployment(job["id"])
    context = CURRENT_PRINCIPAL.set(builder)
    try:
        with pytest.raises(AccessDenied):
            await manager.rollback(job["id"])
    finally:
        CURRENT_PRINCIPAL.reset(context)
    releaser = Principal(uuid4(), owner.issuer, uuid4().hex)
    await ProjectAccess(store).set_membership(project["id"], releaser, "maintainer")
    context = CURRENT_PRINCIPAL.set(releaser)
    try:
        rollback = await manager.rollback(job["id"])
    finally:
        CURRENT_PRINCIPAL.reset(context)
    assert rollback["execution_actor"]["user_id"] == releaser.id.hex
    await suspend_identity(store, releaser)
    await manager.execute_deployment(rollback["id"])
    assert (await store.get_deployment(rollback["id"]))["status"] == "failed"
    assert marker.read_text() == "deployed"


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
    from test_knowledge import retained_configuration

    retained_configuration(knowledge.private)
    knowledge.configure(KnowledgeConfiguration(values={"enabled": False, "api_key": "private-ui-knowledge-key"}))
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


async def test_cloud_recipe_and_credentials_are_frozen_and_outputs_redacted(store, tmp_path):
    from firefly_forge.deployment_credentials import CredentialInput, DeploymentCredentials
    from firefly_forge.operations import DeploymentManager, DeploymentTarget

    coordinator, run, repository = await verified_run(store, tmp_path)
    credentials = DeploymentCredentials(store.configuration)
    credentials.configure("aws", CredentialInput(values={"AWS_SECRET_ACCESS_KEY": "frozen-deployment-secret"}))
    manager = DeploymentManager(store, coordinator, credentials=credentials)
    import hashlib

    expected = hashlib.sha256(b"frozen-deployment-secret").hexdigest()
    argv = [
        sys.executable,
        "-c",
        "import os,hashlib; from pathlib import Path; "
        "assert Path('.forge-deployment/recipe.txt').read_text() == 'verified recipe'; "
        f"assert hashlib.sha256(os.environ['AWS_SECRET_ACCESS_KEY'].encode()).hexdigest() == {expected!r}; "
        "print(os.environ['AWS_SECRET_ACCESS_KEY'])",
    ]
    target = DeploymentTarget(
        name="Cloud",
        repository_id=repository["id"],
        commands=[{"argv": argv}],
        verification=[{"argv": [sys.executable, "-c", "assert True"]}],
        recipe_files={"recipe.txt": "verified recipe"},
        credential_profile="aws",
    )
    await manager.configure_target("cloud", target)
    job = await manager.create(run["id"], "cloud")
    credentials.configure("aws", CredentialInput(values={"AWS_SECRET_ACCESS_KEY": "changed-deployment-secret"}))
    await manager.execute_deployment(job["id"])
    result = await store.get_deployment(job["id"])
    assert result["status"] == "succeeded", result
    assert "frozen-deployment-secret" not in str(result)
    assert "[redacted]" in result["evidence"][0]["output"]


def test_cloud_recipe_files_cannot_escape_or_exceed_limits():
    from firefly_forge.operations import DeploymentTarget

    for files in ({"../../unsafe": "x"}, {"/absolute": "x"}, {".git/config": "x"}, {"file": "x" * 1_000_001}):
        with pytest.raises(ValueError):
            DeploymentTarget(
                name="Invalid",
                repository_id="project",
                commands=[{"argv": ["true"]}],
                verification=[{"argv": ["true"]}],
                recipe_files=files,
            )
