# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Additive clients bind a single v2 request and exact readback."""

import asyncio
import contextlib
import json

import httpx
import pytest
from click.testing import CliRunner
from mcp.server.mcpserver.exceptions import UnexpectedToolError
from starlette.testclient import TestClient
from test_learning_observation_clients import canon_reads as canon_reads_fixture
from test_learning_observation_receipts import admitted_projection

from firefly_forge import cli
from firefly_forge.api import create_application
from firefly_forge.client import ForgeClient
from firefly_forge.engine import Engine
from firefly_forge.knowledge import KnowledgeManager
from firefly_forge.mcp_server import create_server

canon_reads = canon_reads_fixture


async def test_v2_tools_publish_native_strict_input_and_read_only_receipt_contract():
    from firefly_forge.learning_observation_attempt_models import LearningObservationAttemptInput

    tools = {tool.name: tool for tool in await create_server().list_tools()}
    write = tools["observe_learning_projection_attempt"]
    assert write.input_schema["properties"]["request"] == LearningObservationAttemptInput.model_json_schema()
    assert write.input_schema["required"] == ["record_id", "request"]
    assert write.annotations.read_only_hint is False and write.annotations.idempotent_hint is False
    read = tools["get_learning_projection_observation_attempt"]
    assert read.annotations.read_only_hint is True and read.annotations.open_world_hint is False


@pytest.mark.parametrize("field,value", [("source_id", True), ("state", "not_dispatched")])
def test_invalid_v2_input_never_dispatches_or_reflects_sensitive_fields(monkeypatch, field, value):
    from test_learning_observation_attempt_models import packet

    with ForgeClient("https://forge.invalid", "owned-token") as connection:
        calls = []
        monkeypatch.setattr(connection, "request", lambda *a, **k: calls.append((a, k)))
        with pytest.raises(ValueError) as failure:
            connection.observe_learning_projection_attempt(
                "1" * 32, {**packet(), field: value, "token": "owned-private-canary"}
            )
        assert calls == [] and "owned-private-canary" not in str(failure.value)


@pytest.mark.parametrize("interface", ["sdk", "cli", "mcp"])
def test_native_v2_exact_request_clients_and_lost_response(
    tmp_path, postgres_schema, monkeypatch, canon_reads, interface
):
    async def idle(_self):
        await asyncio.Event().wait()

    monkeypatch.setattr(Engine, "_poll", idle)
    token = "owned-v2-attempt-bootstrap-token-32chars"
    calls = []

    class NativeHTTP(TestClient):
        lose_response = True

        def request(self, method, url, *args, **kwargs):
            kwargs.pop("timeout", None)
            calls.append((method, str(url)))
            result = super().request(method, url, *args, **kwargs)
            if self.lose_response and method == "POST" and str(url).endswith("/observation-attempts"):
                self.lose_response = False
                assert result.status_code == 200, result.text
                raise httpx.ReadTimeout("Owned lost response")
            return result

    with NativeHTTP(
        create_application(tmp_path / "api", token),
        base_url="http://testserver/api/v1/",
        headers={"Authorization": "Bearer " + token},
        client=("127.0.0.1", 52000),
    ) as http:

        async def seed():
            return await admitted_projection(http.app.state.store, tmp_path, remote_service=canon_reads)

        service, record, _ = http.portal.call(seed)
        actual_client = service.knowledge.client
        monkeypatch.setattr(KnowledgeManager, "client", lambda _self, *a, **k: actual_client(*a, **k))
        status = http.get(f"learning/records/{record['id']}/projection").json()
        body = {
            "attempt_id": "obs2_" + "a" * 32,
            "source_id": "owned-source",
            "projection_id": status["id"],
            "submission_request_id": status["request_id"],
            "workspace_id": status["workspace_id"],
            "target_revision": status["target"]["revision"],
            "payload_digest": status["payload_digest"],
            "content_sha256": status["content_sha256"],
        }
        connection = ForgeClient("http://testserver", token)
        connection.http.close()
        connection.http = http
        monkeypatch.setattr(cli, "client", lambda: contextlib.nullcontext(connection))
        if interface == "sdk":
            with pytest.raises(httpx.ReadTimeout):
                connection.observe_learning_projection_attempt(record["id"], body)
        elif interface == "cli":
            request_file = tmp_path / "fictional-observation.json"
            request_file.write_text(json.dumps(body))
            result = CliRunner().invoke(cli.main, ["learning", "observe-attempt", record["id"], str(request_file)])
            assert result.exit_code != 0
        else:
            with pytest.raises(UnexpectedToolError):
                asyncio.run(
                    create_server().call_tool(
                        "observe_learning_projection_attempt",
                        {
                            "record_id": record["id"],
                            "request": body,
                        },
                    )
                )
        receipt = connection.learning_projection_observation_attempt(record["id"], body["attempt_id"])
        assert receipt["state"] == "verified" and receipt["observation"]["request_id"] != body["attempt_id"][5:]
        result = CliRunner().invoke(
            cli.main, ["learning", "observation-attempt", record["id"], "--attempt-id", body["attempt_id"]]
        )
        assert result.exit_code == 0, result.output
        assert json.loads(result.output) == receipt
        tool = asyncio.run(
            create_server().call_tool(
                "get_learning_projection_observation_attempt",
                {
                    "record_id": record["id"],
                    "attempt_id": body["attempt_id"],
                },
            )
        )
        assert not tool.is_error and tool.structured_content == receipt
        assert sum(method == "POST" and path.endswith("/observation-attempts") for method, path in calls) == 1
        assert len(canon_reads["calls"]) == 2 and not canon_reads["errors"]
        response = http.post(
            f"learning/records/{record['id']}/projection/observe",
            json={
                "request_id": body["attempt_id"],
                "source_id": "owned-source",
            },
        )
        assert response.status_code == 422
