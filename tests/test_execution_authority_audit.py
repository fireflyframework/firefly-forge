# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Current project authority must hold at each effect boundary, not only admission."""

import json
import sys
from contextlib import suppress
from uuid import uuid4

import pytest
from starlette.requests import Request
from test_execution import seed_repository
from test_operations import release_fixture

from firefly_forge.access_routes import AccessRoutes
from firefly_forge.communication import CommunicationPublicationV1
from firefly_forge.engine import Engine
from firefly_forge.identity import Principal
from firefly_forge.journal import Journal
from firefly_forge.models import Command, RepositoryInput, RunInput
from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied, ProjectAccess
from firefly_forge.project_campaigns import CampaignLaunch, ProjectCampaigns
from firefly_forge.project_models import ProjectDefinition
from firefly_forge.projects import Projects
from firefly_forge.providers import AgentResult
from firefly_forge.sandbox_models import SandboxInput


async def project(store, repository):
    return await Projects(store).configure(
        ProjectDefinition(
            name="Authority audit",
            description="Review stage permission boundaries",
            topology="monorepo",
            owners=["Platform"],
            components=[dict(id="service", name="Service", repository_id=repository["id"], responsibility="Service")],
        )
    )


@pytest.mark.parametrize("boundary", ["prepare", "materialize"])
async def test_membership_revoked_after_prepare_prevents_coding_provider_dispatch(
    store, tmp_path, monkeypatch, boundary
):
    import firefly_forge.engine as module

    source = await seed_repository(tmp_path / "source")
    repository = await store.add_repository(RepositoryInput(name="Service", url=str(source)))
    definition = await project(store, repository)
    user = Principal(uuid4(), "https://identity.example/realms/forge", uuid4().hex)
    access = ProjectAccess(store)
    await access.set_membership(definition["id"], user, "developer")
    context = CURRENT_PRINCIPAL.set(user)
    try:
        run = await store.create_run(
            RunInput(
                repository_id=repository["id"],
                project_id=definition["id"],
                component_id="service",
                brief="Implement a service",
                agent="agentic",
                sandbox=SandboxInput(mode="docker", context="colima-forge-tests", image="forge@sha256:" + "a" * 64),
                commands=[Command(argv=[sys.executable, "-c", "assert True"])],
            )
        )
    finally:
        CURRENT_PRINCIPAL.reset(context)
    clone = module.clone

    async def revoked_clone(*args, **kwargs):
        result = await clone(*args, **kwargs)
        if boundary == "prepare":
            await access.set_membership(definition["id"], user, "viewer")
        return result

    monkeypatch.setattr(module, "clone", revoked_clone)

    class Worker:
        calls = 0

        async def execute(self, workspace, brief, backend, frontend, emit):
            self.calls += 1
            (workspace / "service.py").write_text("service = 'modified after revocation'\n")
            return AgentResult("Changed service", "fixture")

    worker = Worker()
    journal = Journal(store)
    engine = Engine(store, tmp_path / "jobs", lambda _: worker, journal=journal)
    materialize = engine._materialize_design

    async def revoked_materialize(*args):
        await materialize(*args)
        if boundary == "materialize":
            await access.set_membership(definition["id"], user, "viewer")

    monkeypatch.setattr(engine, "_materialize_design", revoked_materialize)
    await engine.execute_run(run["id"])
    assert worker.calls == 0, "A revoked actor must not dispatch a new coding invocation"
    assert (await store.get_run(run["id"]))["status"] == "failed"


async def test_membership_revoked_during_review_setup_prevents_reviewer_dispatch(store, tmp_path):
    from firefly_forge.assurance import Assurance

    _, run, definition, _, builder, _ = await release_fixture(store, tmp_path)
    await store.update(run["id"], agent="agentic", assurance={"review_agent": "agentic"})

    class Reviewer:
        calls = 0

        async def execute(self, workspace, *args):
            self.calls += 1
            (workspace / "review.json").write_text(
                json.dumps({"verdict": "approved", "summary": "Reviewed", "findings": []})
            )
            return AgentResult("Reviewed", "fixture")

    async def emit(stage, message):
        if stage == "reviewing":
            await ProjectAccess(store).set_membership(definition["id"], builder, "viewer")

    reviewer = Reviewer()
    review = Assurance(store, tmp_path / "review", provider_factory=lambda _: reviewer)
    saved = await store.get_run(run["id"])
    with suppress(AccessDenied):
        await review.review(saved, tmp_path / "workspaces" / run["id"], emit)
    assert reviewer.calls == 0, "A revoked actor must not dispatch a new independent reviewer"


@pytest.mark.parametrize("revoke_after_push", [False, True])
async def test_publication_uses_distinct_current_releaser_and_rechecks_before_pr(
    store, tmp_path, monkeypatch, revoke_after_push
):
    import firefly_forge.engine as module

    manager, run, definition, owner, builder, _ = await release_fixture(store, tmp_path)
    engine = manager.engine
    engine.journal = Journal(store)
    files, _, _ = await engine._snapshot(engine.workspaces / run["id"], run["base_revision"])
    await store.update(run["id"], changed_files=files)
    await engine._checkpoint_commit(run["id"], engine.workspaces / run["id"])
    run = await store.get_run(run["id"])
    publication = CommunicationPublicationV1.model_validate(
        {key: run[key] for key in ("summary", "source_tree", "checkpoint_revision")}
    )
    assert publication.summary == "Verified public fixture change"
    original_repository = store.repository

    async def remote_repository(repository_id):
        return {**await original_repository(repository_id), "provider": "github", "url": "https://github.com/org/repo"}

    monkeypatch.setattr(store, "repository", remote_repository)
    monkeypatch.setenv("FORGE_GITHUB_TOKEN", "audit-only-not-an-actual-provider-token")
    effects = []

    async def push(*args, **kwargs):
        effects.append("push")
        if revoke_after_push:
            await ProjectAccess(store).set_membership(definition["id"], owner, "viewer")

    async def create(*args, **kwargs):
        effects.append("pr")
        return "https://github.com/org/repo/pull/1"

    monkeypatch.setattr(module, "push", push)
    monkeypatch.setattr(module.PullRequests, "create", create)
    context = CURRENT_PRINCIPAL.set(owner)
    try:
        if revoke_after_push:
            # A second owner is needed to permit removing the original owner's role.
            await ProjectAccess(store).set_membership(definition["id"], builder, "owner")
            with pytest.raises(AccessDenied):
                await engine.publish(run["id"])
            assert effects == ["push"]
            assert (await store.get_run(run["id"]))["publication_status"] == "publishing"
            entries = await engine.journal.entries(run["id"])
            assert not any(row["kind"] == "receipt" and row["task_id"] == "publish" for row in entries)
        else:
            assert await engine.publish(run["id"]) == "https://github.com/org/repo/pull/1"
            assert effects == ["push", "pr"]
            saved = await store.get_run(run["id"])
            assert saved["publication_actor"]["user_id"] == owner.id.hex
            assert saved["execution_actor"]["user_id"] == builder.id.hex
    finally:
        CURRENT_PRINCIPAL.reset(context)


@pytest.mark.parametrize("action", ["resume", "retry"])
async def test_campaign_retained_publication_requires_current_release_authority(store, tmp_path, action):
    repository = await store.add_repository(RepositoryInput(name="Service", url=str(tmp_path)))
    definition = await project(store, repository)
    journal = Journal(store)
    engine = Engine(store, tmp_path / "jobs", journal=journal)

    async def revision(_):
        return "1" * 40

    manager = ProjectCampaigns(store, engine, journal, source_resolver=revision)
    campaign = await manager.launch(
        definition["id"], CampaignLaunch(request_id=uuid4().hex, brief="Ship service", publish=True)
    )
    developer = Principal(uuid4(), "https://identity.example/realms/forge", uuid4().hex)
    access = ProjectAccess(store)
    await access.set_membership(definition["id"], developer, "developer")

    async def receive():
        return {"type": "http.request", "body": b"{}", "more_body": False}

    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": f"/api/v1/projects/{definition['id']}/campaigns/{campaign['id']}/{action}",
            "headers": [],
        },
        receive,
    )
    with pytest.raises(AccessDenied):
        await AccessRoutes(access, store).authorize(developer, request)
