# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""All report clients use the same native authenticated read route and service graph."""

import asyncio
import contextlib
import json
from uuid import uuid4

import pytest
from click.testing import CliRunner
from starlette.testclient import TestClient
from test_project_campaigns import one_component

from firefly_forge import cli
from firefly_forge.api import create_application
from firefly_forge.campaign_reports import CampaignReports
from firefly_forge.client import ForgeClient
from firefly_forge.engine import Engine
from firefly_forge.mcp_server import create_server
from firefly_forge.project_campaign_controllers import ProjectCampaignController
from firefly_forge.store import Store


@pytest.mark.parametrize("interface", ["sdk", "cli", "mcp"])
def test_native_report_di_and_authenticated_client_routes(tmp_path, postgres_schema, monkeypatch, interface):
    async def idle(_self):
        await asyncio.Event().wait()

    monkeypatch.setattr(Engine, "_poll", idle)
    token = "owned-campaign-report-native-token-32chars"
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
        reports = context.get_bean(CampaignReports)
        assert context.get_bean(ProjectCampaignController).reports is reports
        assert reports.store is context.get_bean(Store)
        assert reports.store.runtime_roles.role == "api"
        assert reports.campaigns.poller is None

        async def seed():
            return await one_component(http.app.state.store, tmp_path)

        project, manager, journal, campaign = http.portal.call(seed)
        with ForgeClient("http://testserver", token) as connection:
            connection.http.close()
            connection.http = http
            monkeypatch.setattr(cli, "client", lambda: contextlib.nullcontext(connection))
            if interface == "sdk":
                result = connection.project_campaign_report(project["id"], campaign["id"])
            elif interface == "cli":
                output = CliRunner().invoke(cli.main, ["campaign", "report", project["id"], campaign["id"]])
                assert output.exit_code == 0, output.output
                result = json.loads(output.output)
            else:
                output = asyncio.run(
                    create_server().call_tool(
                        "get_project_campaign_report", {"project_id": project["id"], "campaign_id": campaign["id"]}
                    )
                )
                assert not output.is_error, str(output)
                result = output.structured_content
            assert result["campaign"]["id"] == campaign["id"] and result["state"] == "in_progress"
            path = f"projects/{project['id']}/campaigns/{campaign['id']}/report"
            assert calls == [("GET", path)]
            assert http.get(path, headers={"Authorization": "Bearer invalid-owned-token"}).status_code == 401
            assert http.get(f"projects/{uuid4().hex}/campaigns/{campaign['id']}/report").status_code == 404
            assert (
                http.portal.call(manager.campaign, project["id"], campaign["id"])["journal"]["head"]
                == campaign["journal"]["head"]
            )


async def test_report_tool_is_read_only_bounded_and_requires_both_scope_ids():
    tools = {tool.name: tool for tool in await create_server().list_tools()}
    tool = tools["get_project_campaign_report"]
    assert len(tools) == 172
    assert tool.input_schema["required"] == ["project_id", "campaign_id"]
    assert tool.annotations.read_only_hint is True and tool.annotations.idempotent_hint is True
    assert tool.annotations.open_world_hint is False and tool.annotations.destructive_hint is False


def test_report_sdk_rejects_bad_scope_before_dispatch_without_reflecting_it(monkeypatch):
    with ForgeClient("https://forge.invalid", "owned-token") as connection:
        calls = []
        monkeypatch.setattr(connection, "request", lambda *a, **k: calls.append((a, k)))
        with pytest.raises(ValueError) as error:
            connection.project_campaign_report("owned-private-invalid-id", uuid4().hex)
        assert "owned-private-invalid-id" not in str(error.value)
        assert calls == []
