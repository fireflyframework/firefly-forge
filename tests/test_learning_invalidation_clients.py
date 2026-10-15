# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Native receipt reads retain an uncertain request without another invalidation."""

import asyncio
import contextlib
import json
from uuid import uuid4

import httpx
import pytest
from click.testing import CliRunner
from starlette.testclient import TestClient
from test_learning_ledger import completed

from firefly_forge import cli
from firefly_forge.api import create_application
from firefly_forge.client import ForgeClient
from firefly_forge.engine import Engine
from firefly_forge.learning import LearningLedger
from firefly_forge.mcp_server import create_server


def test_native_lost_response_exact_receipt_and_three_read_clients(tmp_path, postgres_schema, monkeypatch):
    async def idle(_self):
        await asyncio.Event().wait()

    monkeypatch.setattr(Engine, "_poll", idle)
    token = "owned-learning-receipt-bootstrap-token-32chars"
    calls = []

    class NativeHTTP(TestClient):
        lose_response = False

        def request(self, method, url, *args, **kwargs):
            kwargs.pop("timeout", None)
            calls.append((method, str(url)))
            response = super().request(method, url, *args, **kwargs)
            if self.lose_response and method == "POST" and str(url).endswith("/invalidate"):
                self.lose_response = False
                assert response.status_code == 200
                raise httpx.ReadTimeout("Owned fixture lost response after actual commit")
            return response

    with NativeHTTP(
        create_application(tmp_path / "api", token),
        base_url="http://testserver/api/v1/",
        headers={"Authorization": "Bearer " + token},
        client=("127.0.0.1", 52000),
    ) as http:

        async def seed():
            store = http.app.state.store
            engine, run = await completed(store, tmp_path, scoped=True)
            ledger = LearningLedger(store, engine.journal)
            ledger.engine = engine
            return await ledger.capture_outcome(run["id"])

        record = http.portal.call(seed)
        connection = ForgeClient("http://testserver", token)
        connection.http.close()
        connection.http = http
        monkeypatch.setattr(cli, "client", lambda: contextlib.nullcontext(connection))
        request_id = uuid4().hex
        reason = "Reviewed source changed after verification"
        http.lose_response = True
        with pytest.raises(httpx.ReadTimeout):
            connection.invalidate_learning(record["id"], reason, request_id)
        receipt = connection.learning_invalidation_receipt(record["id"], request_id)
        assert receipt["request_id"] == request_id and receipt["record_id"] == record["id"]
        assert receipt["event_state"] == "invalidated" and receipt["current_record_state"] == "invalidated"
        assert receipt["reason"] == reason and len(receipt["actor_id"]) == 32
        command = ["learning", "invalidation-receipt", record["id"], "--request-id", request_id]
        result = CliRunner().invoke(cli.main, command)
        assert result.exit_code == 0, result.output
        assert json.loads(result.output) == receipt
        result = asyncio.run(
            create_server().call_tool(
                "get_learning_invalidation_receipt", {"record_id": record["id"], "request_id": request_id}
            )
        )
        assert not result.is_error and result.structured_content == receipt
        missing = connection.http.get(f"learning/records/{record['id']}/invalidations/{uuid4().hex}")
        assert missing.status_code == 404
        unauthorized = http.get(
            f"learning/records/{record['id']}/invalidations/{request_id}", headers={"Authorization": "Bearer invalid"}
        )
        assert unauthorized.status_code == 401
        assert sum(method == "POST" and path.endswith("/invalidate") for method, path in calls) == 1
        paths = http.get("openapi.json").json()["paths"]
        route = paths["/api/v1/learning/records/{id}/invalidations/{request_id}"]
        assert set(route) == {"get"}
        schema = http.get("openapi.json").json()["components"]["schemas"]["LearningInvalidationReceipt"]
        assert {
            "request_id",
            "record_id",
            "event_id",
            "digest",
            "actor_id",
            "event_state",
            "current_record_state",
        } <= set(schema["properties"])
        assert schema["properties"]["event_state"]["const"] == "invalidated"


@pytest.mark.parametrize("record,request_id", [("../another-record", "a" * 32), ("a" * 32, "../another-request")])
def test_malformed_receipt_identity_does_not_dispatch(record, request_id, monkeypatch):
    with ForgeClient("https://forge.invalid", "owned-token") as connection:
        calls = []
        monkeypatch.setattr(connection, "request", lambda *a, **k: calls.append((a, k)))
        with pytest.raises(ValueError):
            connection.learning_invalidation_receipt(record, request_id)
        assert not calls


async def test_receipt_tool_is_read_only_and_has_only_two_required_identifiers():
    tools = {tool.name: tool for tool in await create_server().list_tools()}
    tool = tools["get_learning_invalidation_receipt"]
    assert tool.input_schema["required"] == ["record_id", "request_id"]
    annotations = tool.annotations.model_dump(by_alias=True)
    assert annotations["readOnlyHint"] and annotations["idempotentHint"]
    assert not annotations["destructiveHint"] and not annotations["openWorldHint"]
