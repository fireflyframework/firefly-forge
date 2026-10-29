# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Explicit reporting clients preserve request identity and never execute or archive implicitly."""

import asyncio
import contextlib
import json
from uuid import uuid4

import httpx
import pytest
from click.testing import CliRunner
from starlette.testclient import TestClient
from test_campaign_delivery_progress import fixture
from test_project_campaigns import one_component

from firefly_forge import cli
from firefly_forge.api import create_application
from firefly_forge.client import ForgeClient
from firefly_forge.mcp_server import create_server


def test_sdk_sends_one_exact_plan_request_and_never_retries_a_lost_response():
    project, campaign = uuid4().hex, uuid4().hex
    body = fixture()[0]["request"]
    calls = []

    def transport(request):
        calls.append(request)
        raise httpx.ReadTimeout("Fictional lost plan reply")

    with ForgeClient("https://forge.invalid", "owned-token") as connection:
        connection.http.close()
        connection.http = httpx.Client(
            base_url="https://forge.invalid/api/v1/", transport=httpx.MockTransport(transport)
        )
        with pytest.raises(httpx.ReadTimeout):
            connection.save_campaign_delivery_plan(project, campaign, body)
    assert len(calls) == 1
    assert calls[0].method == "POST"
    assert calls[0].url.path == f"/api/v1/projects/{project}/campaigns/{campaign}/delivery-plan-revisions"
    assert json.loads(calls[0].content) == body


@pytest.mark.parametrize("invalid", [True, {"request_id": "unregistered-secret-input"}])
def test_sdk_rejects_invalid_plan_without_reflection_or_dispatch(monkeypatch, invalid):
    calls = []
    with ForgeClient("https://forge.invalid", "owned-token") as connection:
        monkeypatch.setattr(connection, "request", lambda *args, **kwargs: calls.append(args))
        with pytest.raises(ValueError) as error:
            connection.save_campaign_delivery_plan(uuid4().hex, uuid4().hex, invalid)
        assert "unregistered-secret-input" not in str(error.value)
    assert calls == []


async def test_mcp_nested_plan_schema_is_usable_and_validation_remains_credential_safe(monkeypatch):
    from jsonschema import Draft202012Validator
    from mcp.server.mcpserver.exceptions import UnexpectedToolError

    server = create_server()
    tools = {tool.name: tool for tool in await server.list_tools()}
    tool = tools["save_project_campaign_delivery_plan"]
    body = fixture()[0]["request"]
    args = {"project_id": uuid4().hex, "campaign_id": uuid4().hex, "request": body}
    Draft202012Validator.check_schema(tool.input_schema)
    Draft202012Validator(tool.input_schema).validate(args)
    assert tool.annotations.read_only_hint is False and tool.annotations.idempotent_hint is True
    for name in (
        "list_project_campaign_delivery_plans",
        "get_project_campaign_delivery_plan",
        "get_project_campaign_delivery_progress",
    ):
        assert tools[name].annotations.read_only_hint is True
    calls = []
    with ForgeClient("https://forge.invalid", "owned-token") as connection:
        monkeypatch.setattr(connection, "request", lambda *a, **k: calls.append(a))
        monkeypatch.setattr(cli, "client", lambda: contextlib.nullcontext(connection))
        for invalid in (True, {"request_id": "unregistered-secret-input"}):
            with pytest.raises(UnexpectedToolError) as failure:
                await server.call_tool("save_project_campaign_delivery_plan", {**args, "request": invalid})
            assert "unregistered-secret-input" not in str(failure.value)
            assert "unregistered-secret-input" not in str(failure.value.__cause__)
    assert calls == []


@pytest.mark.parametrize("interface", ["sdk", "cli", "mcp"])
def test_reporting_clients_traverse_native_api_di_without_execution(tmp_path, postgres_schema, monkeypatch, interface):
    from native_client_knowledge_fixtures import activate_native_client_target

    from firefly_forge.campaign_delivery_plan_controllers import CampaignDeliveryPlanController
    from firefly_forge.campaign_delivery_plans import CampaignDeliveryPlans

    token = "owned-delivery-plan-native-token-32chars"
    calls = []

    class NativeHTTP(TestClient):
        def request(self, method, url, *args, **kwargs):
            kwargs.pop("timeout", None)
            calls.append((method, str(url)))
            return super().request(method, url, *args, **kwargs)

    with NativeHTTP(
        create_application(tmp_path / "api", token, role="api"),
        base_url="http://testserver/api/v1/",
        headers={"Authorization": "Bearer " + token},
        client=("127.0.0.1", 52000),
    ) as http:
        context = http.app.state.runtime.context
        plans = context.get_bean(CampaignDeliveryPlans)
        assert context.get_bean(CampaignDeliveryPlanController).manager is plans
        assert plans.store is http.app.state.store and plans.history.reports is plans.reports
        assert plans.store.runtime_roles.role == "api" and plans.reports.campaigns.poller is None

        async def seed():
            await activate_native_client_target(http.app.state.store, tmp_path / "framework-prerequisite")
            return await one_component(http.app.state.store, tmp_path)

        project, manager, journal, campaign = http.portal.call(seed)
        with ForgeClient("http://testserver", token) as connection:
            connection.http.close()
            connection.http = http
            report = connection.project_campaign_report(project["id"], campaign["id"])
            archived = connection.archive_campaign_report(
                project["id"],
                campaign["id"],
                {
                    "request_id": uuid4().hex,
                    "expected_report_digest": report["digest"],
                },
            )
            body = fixture()[0]["request"]
            body.update(
                request_id=uuid4().hex,
                expected_project_digest=report["campaign"]["project_digest"],
                expected_campaign_request_digest=report["campaign"]["request_digest"],
            )
            body["milestones"][0]["conditions"] = [
                {"kind": "component_verified", "component_id": "api", "task_id": None}
            ]
            calls.clear()
            monkeypatch.setattr(cli, "client", lambda: contextlib.nullcontext(connection))
            if interface == "sdk":
                saved = connection.save_campaign_delivery_plan(project["id"], campaign["id"], body)
                exact = connection.campaign_delivery_plan(project["id"], campaign["id"], body["request_id"])
                page = connection.campaign_delivery_plans(project["id"], campaign["id"])
                progress = connection.campaign_delivery_progress(
                    project["id"], campaign["id"], body["request_id"], archived["request_id"]
                )
            elif interface == "cli":
                path = tmp_path / "owned-plan.json"
                path.write_text(json.dumps(body))

                def invoke(*args):
                    output = CliRunner().invoke(cli.main, ["campaign", *args])
                    assert output.exit_code == 0, output.output
                    return json.loads(output.output)

                saved = invoke("plan-save", project["id"], campaign["id"], str(path))
                exact = invoke("plan-get", project["id"], campaign["id"], body["request_id"])
                page = invoke("plan-list", project["id"], campaign["id"])
                progress = invoke(
                    "plan-progress",
                    project["id"],
                    campaign["id"],
                    body["request_id"],
                    "--report-request-id",
                    archived["request_id"],
                )
            else:

                async def invoke(name, **args):
                    result = await create_server().call_tool(
                        name, {"project_id": project["id"], "campaign_id": campaign["id"], **args}
                    )
                    assert not result.is_error, str(result)
                    return result.structured_content

                saved = asyncio.run(invoke("save_project_campaign_delivery_plan", request=body))
                exact = asyncio.run(invoke("get_project_campaign_delivery_plan", request_id=body["request_id"]))
                page = asyncio.run(invoke("list_project_campaign_delivery_plans"))
                progress = asyncio.run(
                    invoke(
                        "get_project_campaign_delivery_progress",
                        request_id=body["request_id"],
                        report_request_id=archived["request_id"],
                    )
                )
            assert saved == exact
            assert page["items"][0]["receipt_digest"] == saved["digest"]
            assert progress["criteria_binding"] == "reporting_only_post_admission"
            assert progress["enforced_at_admission"] is False and progress["state"] == "in_progress"
            base = f"projects/{project['id']}/campaigns/{campaign['id']}/delivery-plan-revisions"
            assert calls == [
                ("POST", base),
                ("GET", base + "/" + body["request_id"]),
                ("GET", base),
                ("GET", base + "/" + body["request_id"] + "/progress"),
            ]
            assert http.get(base, headers={"Authorization": "Bearer invalid-owned-token"}).status_code == 401
            assert http.get(base.replace(project["id"], uuid4().hex)).status_code == 404
            assert http.portal.call(manager.campaign, project["id"], campaign["id"])["journal"] == campaign["journal"]
