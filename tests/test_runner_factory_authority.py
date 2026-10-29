# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Factory authority remains native and current before any project bytes are transferred."""

import importlib
from uuid import uuid4

import pytest

from firefly_forge.journal import Journal
from firefly_forge.models import RepositoryInput, RunInput


def authority_module():
    try:
        return importlib.import_module("firefly_forge.runner_factory_authority")
    except ModuleNotFoundError:
        pytest.fail("Native factory runner transfer authority is not implemented")


async def seed(store):
    repository = await store.add_repository(
        RepositoryInput(name="Owned source", url="https://example.invalid/owned.git")
    )
    run = await store.create_run(RunInput(repository_id=repository["id"], brief="Owned source transfer"))
    await store.update(run["id"], status="validating")
    journal = Journal(store)
    await journal.ensure_plan(run["id"])
    await journal.intent(run["id"], "verify")
    await store.acquire_ownership()
    return run, journal


async def test_factory_transfer_requires_actual_owner_and_retained_stage(store):
    module = authority_module()
    run, journal = await seed(store)
    authority = module.RunnerFactoryAuthority(store, journal)
    scope = await authority.authorize(run["id"], "verify")
    assert scope["run"]["repository_id"] == run["repository_id"]
    assert scope["attempt"] == 1 and scope["fence"].factory_schema == store.schema
    await store.release_ownership()
    with pytest.raises(RuntimeError, match="ownership|owner"):
        await authority.authorize(run["id"], "verify")


async def test_factory_transfer_cannot_use_foreign_retained_repository_or_cancelled_stage(store):
    module = authority_module()
    run, journal = await seed(store)
    authority = module.RunnerFactoryAuthority(store, journal)
    project_id = uuid4().hex
    await store.update(
        run["id"], project_id=project_id, component_snapshot={"project_id": project_id, "repository_id": uuid4().hex}
    )
    with pytest.raises(ValueError, match="repository|component"):
        await authority.authorize(run["id"], "verify")
    await store.update(run["id"], project_id=None, component_snapshot=None, cancel_request={"status": "requested"})
    with pytest.raises(RuntimeError, match="cancel|dispatch"):
        await authority.authorize(run["id"], "verify")


async def test_repository_read_and_project_execute_are_rechecked_after_membership_revocation(store):
    from firefly_forge.identity import Principal
    from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied, ProjectAccess
    from firefly_forge.project_models import ProjectDefinition
    from firefly_forge.projects import Projects
    from firefly_forge.sandbox_models import SandboxInput

    module = authority_module()
    repository = await store.add_repository(
        RepositoryInput(name="Owned project", url="https://example.invalid/product.git")
    )
    project = await Projects(store).configure(
        ProjectDefinition(
            name="Owned product",
            description="Own project",
            topology="monorepo",
            owners=["Platform"],
            components=[
                dict(id="service", name="Service", repository_id=repository["id"], responsibility="Own source")
            ],
        )
    )
    user = Principal(uuid4(), "https://issuer.invalid", "owned-member")
    access = ProjectAccess(store)
    await access.set_membership(project["id"], user, "developer")
    context = CURRENT_PRINCIPAL.set(user)
    try:
        run = await store.create_run(
            RunInput(
                repository_id=repository["id"],
                project_id=project["id"],
                component_id="service",
                brief="Scoped transfer",
                agent="agentic",
                sandbox=SandboxInput(mode="docker", image="example/worker@sha256:" + "a" * 64, context="owned-context"),
            ),
        )
    finally:
        CURRENT_PRINCIPAL.reset(context)
    await store.update(run["id"], status="validating")
    journal = Journal(store)
    await journal.ensure_plan(run["id"])
    await journal.intent(run["id"], "verify")
    await store.acquire_ownership()
    authority = module.RunnerFactoryAuthority(store, journal)
    scope = await authority.authorize(run["id"], "verify")
    assert scope["run"]["project_id"] == project["id"]
    await access.set_membership(project["id"], user, None)
    with pytest.raises(AccessDenied):
        await authority.authorize(run["id"], "verify")
