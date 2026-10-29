# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Archive clients bind one explicit write and exact reads to native campaign scope."""

import asyncio
import contextlib
import json
from uuid import uuid4

import httpx
import pytest
from click.testing import CliRunner
from starlette.testclient import TestClient
from test_project_campaigns import one_component

from firefly_forge import cli
from firefly_forge.api import create_application
from firefly_forge.client import ForgeClient
from firefly_forge.mcp_server import create_server


def test_archive_sdk_binds_exact_payload_and_never_retries_unknown_transport():
    project, campaign, request_id = uuid4().hex, uuid4().hex, uuid4().hex
    calls = []

    def dispatch(request):
        calls.append(request)
        raise httpx.ReadTimeout("Fictional response lost")

    with ForgeClient("https://forge.invalid", "owned-token") as connection:
        connection.http.close()
        connection.http = httpx.Client(
            base_url="https://forge.invalid/api/v1/", transport=httpx.MockTransport(dispatch)
        )
        with pytest.raises(httpx.ReadTimeout):
            connection.archive_campaign_report(
                project, campaign, {"request_id": request_id, "expected_report_digest": "a" * 64}
            )
    assert len(calls) == 1 and calls[0].method == "POST"
    assert calls[0].url.path == f"/api/v1/projects/{project}/campaigns/{campaign}/report-archives"
    assert json.loads(calls[0].content) == {"request_id": request_id, "expected_report_digest": "a" * 64}


@pytest.mark.parametrize(
    "body",
    [
        {"request_id": True, "expected_report_digest": "a" * 64},
        {"request_id": "a" * 32, "expected_report_digest": False},
        {"request_id": "a" * 32, "expected_report_digest": "a" * 64, "report": {"secret": "not-reflected"}},
    ],
)
def test_archive_client_strict_validation_never_reflects_input_or_dispatches(body, monkeypatch):
    calls = []
    with ForgeClient("https://forge.invalid", "owned-token") as connection:
        monkeypatch.setattr(connection, "request", lambda *args, **kwargs: calls.append(args))
        with pytest.raises(ValueError) as error:
            connection.archive_campaign_report(uuid4().hex, uuid4().hex, body)
    assert calls == [] and "not-reflected" not in str(error.value)


@pytest.mark.parametrize("cursor", [True, 1.5, "1"])
async def test_mcp_cursor_cannot_coerce_non_integer_inputs_before_sdk_validation(monkeypatch, cursor):
    from mcp.server.mcpserver.exceptions import UnexpectedToolError

    calls = []
    with ForgeClient("https://forge.invalid", "owned-token") as connection:
        monkeypatch.setattr(connection, "request", lambda *args, **kwargs: calls.append(args))
        monkeypatch.setattr(cli, "client", lambda: contextlib.nullcontext(connection))
        with pytest.raises(UnexpectedToolError):
            await create_server().call_tool(
                "list_project_campaign_report_archives",
                {
                    "project_id": uuid4().hex,
                    "campaign_id": uuid4().hex,
                    "after": cursor,
                },
            )
        assert calls == []


@pytest.mark.parametrize("interface", ["sdk", "cli", "mcp"])
def test_all_archive_clients_use_actual_native_di_routes(tmp_path, postgres_schema, monkeypatch, interface):
    from native_client_knowledge_fixtures import activate_native_client_target

    from firefly_forge.campaign_report_history import CampaignReportHistory
    from firefly_forge.campaign_report_history_controllers import CampaignReportHistoryController

    token = "owned-report-history-token-32chars"
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
        history = context.get_bean(CampaignReportHistory)
        assert context.get_bean(CampaignReportHistoryController).manager is history
        assert history.store is http.app.state.store
        assert history.store.runtime_roles.role == "api" and history.reports.campaigns.poller is None

        async def seed():
            await activate_native_client_target(http.app.state.store, tmp_path / "framework-prerequisite")
            return await one_component(http.app.state.store, tmp_path)

        project, manager, journal, campaign = http.portal.call(seed)
        with ForgeClient("http://testserver", token) as connection:
            connection.http.close()
            connection.http = http
            report = connection.project_campaign_report(project["id"], campaign["id"])
            calls.clear()
            body = {"request_id": uuid4().hex, "expected_report_digest": report["digest"]}
            monkeypatch.setattr(cli, "client", lambda: contextlib.nullcontext(connection))
            if interface == "sdk":
                saved = connection.archive_campaign_report(project["id"], campaign["id"], body)
                exact = connection.campaign_report_archive(project["id"], campaign["id"], body["request_id"])
                page = connection.campaign_report_archives(project["id"], campaign["id"])
            elif interface == "cli":

                def invoke(*args):
                    output = CliRunner().invoke(cli.main, ["campaign", *args])
                    assert output.exit_code == 0, output.output
                    return json.loads(output.output)

                saved = invoke(
                    "archive-report",
                    project["id"],
                    campaign["id"],
                    "--request-id",
                    body["request_id"],
                    "--expected-digest",
                    report["digest"],
                )
                exact = invoke("report-archive", project["id"], campaign["id"], body["request_id"])
                page = invoke("report-history", project["id"], campaign["id"])
            else:

                async def invoke(name, **args):
                    result = await create_server().call_tool(
                        name, {"project_id": project["id"], "campaign_id": campaign["id"], **args}
                    )
                    assert not result.is_error, str(result)
                    return result.structured_content

                saved = asyncio.run(invoke("archive_project_campaign_report", request=body))
                exact = asyncio.run(invoke("get_project_campaign_report_archive", request_id=body["request_id"]))
                page = asyncio.run(invoke("list_project_campaign_report_archives"))
            assert saved == exact and saved["report"] == report
            assert page["items"][0]["receipt_digest"] == saved["digest"]
            base = f"projects/{project['id']}/campaigns/{campaign['id']}/report-archives"
            assert calls == [("POST", base), ("GET", base + "/" + body["request_id"]), ("GET", base)]
            assert http.get(base, headers={"Authorization": "Bearer invalid-token"}).status_code == 401
            assert http.get(base.replace(project["id"], uuid4().hex)).status_code == 404
            assert http.get(base + "/" + uuid4().hex).status_code == 404
            assert http.get(base, params={"after": "true"}).status_code == 422
            assert http.portal.call(manager.campaign, project["id"], campaign["id"])["journal"] == campaign["journal"]
