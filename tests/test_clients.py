# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
import httpx
from click.testing import CliRunner

from firefly_forge import __version__
from firefly_forge.cli import main
from firefly_forge.client import ForgeClient
from firefly_forge.mcp_server import create_server
from firefly_forge.models import RunInput


def test_client_carries_authentication_and_validated_run():
    def handle(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer fixture-token"
        assert request.url.path == "/api/v1/runs"
        assert b'"skills"' not in request.content
        return httpx.Response(201, json={"id": "run-one", "status": "queued"})

    with ForgeClient("http://factory.invalid", "fixture-token") as client:
        client.http.close()
        client.http = httpx.Client(
            base_url="http://factory.invalid/api/v1/",
            headers={"Authorization": "Bearer fixture-token"},
            transport=httpx.MockTransport(handle),
        )
        result = client.create_run(RunInput(repository_id="repo-one", brief="Create real product"))
        assert result["id"] == "run-one"


async def test_mcp_tools_have_real_input_contracts():
    tools = {tool.name: tool for tool in await create_server().list_tools()}
    assert set(tools) == {
        "get_knowledge_configuration",
        "configure_koinon_knowledge",
        "check_koinon_knowledge",
        "get_project_knowledge_schema",
        "read_project_knowledge_chunks",
        "execute_project_knowledge_sql",
        "get_project_knowledge_sql_request",
        "get_final_outcome_candidate",
        "request_final_outcome_continuation",
        "get_final_outcome_status",
        "get_framework_knowledge_status",
        "check_framework_updates",
        "refresh_framework_knowledge",
        "get_framework_knowledge_refresh",
        "observe_framework_knowledge_refresh",
        "list_framework_corpora",
        "list_framework_corpus_sources",
        "search_framework_knowledge",
        "list_repositories",
        "configure_scm_polling_functional_grant",
        "get_scm_polling_functional_grant",
        "get_current_scm_polling_functional_grant",
        "review_scm_polling_functional",
        "connect_repository",
        "create_run",
        "request_run_creation",
        "get_run_creation_request",
        "cancel_run_creation_request",
        "get_run_creation_decision",
        "get_agent_checkpoint_candidates",
        "request_agent_checkpoint_restore",
        "get_agent_checkpoint_restore",
        "get_run",
        "get_run_diff_index",
        "get_run_diff_file",
        "cancel_run",
        "publish_run",
        "get_development_plan",
        "get_execution_journal",
        "query_run_evidence",
        "get_run_activity_content",
        "get_execution_requests",
        "request_runner_continuation",
        "get_runner_continuation",
        "get_runner_continuation_candidates",
        "reconcile_execution_request",
        "resume_run",
        "handoff_run",
        "list_source_connections",
        "connect_granted_repository",
        "list_scm_actor_binding_candidates",
        "configure_scm_actor_binding",
        "list_scm_actor_bindings",
        "get_scm_actor_binding",
        "configure_scm_resume_grant",
        "get_scm_resume_grant",
        "get_current_scm_resume_grant",
        "configure_scm_polling_resume_grant",
        "get_scm_polling_resume_grant",
        "get_current_scm_polling_resume_grant",
        "review_scm_polling_resume",
        "configure_scm_product_change_grant",
        "get_scm_product_change_grant",
        "get_current_scm_product_change_grant",
        "get_scm_product_change",
        "search_project_knowledge",
        "index_verified_source",
        "remember_verified_run",
        "get_learning_policy",
        "configure_learning_policy",
        "list_learning_records",
        "invalidate_learning",
        "get_learning_invalidation_receipt",
        "get_learning_projection_observation_receipt",
        "get_learning_projection_observation_attempt",
        "get_learning_projection",
        "submit_learning_projection",
        "observe_learning_projection",
        "observe_learning_projection_attempt",
        "list_deployment_targets",
        "deploy_verified_run",
        "rollback_deployment",
        "get_product_definition",
        "save_product_definition",
        "list_repository_receipts",
        "get_repository_workflow",
        "list_cloud_providers",
        "render_cloud_recipe",
        "list_applications",
        "get_application",
        "check_application",
        "list_incidents",
        "request_incident_maintenance",
        "resolve_incident",
        "list_projects",
        "configure_project",
        "get_project",
        "get_project_revision",
        "list_source_analyses",
        "analyze_project",
        "get_source_analysis",
        "review_source_analysis",
        "list_product_conversations",
        "start_product_conversation",
        "get_product_conversation",
        "get_product_conversation_capabilities",
        "recover_product_design_turn",
        "request_product_design_turn",
        "list_product_attachments",
        "upload_product_attachment",
        "get_product_attachment",
        "get_agent_roles",
        "get_agent_role_lifecycle",
        "get_repository_agent_role_lifecycle",
        "get_identity_configuration",
        "get_identity_session",
        "list_project_members",
        "set_project_membership",
        "list_identity_users",
        "find_project_eligible_users",
        "get_identity_administration",
        "list_identity_operations",
        "set_identity_user_enabled",
        "provision_identity_user",
        "invite_identity_user",
        "reconcile_identity_operation",
        "recover_identity_operation",
        "save_agent_roles",
        "get_repository_agent_roles",
        "save_repository_agent_roles",
        "get_worker_messages",
        "respond_worker_request",
        "list_project_campaigns",
        "launch_project_campaign",
        "get_project_campaign",
        "get_project_campaign_report",
        "archive_project_campaign_report",
        "get_project_campaign_report_archive",
        "list_project_campaign_report_archives",
        "save_project_campaign_delivery_plan",
        "list_project_campaign_delivery_plans",
        "get_project_campaign_delivery_plan",
        "get_project_campaign_delivery_progress",
        "cancel_project_campaign",
        "resume_project_campaign",
        "retry_project_campaign",
        "get_repository_runner",
        "save_repository_runner",
        "check_repository_runner",
        "probe_runner",
        "get_internet_search_configuration",
        "configure_internet_search",
        "test_internet_search",
        "search_internet",
        "get_search_citation",
        "get_browser_evidence_image",
        "preview_product_change",
        "apply_product_change",
        "list_model_profiles",
        "configure_model_profile",
        "test_model_profile",
        "get_task_routing",
        "save_task_routing",
        "get_tool_policy",
        "configure_tool_policy",
        "get_run_tool_invocations",
        "abandon_tool_invocation",
        "get_external_mcp_catalog",
        "configure_external_mcp",
        "discover_external_mcp",
        "review_external_mcp",
        "get_external_mcp_grants",
        "grant_external_mcp",
        "revoke_external_mcp",
        "get_connection_oauth_providers",
        "get_oauth_provider_policies",
        "configure_local_oauth_provider_policy",
        "list_connection_oauth",
        "get_connection_oauth_effect",
        "supersede_connection_oauth",
        "link_external_mcp_oauth",
        "get_connection_oauth",
        "configure_connection_oauth",
        "prepare_connection_oauth",
        "refresh_connection_oauth",
        "disconnect_connection_oauth",
        "get_git_delivery_policy",
        "configure_git_delivery_policy",
        "get_git_deliveries",
        "request_git_delivery",
        "get_git_delivery_request",
        "get_git_delivery_observations",
    }
    activity = tools["get_run_activity_content"]
    assert activity.input_schema["required"] == ["identifier", "event_id", "query"]
    assert activity.annotations.read_only_hint is True and activity.annotations.open_world_hint is False
    assert tools["get_run_diff_index"].input_schema["required"] == ["run_id", "expected_manifest"]
    assert tools["get_run_diff_file"].input_schema["required"] == ["run_id", "expected_manifest", "file_index"]
    for name in ("get_run_diff_index", "get_run_diff_file"):
        assert tools[name].annotations.read_only_hint is True
        assert tools[name].annotations.open_world_hint is False
    assert tools["create_run"].input_schema["required"] == ["repository_id", "brief"]
    assert "publish" not in tools["create_run"].input_schema["properties"]
    assert tools["request_incident_maintenance"].input_schema["required"] == ["identifier", "brief", "checks"]
    assert tools["invalidate_learning"].input_schema["required"] == ["record_id", "reason", "request_id"]
    assert tools["get_learning_invalidation_receipt"].input_schema["required"] == ["record_id", "request_id"]
    assert tools["submit_learning_projection"].input_schema["required"] == ["record_id", "request_id"]
    assert tools["observe_learning_projection"].input_schema["required"] == ["record_id", "request_id"]
    assert "source_id" in tools["observe_learning_projection"].input_schema["properties"]
    assert tools["observe_learning_projection_attempt"].input_schema["required"] == ["record_id", "request"]
    assert tools["get_learning_projection_observation_attempt"].input_schema["required"] == ["record_id", "attempt_id"]
    assert tools["observe_learning_projection_attempt"].annotations.read_only_hint is False
    assert tools["get_learning_projection_observation_attempt"].annotations.read_only_hint is True
    assert tools["list_scm_actor_binding_candidates"].input_schema["required"] == ["project_id", "repository_id"]
    assert tools["configure_scm_actor_binding"].input_schema["required"] == ["project_id", "repository_id", "request"]
    assert tools["list_scm_actor_bindings"].input_schema["required"] == ["project_id", "repository_id"]
    assert tools["get_scm_actor_binding"].input_schema["required"] == ["project_id", "repository_id", "request_id"]
    for name in ("get_final_outcome_candidate", "get_final_outcome_status"):
        assert tools[name].input_schema["required"] == ["identifier", "request_id"]
        assert tools[name].annotations.read_only_hint is True
        assert tools[name].annotations.open_world_hint is False
    final = tools["request_final_outcome_continuation"]
    assert final.input_schema["required"] == ["identifier", "request_id", "action_id", "request"]
    assert final.annotations.read_only_hint is False and final.annotations.idempotent_hint is True
    final_request = final.input_schema["properties"]["request"]
    assert final_request["additionalProperties"] is False
    for flag in (
        "acknowledge_already_consumed_final_output",
        "acknowledge_no_completed_effect_replay",
        "acknowledge_same_request_attempt_and_budgets",
        "acknowledge_finite_ownership_lease",
    ):
        assert flag in final_request["required"]
        assert final_request["properties"][flag]["const"] is True


def test_cli_version_and_command_help():
    runner = CliRunner()
    assert __version__ in runner.invoke(main, ["--version"]).output
    result = runner.invoke(main, ["run", "--help"])
    assert result.exit_code == 0
    assert "--check" in result.output and "--publish" in result.output


def test_client_projects_assistant_and_roles_use_native_authenticated_endpoints():
    import json

    from firefly_forge.agent_roles import WorkerResponse
    from firefly_forge.product_assistant import AssistantTurnInput
    from firefly_forge.project_models import ProjectDefinition
    from firefly_forge.reverse_engineering import AnalysisReview

    seen = []
    repository = "1" * 32
    project = "2" * 32
    analysis = "3" * 32
    conversation = "4" * 32
    run = "5" * 32

    def handle(request):
        assert request.headers["Authorization"] == "Bearer fixture-token"
        seen.append((request.method, request.url.path, json.loads(request.content) if request.content else None))
        return httpx.Response(
            200,
            json=[]
            if request.method == "GET"
            and request.url.path.endswith(("projects", "analyses", "worker-messages", "conversations"))
            else {"id": "result"},
        )

    with ForgeClient("http://factory.invalid", "fixture-token") as client:
        client.http.close()
        client.http = httpx.Client(
            base_url="http://factory.invalid/api/v1/",
            headers={"Authorization": "Bearer fixture-token"},
            transport=httpx.MockTransport(handle),
        )
        definition = ProjectDefinition(
            name="Stock",
            description="Stock product",
            topology="monorepo",
            owners=["owner"],
            components=[
                {
                    "id": "stock",
                    "name": "Stock API",
                    "repository_id": repository,
                    "responsibility": "Manage stock",
                    "path": ".",
                }
            ],
        )
        client.projects()
        client.configure_project(definition)
        client.configure_project(definition, project)
        client.project(project)
        client.project_revision(project, 1)
        client.analyze_project(project, ["stock"])
        client.project_analyses(project)
        client.project_analysis(project, analysis)
        client.review_project_analysis(project, analysis, AnalysisReview(digest="a" * 64, reviewed=True))
        client.assistant_conversations(repository)
        client.create_assistant_conversation(repository, project_id=project)
        client.assistant_conversation(repository, conversation)
        client.assistant_turn(
            repository, conversation, AssistantTurnInput(request_id="b" * 32, message="Define customer")
        )
        client.agent_roles()
        client.worker_messages(run)
        client.respond_worker(run, "question-1", WorkerResponse(id="answer-1", actor="operator", answer="Support"))
    assert ("POST", f"/api/v1/projects/{project}/analyses", {"component_ids": ["stock"]}) in seen
    assert (
        "POST",
        f"/api/v1/repositories/{repository}/assistant/conversations/{conversation}/turns",
        {"request_id": "b" * 32, "message": "Define customer", "attachment_ids": None},
    ) in seen
    assert seen[-1][1] == f"/api/v1/runs/{run}/worker-requests/question-1/respond"


def test_cli_component_run_inherits_stack_and_checks_without_sending_false_defaults(monkeypatch):
    import contextlib

    from firefly_forge import cli

    observed = []

    class Connection:
        def create_run(self, request):
            observed.append(request.model_dump(exclude_unset=True))
            return {"id": "run", "status": "queued"}

    monkeypatch.setattr(cli, "client", lambda: contextlib.nullcontext(Connection()))
    result = CliRunner().invoke(
        main, ["run", "--repository", "1" * 32, "--project", "2" * 32, "--component", "stock", "--brief", "Build stock"]
    )
    assert result.exit_code == 0, result.output
    assert observed[0]["project_id"] == "2" * 32 and observed[0]["component_id"] == "stock"
    assert "backend" not in observed[0] and "frontend" not in observed[0] and "commands" not in observed[0]


def test_native_projects_analysis_assistant_roles_sdk_cli_and_mcp_parity(tmp_path, postgres_schema, monkeypatch):
    import asyncio
    import contextlib
    import json
    import subprocess

    from starlette.testclient import TestClient

    from firefly_forge import cli
    from firefly_forge.api import create_application
    from firefly_forge.models import RepositoryInput
    from firefly_forge.product_assistant import ProductAssistant
    from firefly_forge.product_assistant_runtime import AssistantReply

    source = tmp_path / "source"
    source.mkdir()
    (source / "README.md").write_text("# Stock service\nAPI contract: assigned accounts only.\n")
    subprocess.run(["git", "init", "--initial-branch=main"], cwd=source, check=True, capture_output=True)
    subprocess.run(["git", "add", "."], cwd=source, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-m", "Fixture"],
        cwd=source,
        check=True,
        capture_output=True,
    )
    token = "native-sdk-fixture-token-32-characters"
    app = create_application(tmp_path / "native", token)

    class InProcessClient(TestClient):
        def request(self, *args, **kwargs):
            kwargs.pop("timeout", None)
            return super().request(*args, **kwargs)

    with InProcessClient(
        app,
        base_url="http://testserver/api/v1/",
        headers={"Authorization": "Bearer " + token},
        client=("127.0.0.1", 50000),
    ) as http:
        connection = ForgeClient("http://testserver", token)
        connection.http.close()
        connection.http = http
        monkeypatch.setattr(cli, "client", lambda: contextlib.nullcontext(connection))
        repo = connection.add_repository(RepositoryInput(name="Stock", url=str(source)))
        project = connection.configure_project(
            {
                "name": "Stock",
                "description": "Stock product",
                "topology": "monorepo",
                "owners": ["owner"],
                "components": [
                    {
                        "id": "stock",
                        "name": "Stock API",
                        "repository_id": repo["id"],
                        "responsibility": "Manage stock",
                        "path": ".",
                    }
                ],
            }
        )
        assert connection.project_revision(project["id"], 1)["digest"] == project["digest"]
        analysis = connection.analyze_project(project["id"])
        assert analysis["status"] == "ready_for_review"
        assert connection.project_analysis(project["id"], analysis["id"])["digest"] == analysis["digest"]
        reviewed = connection.review_project_analysis(
            project["id"], analysis["id"], {"digest": analysis["digest"], "reviewed": True}
        )
        assert reviewed["status"] == "reviewed"
        requests = []

        async def designer(prompt, settings):
            requests.append(prompt)
            return AssistantReply(message="Which account owners need the stock report?")

        from firefly_forge.knowledge import KnowledgeConfiguration, KnowledgeManager

        app.state.runtime.context.get_bean(KnowledgeManager).configure(
            KnowledgeConfiguration(values={"enabled": False, "required": False, "auto_memory": False})
        )
        app.state.runtime.context.get_bean(ProductAssistant).designer = designer
        conversation = connection.create_assistant_conversation(repo["id"], project_id=project["id"])
        turn = connection.assistant_turn(
            repo["id"], conversation["id"], {"request_id": "a" * 32, "message": "Define the stock customer"}
        )
        replay = connection.assistant_turn(
            repo["id"], conversation["id"], {"request_id": "a" * 32, "message": "Define the stock customer"}
        )
        assert turn["status"] == "completed" and replay["id"] == turn["id"] and len(requests) == 1
        assert connection.assistant_conversation(repo["id"], conversation["id"])["journal"]["verified"] is True
        roles = connection.agent_roles()
        assert len(roles["document"]["roles"]) == 7 and roles["community_skills"]
        assert connection.save_agent_roles(roles["document"])["digest"] == roles["digest"]
        assert connection.repository_roles(repo["id"])["inherited"] is True
        selection = {"skills": {"implementer": ["test-driven-development"]}}
        saved_defaults = connection.save_repository_roles(repo["id"], selection)
        assert saved_defaults["roles"]["skills"] == selection["skills"]
        result = CliRunner().invoke(main, ["project", "get", project["id"]])
        assert result.exit_code == 0 and json.loads(result.output)["id"] == project["id"]
        server = create_server()
        content = asyncio.run(server.call_tool("get_project", {"project_id": project["id"]}))
        assert project["id"] in str(content)
        content = asyncio.run(server.call_tool("get_agent_roles", {}))
        assert "implementation" in str(content)
        content = asyncio.run(server.call_tool("get_repository_agent_roles", {"repository_id": repo["id"]}))
        assert "test-driven-development" in str(content)
        result = CliRunner().invoke(main, ["roles", "repository-get", repo["id"]])
        assert result.exit_code == 0 and json.loads(result.output)["roles"]["skills"] == selection["skills"]


def test_client_campaign_runner_search_and_browser_evidence_contracts(tmp_path):
    import json

    seen = []
    png = b"\x89PNG\r\n\x1a\nfixture-image"

    def handle(request):
        assert request.headers["authorization"] == "Bearer fixture-token"
        seen.append((request.method, request.url.path, json.loads(request.content) if request.content else None))
        if request.url.path.endswith("/screenshot"):
            return httpx.Response(200, content=png, headers={"Content-Type": "image/png"})
        return httpx.Response(
            200,
            json=[] if request.method == "GET" and request.url.path.endswith("/campaigns") else {"status": "fixture"},
        )

    with ForgeClient("http://factory.invalid", "fixture-token") as connection:
        connection.http.close()
        connection.http = httpx.Client(
            base_url="http://factory.invalid/api/v1/",
            headers={"Authorization": "Bearer fixture-token"},
            transport=httpx.MockTransport(handle),
        )
        project = "1" * 32
        campaign = "2" * 32
        repo = "3" * 32
        evidence = "4" * 32
        connection.project_campaigns(project)
        connection.launch_campaign(project, {"request_id": "5" * 32, "brief": "Build account rule"})
        connection.project_campaign(project, campaign)
        connection.cancel_campaign(project, campaign, {"request_id": "6" * 32})
        connection.resume_campaign(project, campaign, {"request_id": "7" * 32})
        connection.retry_campaign(project, campaign, {"request_id": "8" * 32, "component_ids": ["api"]})
        connection.repository_runner(repo)
        connection.save_repository_runner(repo, {"mode": "host"})
        connection.runner_readiness(repo)
        connection.probe_runner({"mode": "host"})
        connection.search_configuration()
        connection.configure_search(api_key="private-search-key")
        connection.test_search()
        connection.internet_search("account owner documentation", 3)
        connection.search_citation("web-" + "a" * 24)
        assert connection.browser_screenshot(evidence) == png
        target = tmp_path / "evidence.png"
        receipt = connection.download_browser_screenshot(evidence, target)
        assert target.read_bytes() == png and target.stat().st_mode & 0o077 == 0 and receipt["size"] == len(png)
        import pytest

        with pytest.raises(FileExistsError):
            connection.download_browser_screenshot(evidence, target)
        with pytest.raises(ValueError):
            connection.search_citation("../private")
    assert len(seen) == 18
    assert (
        "POST",
        "/api/v1/projects/" + project + "/campaigns/" + campaign + "/retry",
        {"request_id": "8" * 32, "component_ids": ["api"]},
    ) in seen
    saved = next(payload for method, path, payload in seen if method == "PUT" and path.endswith("/runner"))
    assert saved["sandbox"]["mode"] == "host" and "timeout" in saved["sandbox"]


def test_cli_search_secret_file_is_private_and_stdout_contains_readiness_only(tmp_path, monkeypatch):
    import contextlib
    import json

    from firefly_forge import cli

    key = tmp_path / "brave.key"
    key.write_text("private-brave-key\n")
    key.chmod(0o644)
    payloads = []

    class Connection:
        def configure_search(self, *, api_key=None, clear_secret=False):
            payloads.append((api_key, clear_secret))
            return {"provider": "brave", "configured": bool(api_key)}

    monkeypatch.setattr(cli, "client", lambda: contextlib.nullcontext(Connection()))
    blocked = CliRunner().invoke(main, ["search", "configure", "--key-file", str(key)])
    assert blocked.exit_code != 0 and payloads == [] and "private-brave-key" not in blocked.output
    key.chmod(0o600)
    configured = CliRunner().invoke(main, ["search", "configure", "--key-file", str(key)])
    assert configured.exit_code == 0, configured.output
    assert json.loads(configured.output) == {"provider": "brave", "configured": True}
    assert payloads == [("private-brave-key", False)] and "private-brave-key" not in configured.output


def test_native_campaign_runner_search_and_image_sdk_cli_mcp_parity(tmp_path, postgres_schema, monkeypatch):
    import asyncio
    import base64
    import contextlib
    import json
    import subprocess

    from starlette.testclient import TestClient

    from firefly_forge import cli
    from firefly_forge.api import create_application
    from firefly_forge.browser_runner import BrowserRunner
    from firefly_forge.engine import Engine
    from firefly_forge.models import RepositoryInput
    from firefly_forge.search_tools import SearchTools

    async def idle_worker(self):
        await asyncio.Event().wait()

    monkeypatch.setattr(Engine, "_poll", idle_worker)
    source = tmp_path / "source"
    source.mkdir()
    (source / "README.md").write_text("# Stock source\n")
    for argv in [
        ["git", "init", "--initial-branch=main"],
        ["git", "add", "."],
        ["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-m", "Fixture"],
    ]:
        subprocess.run(argv, cwd=source, check=True, capture_output=True)
    token = "native-parity-fixture-token-32-characters"
    app = create_application(tmp_path / "native-campaign", token)

    class InProcessClient(TestClient):
        def request(self, *args, **kwargs):
            kwargs.pop("timeout", None)
            return super().request(*args, **kwargs)

    with InProcessClient(
        app,
        base_url="http://testserver/api/v1/",
        headers={"Authorization": "Bearer " + token},
        client=("127.0.0.1", 50000),
    ) as http:
        from native_client_knowledge_fixtures import activate_native_client_target

        http.portal.call(activate_native_client_target, app.state.store, tmp_path / "framework-target")
        connection = ForgeClient("http://testserver", token)
        connection.http.close()
        connection.http = http
        monkeypatch.setattr(cli, "client", lambda: contextlib.nullcontext(connection))
        repo = connection.add_repository(RepositoryInput(name="Stock", url=str(source)))
        project = connection.configure_project(
            {
                "name": "Stock",
                "description": "Account stock rules",
                "topology": "monorepo",
                "owners": ["owner"],
                "components": [
                    {
                        "id": "stock",
                        "name": "Stock API",
                        "repository_id": repo["id"],
                        "responsibility": "Stock visibility",
                    }
                ],
            }
        )
        assert connection.repository_runner(repo["id"])["source"] == "platform_default"
        assert connection.save_repository_runner(repo["id"], {"mode": "host"})["source"] == "repository"
        assert connection.runner_readiness(repo["id"])["execution"]["available"] is True
        assert connection.probe_runner({"mode": "host"})["available"] is True
        launch = {"request_id": "1" * 32, "brief": "Implement account visibility"}
        campaign = connection.launch_campaign(project["id"], launch)
        assert campaign["journal"]["verified"] is True and campaign["children"][0]["status"] == "queued"
        assert connection.launch_campaign(project["id"], launch)["id"] == campaign["id"]
        assert len(connection.project_campaigns(project["id"])) == 1
        assert connection.project_campaign(project["id"], campaign["id"])["journal"]["verified"] is True
        cancelled = connection.cancel_campaign(project["id"], campaign["id"], {"request_id": "2" * 32})
        assert cancelled["status"] == "cancelled"
        retry = connection.retry_campaign(
            project["id"], campaign["id"], {"request_id": "3" * 32, "component_ids": ["stock"]}
        )
        current = next(child for child in retry["children"] if child["current"])
        assert current["attempt"] == 2 and current["status"] == "queued"
        resumed = connection.resume_campaign(project["id"], campaign["id"], {"request_id": "4" * 32})
        assert resumed["journal"]["verified"] is True
        assert any(event["kind"] == "action_receipt" for event in resumed["journal"]["entries"])
        project_runs = [run for run in connection.request("GET", "runs") if run.get("project_id") == project["id"]]
        assert project_runs and all(run["status"] in {"queued", "cancelled"} for run in project_runs)
        assert all(not run.get("summary") for run in project_runs)
        calls = []
        key = "native-fixture-brave-key"

        def search_response(request):
            calls.append(request)
            assert request.headers["X-Subscription-Token"] == key
            return httpx.Response(
                200,
                json={
                    "web": {
                        "results": [
                            {
                                "url": "https://docs.fireflyframework.org/",
                                "title": "Firefly documentation",
                                "description": "Framework source documentation",
                            }
                        ]
                    }
                },
            )

        transport_client = httpx.AsyncClient(transport=httpx.MockTransport(search_response))
        service = app.state.runtime.context.get_bean(SearchTools)
        service.client = transport_client
        keyfile = tmp_path / "search.key"
        keyfile.write_text(key)
        keyfile.chmod(0o600)
        configured = CliRunner().invoke(main, ["search", "configure", "--key-file", str(keyfile)])
        assert configured.exit_code == 0, configured.output
        assert key not in configured.output and json.loads(configured.output)["configured"] is True
        assert (
            connection.search_configuration()["configured"] is True
            and "api_key" not in connection.search_configuration()
        )
        result = connection.internet_search("official Firefly documentation", 1)
        citation = result["results"][0]["citation_id"]
        assert connection.search_citation(citation)["url"] == "https://docs.fireflyframework.org/"
        assert connection.test_search()["result_count"] == 1 and len(calls) == 2
        image = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aB1sAAAAASUVORK5CYII="
        )
        evidence_id = "5" * 32
        runner = app.state.runtime.context.get_bean(BrowserRunner)
        image_path = runner.directory / (evidence_id + ".png")
        image_path.write_bytes(image)
        image_path.chmod(0o600)
        assert connection.browser_screenshot(evidence_id) == image
        output = tmp_path / "downloaded.png"
        download = CliRunner().invoke(main, ["runner", "screenshot", evidence_id, "--output", str(output)])
        assert download.exit_code == 0 and output.read_bytes() == image and output.stat().st_mode & 0o077 == 0
        server = create_server()
        content = asyncio.run(
            server.call_tool("get_project_campaign", {"project_id": project["id"], "campaign_id": campaign["id"]})
        )
        assert campaign["id"] in str(content)
        content = asyncio.run(server.call_tool("check_repository_runner", {"repository_id": repo["id"]}))
        assert "available" in str(content)
        content = asyncio.run(server.call_tool("get_search_citation", {"citation_id": citation}))
        assert citation in str(content)
        content = asyncio.run(server.call_tool("get_browser_evidence_image", {"evidence_id": evidence_id}))
        assert any(
            getattr(block, "type", None) == "image" and block.data == base64.b64encode(image).decode()
            for block in content.content
        )
        status = CliRunner().invoke(main, ["campaign", "get", project["id"], campaign["id"]])
        assert status.exit_code == 0 and json.loads(status.output)["id"] == campaign["id"]
        connection.configure_search(clear_secret=True)
        assert connection.search_configuration()["configured"] is False
        asyncio.run(transport_client.aclose())


async def test_new_mcp_schemas_are_typed_and_configuration_key_is_not_model_input():
    tools = {tool.name: tool for tool in await create_server().list_tools()}
    assert tools["launch_project_campaign"].input_schema["required"] == ["project_id", "request"]
    schema = tools["launch_project_campaign"].input_schema
    assert "request_id" in str(schema) and "dependency_gate" in str(schema) and "component_ids" in str(schema)
    assert tools["retry_project_campaign"].input_schema["required"] == ["project_id", "campaign_id", "request"]
    assert "api_key" not in tools["configure_internet_search"].input_schema["properties"]
    assert "api_key_file" in tools["configure_internet_search"].input_schema["properties"]
