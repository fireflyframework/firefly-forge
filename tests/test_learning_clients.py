# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
import asyncio
import contextlib
import json

from click.testing import CliRunner
from starlette.testclient import TestClient

from firefly_forge import cli
from firefly_forge.api import create_application
from firefly_forge.client import ForgeClient
from firefly_forge.engine import Engine
from firefly_forge.mcp_server import create_server


def test_learning_policy_native_api_sdk_cli_and_mcp_are_the_same_contract(tmp_path, postgres_schema, monkeypatch):
    async def idle(_self):
        await asyncio.Event().wait()

    monkeypatch.setattr(Engine, "_poll", idle)
    token = "fixture-learning-owner-token-32characters"

    class InProcess(TestClient):
        def request(self, *args, **kwargs):
            kwargs.pop("timeout", None)
            return super().request(*args, **kwargs)

    with InProcess(
        create_application(tmp_path, token),
        base_url="http://testserver/api/v1/",
        headers={"Authorization": "Bearer " + token},
        client=("127.0.0.1", 50000),
    ) as http:
        connection = ForgeClient("http://testserver", token)
        connection.http.close()
        connection.http = http
        monkeypatch.setattr(cli, "client", lambda: contextlib.nullcontext(connection))
        current = connection.learning_policy()
        assert current["settings"]["enabled"] and current["settings"]["max_age_hours"] == 72
        changed = connection.configure_learning_policy({"max_age_hours": 24})
        assert changed["digest"] != current["digest"] and changed["settings"]["max_age_hours"] == 24
        assert http.get("learning/policy", headers={"Authorization": "Bearer invalid"}).status_code == 401
        assert http.put("learning/policy", json={"max_age_hours": 0}).status_code == 422
        viewed = CliRunner().invoke(cli.main, ["learning", "policy"])
        assert viewed.exit_code == 0 and json.loads(viewed.output) == changed
        result = asyncio.run(create_server().call_tool("get_learning_policy", {}))
        assert not result.is_error and "24" in str(result)
        paths = http.get("openapi.json").json()["paths"]
        assert "/api/v1/learning/records/{id}/projection/observe" in paths


async def test_campaign_captures_knowledge_before_deriving_native_capabilities(store, tmp_path):
    import uuid
    from types import SimpleNamespace

    from test_mcp_registry_native import project_definition

    from firefly_forge.agent_roles import ProjectRoles, stage_allowed_tools
    from firefly_forge.journal import Journal
    from firefly_forge.knowledge import KnowledgeConfiguration, KnowledgeManager
    from firefly_forge.learning_configuration import configure_learning
    from firefly_forge.learning_models import LearningPolicy
    from firefly_forge.models import RepositoryInput
    from firefly_forge.project_campaigns import CampaignLaunch, ProjectCampaigns
    from firefly_forge.projects import Projects
    from firefly_forge.runner_configuration import RunnerConfiguration
    from firefly_forge.search_tools import SearchTools

    manager = KnowledgeManager(store)
    from test_knowledge import retained_configuration

    retained_configuration(manager.private)
    manager.configure(
        KnowledgeConfiguration(
            values={
                "enabled": True,
                "endpoint": "http://127.0.0.1:18500",
                "api_key": "fixture-knowledge-campaign-key",
                "tenant_id": "owned",
            }
        )
    )
    repo = await store.add_repository(RepositoryInput(name="Owned source", url="https://github.com/team/product.git"))
    definition = project_definition(repo["id"])
    definition.roles = ProjectRoles(allowed_tools={"implementer": ["knowledge_lookup", "write_file"]})
    project = await Projects(store).configure(definition)
    store.runtime_roles = SimpleNamespace(role="api", executes_jobs=False)
    store.runners = RunnerConfiguration(store, SimpleNamespace(), SearchTools(tmp_path / "search"), SimpleNamespace())

    async def source_base(repository):
        return "a" * 40

    campaigns = ProjectCampaigns(
        store, Engine(store, tmp_path / "workspaces"), Journal(store), source_resolver=source_base
    )
    campaign = await campaigns.launch(
        project["id"], CampaignLaunch(request_id=uuid.uuid4().hex, brief="Read scoped knowledge", agent="agentic")
    )
    child = await store.get_run(campaign["children"][0]["run_id"])
    assert "knowledge_lookup" in stage_allowed_tools(child["role_snapshot"], "implement", "agentic")
    configure_learning(store.configuration, LearningPolicy(enabled=False))
    assert (await store.get_run(child["id"]))["learning_snapshot"]["settings"]["enabled"]


async def test_legacy_campaign_learning_fields_remain_absent_and_one_sided_capture_is_denied(store, tmp_path):
    import copy

    import pytest
    from test_role_execution import fixture_run

    from firefly_forge.journal import Journal
    from firefly_forge.project_campaigns import ProjectCampaigns

    run = await fixture_run(store, tmp_path)
    campaigns = ProjectCampaigns(store, Engine(store, tmp_path / "workspaces"), Journal(store))
    vector = campaigns._configuration_vector(run["repository_id"], {run["agent"]})
    original = copy.deepcopy(run)
    legacy_run = {key: value for key, value in run.items() if key != "learning_snapshot"}
    legacy_vector = {key: value for key, value in vector.items() if key != "learning_policy"}
    campaigns._validate_admission_snapshots(legacy_run, legacy_vector)
    assert run == original
    with pytest.raises(ValueError, match="learning policy"):
        campaigns._validate_admission_snapshots(legacy_run, vector)
    with pytest.raises(ValueError, match="learning policy"):
        campaigns._validate_admission_snapshots(run, legacy_vector)
