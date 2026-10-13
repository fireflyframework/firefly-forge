# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
import uuid

import pytest

from firefly_forge.controllers import RunService
from firefly_forge.engine import Engine
from firefly_forge.journal import Journal
from firefly_forge.models import RepositoryInput
from firefly_forge.project_campaigns import CampaignAction, CampaignLaunch, ProjectCampaigns
from firefly_forge.project_models import ProjectDefinition
from firefly_forge.projects import Projects


async def cancelled_campaign(store, tmp_path):
    repo = await store.add_repository(RepositoryInput(name="Stock", url=str(tmp_path)))
    project = await Projects(store).configure(
        ProjectDefinition(
            name="Stock",
            description="Stock service",
            topology="monorepo",
            owners=["Team"],
            components=[{"id": "api", "name": "API", "repository_id": repo["id"], "responsibility": "Manage stock"}],
        )
    )
    journal = Journal(store)
    engine = Engine(store, tmp_path / "workspaces", journal=journal)

    async def source_base(repo):
        return "1" * 40

    manager = ProjectCampaigns(store, engine, journal, source_resolver=source_base)
    campaign = await manager.launch(project["id"], CampaignLaunch(request_id=uuid.uuid4().hex, brief="Maintain stock"))
    child = campaign["children"][0]["run_id"]
    await journal.ensure_plan(child)
    await store.finish(child, "interrupted")
    await manager.cancel(project["id"], campaign["id"], CampaignAction(request_id=uuid.uuid4().hex))
    return child, journal, engine


async def test_individual_resume_cannot_restart_a_child_after_campaign_cancel(store, tmp_path):
    child, journal, engine = await cancelled_campaign(store, tmp_path)
    with pytest.raises(ValueError, match="campaign|Campaign"):
        await journal.resume(child)
    assert (await store.get_run(child))["status"] == "interrupted"


async def test_individual_retry_cannot_bypass_frozen_campaign_admission(store, tmp_path):
    child, journal, engine = await cancelled_campaign(store, tmp_path)
    with pytest.raises(ValueError, match="campaign|Campaign"):
        await RunService(store, engine).retry(child)
    assert len(await store.runs()) == 1
