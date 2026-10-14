# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Production domain beans retain the native repositories used by shared units."""

import pytest
import pytest_asyncio

from firefly_forge.api import create_application
from firefly_forge.assistant_persistence import (
    ForgeAssistantEventRepository,
    ForgeAssistantSessionRepository,
    ForgeAssistantTurnRepository,
)
from firefly_forge.product_assistant import ProductAssistant
from firefly_forge.project_campaign_persistence import ForgeCampaignChildRepository, ForgeCampaignRepository
from firefly_forge.project_campaigns import ProjectCampaigns
from firefly_forge.project_persistence import (
    ForgeAnalysisRepository,
    ForgeProjectRepository,
    ForgeProjectRevisionRepository,
)
from firefly_forge.projects import Projects
from firefly_forge.reverse_engineering import ReverseEngineering
from firefly_forge.store import Store


@pytest_asyncio.fixture
async def native_context(tmp_path, postgres_schema):
    app = create_application(tmp_path, "owned-native-di-fixture-token-32-characters", role="api")
    async with app.router.lifespan_context(app):
        yield app.state.runtime.context


@pytest.mark.parametrize(
    ("domain", "attribute", "dependency"),
    [
        (ReverseEngineering, "records", ForgeAnalysisRepository),
        (Projects, "records", ForgeProjectRepository),
        (Projects, "revisions", ForgeProjectRevisionRepository),
        (ProductAssistant, "records", ForgeAssistantSessionRepository),
        (ProductAssistant, "turns", ForgeAssistantTurnRepository),
        (ProductAssistant, "events", ForgeAssistantEventRepository),
        (ProductAssistant, "projects", Projects),
        (ProjectCampaigns, "records", ForgeCampaignRepository),
        (ProjectCampaigns, "project_records", ForgeProjectRepository),
        (ProjectCampaigns, "projects", Projects),
        (Store, "campaign_records", ForgeCampaignRepository),
        (Store, "campaign_children", ForgeCampaignChildRepository),
    ],
)
async def test_production_domain_dependency_is_the_native_container_bean(native_context, domain, attribute, dependency):
    bean = native_context.get_bean(domain)
    expected = native_context.get_bean(dependency)
    assert getattr(bean, attribute, None) is expected
    if domain is not Store:
        assert bean.store is native_context.get_bean(Store)


async def test_production_project_revisions_use_injected_repository_through_native_units(
    native_context, tmp_path, monkeypatch
):
    from test_projects import definition

    from firefly_forge import projects as project_module
    from firefly_forge.models import RepositoryInput

    store = native_context.get_bean(Store)
    projects = native_context.get_bean(Projects)
    repo = await store.add_repository(RepositoryInput(name="Owned DI fixture", url=str(tmp_path)))

    def refused_constructor():
        raise AssertionError("Production project operations constructed an unbound revision repository")

    # Existing container DAO still performs actual PostgreSQL operations; only
    # an accidental new constructor is refused after the native lifespan starts.
    monkeypatch.setattr(project_module, "ForgeProjectRevisionRepository", refused_constructor)
    first = await projects.configure(definition(repo["id"]))
    second = await projects.configure(definition(repo["id"], name="Revised fixture"), first["id"])
    assert second["revision"] == 2
    assert (await projects.revision(first["id"], 1))["definition"]["name"] == "Inventory"
    assert (await projects.revision(first["id"], 2))["definition"]["name"] == "Revised fixture"


async def test_production_conversation_and_campaign_use_the_injected_project_service(
    native_context, tmp_path, monkeypatch
):
    from test_projects import definition

    from firefly_forge import product_assistant as assistant_module
    from firefly_forge import project_campaigns as campaign_module
    from firefly_forge.models import RepositoryInput
    from firefly_forge.product_assistant import AssistantSessionInput

    store = native_context.get_bean(Store)
    projects = native_context.get_bean(Projects)
    assistant = native_context.get_bean(ProductAssistant)
    campaigns = native_context.get_bean(ProjectCampaigns)
    repo = await store.add_repository(RepositoryInput(name="Owned conversation fixture", url=str(tmp_path)))
    project = await projects.configure(definition(repo["id"], product_repository_id=repo["id"]))

    def refused_constructor(*args, **kwargs):
        raise AssertionError("Production domain operation constructed a second project service")

    monkeypatch.setattr(assistant_module, "Projects", refused_constructor)
    monkeypatch.setattr(campaign_module, "Projects", refused_constructor)
    conversation = await assistant.create(repo["id"], AssistantSessionInput(project_id=project["id"]))
    observed = await assistant.session(repo["id"], conversation["id"])
    assert observed["project_snapshot"]["id"] == project["id"]
    assert observed["turns"] == []
    assert await campaigns.campaigns(project["id"]) == []


async def test_production_campaign_current_fence_keeps_injected_native_repository(native_context, monkeypatch):
    from uuid import uuid4

    from firefly_forge import store as store_module

    store = native_context.get_bean(Store)

    def refused_constructor():
        raise AssertionError("Campaign admission constructed another campaign repository")

    monkeypatch.setattr(store_module, "ForgeCampaignRepository", refused_constructor)
    monkeypatch.setattr(store_module, "ForgeCampaignChildRepository", refused_constructor)
    async with store._unit():
        with pytest.raises(ValueError, match="no longer current"):
            await store.assert_campaign_current(
                {"campaign_context": {"campaign_id": uuid4().hex, "component_id": "api", "attempt": 1}}
            )
