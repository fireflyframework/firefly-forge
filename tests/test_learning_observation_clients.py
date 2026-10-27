# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Native observation recovery uses exact local receipts without remote replay."""

import asyncio
import contextlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from uuid import uuid4

import httpx
import pytest
from click.testing import CliRunner
from starlette.testclient import TestClient
from test_learning_observation_receipts import admitted_projection

from firefly_forge import cli
from firefly_forge.api import create_application
from firefly_forge.client import ForgeClient
from firefly_forge.engine import Engine
from firefly_forge.knowledge import KnowledgeManager
from firefly_forge.mcp_server import create_server
from firefly_forge.store import identifier


@pytest.fixture
def canon_reads():
    fixture = {"source": {}, "calls": [], "errors": []}

    class CanonReads(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def respond(self, payload):
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def authorized(self):
            assert self.headers["X-API-Key"] == "owned-projection-key-no-provider"
            assert self.headers["X-Tenant-ID"] == "owned"
            assert self.headers["X-Workspace-ID"].startswith("forge-learning-")

        def do_GET(self):
            try:
                self.authorized()
                fixture["calls"].append(("GET", self.path))
                assert self.path == "/api/v1/sources/owned-source"
                self.respond(fixture["source"])
            except Exception as exc:
                fixture["errors"].append(type(exc).__name__)
                self.send_error(500)

        def do_POST(self):
            try:
                self.authorized()
                fixture["calls"].append(("POST", self.path))
                assert self.path == "/api/v1/search"
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                assert body["source_ids"] == ["owned-source"] and body["top_k"] == 8
                self.respond(
                    {
                        "elapsed_ms": 1,
                        "hits": [
                            {
                                "source_id": "owned-source",
                                "chunk_id": "owned-chunk",
                                "content": "Owned evidence",
                                "score": 1,
                            }
                        ],
                    }
                )
            except Exception as exc:
                fixture["errors"].append(type(exc).__name__)
                self.send_error(500)

    server = ThreadingHTTPServer(("127.0.0.1", 0), CanonReads)
    fixture["endpoint"] = f"http://127.0.0.1:{server.server_port}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield fixture
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)
        assert not thread.is_alive()


def test_native_observation_lost_response_and_three_exact_read_clients(
    tmp_path, postgres_schema, monkeypatch, canon_reads
):
    from native_client_knowledge_fixtures import activate_native_client_target

    async def idle(_self):
        await asyncio.Event().wait()

    monkeypatch.setattr(Engine, "_poll", idle)
    token = "owned-observation-bootstrap-token-32chars"
    calls = []

    class NativeHTTP(TestClient):
        lose_response = False

        def request(self, method, url, *args, **kwargs):
            kwargs.pop("timeout", None)
            calls.append((method, str(url)))
            response = super().request(method, url, *args, **kwargs)
            if self.lose_response and method == "POST" and str(url).endswith("/observe"):
                self.lose_response = False
                assert response.status_code == 200, response.text
                raise httpx.ReadTimeout("Owned lost response after committed observation")
            return response

    with NativeHTTP(
        create_application(tmp_path / "api", token),
        base_url="http://testserver/api/v1/",
        headers={"Authorization": "Bearer " + token},
        client=("127.0.0.1", 52000),
    ) as http:

        async def seed():
            remote = await activate_native_client_target(http.app.state.store, tmp_path / "framework-prerequisite")
            return await admitted_projection(
                http.app.state.store, tmp_path, remote_service=canon_reads, framework_remote=remote
            )

        service, record, _ = http.portal.call(seed)
        actual_client = service.knowledge.client
        monkeypatch.setattr(KnowledgeManager, "client", lambda _self, *a, **k: actual_client(*a, **k))
        connection = ForgeClient("http://testserver", token)
        connection.http.close()
        connection.http = http
        monkeypatch.setattr(cli, "client", lambda: contextlib.nullcontext(connection))
        request_id = uuid4().hex
        http.lose_response = True
        with pytest.raises(httpx.ReadTimeout):
            connection.observe_learning_projection(record["id"], request_id, "owned-source")
        receipt = connection.learning_projection_observation_receipt(record["id"], request_id)
        assert receipt["request_id"] == request_id and receipt["actor_id"] is not None
        assert receipt["projection_id"] == connection.learning_projection(record["id"])["id"]
        command = ["learning", "observation-receipt", record["id"], "--request-id", request_id]
        result = CliRunner().invoke(cli.main, command)
        assert result.exit_code == 0, result.output
        assert json.loads(result.output) == receipt
        tool = asyncio.run(
            create_server().call_tool(
                "get_learning_projection_observation_receipt", {"record_id": record["id"], "request_id": request_id}
            )
        )
        assert not tool.is_error and tool.structured_content == receipt
        missing = http.get(f"learning/records/{record['id']}/projection/observations/{uuid4().hex}")
        assert missing.status_code == 404
        unauthorized = http.get(
            f"learning/records/{record['id']}/projection/observations/{request_id}",
            headers={"Authorization": "Bearer invalid"},
        )
        assert unauthorized.status_code == 401
        assert sum(method == "POST" and path.endswith("/observe") for method, path in calls) == 1
        assert canon_reads["calls"] == [("GET", "/api/v1/sources/owned-source"), ("POST", "/api/v1/search")]
        assert not canon_reads["errors"]

        async def committed():
            async with service.store._unit():
                return [
                    event
                    for event in await service.ledger.events.for_record(identifier(record["id"]))
                    if event.data["kind"] == "projection_observation"
                ]

        events = http.portal.call(committed)
        assert len(events) == 1 and events[0].digest == receipt["digest"]
        contract = http.get("openapi.json").json()
        route = contract["paths"]["/api/v1/learning/records/{id}/projection/observations/{request_id}"]
        assert set(route) == {"get"}
        schema = contract["components"]["schemas"]["LearningProjectionObservationReceipt"]
        assert schema["properties"]["event_state"]["const"] == "verified"
        assert set(receipt) == set(schema["properties"])


@pytest.mark.parametrize("record,request_id", [("../other", "a" * 32), ("a" * 32, "../other")])
def test_malformed_observation_receipt_ids_never_dispatch(record, request_id, monkeypatch):
    with ForgeClient("https://forge.invalid", "owned-token") as connection:
        calls = []
        monkeypatch.setattr(connection, "request", lambda *a, **k: calls.append((a, k)))
        with pytest.raises(ValueError):
            connection.learning_projection_observation_receipt(record, request_id)
        assert not calls


async def test_observation_tool_read_contract():
    tools = {tool.name: tool for tool in await create_server().list_tools()}
    tool = tools["get_learning_projection_observation_receipt"]
    assert tool.input_schema["required"] == ["record_id", "request_id"]
    annotations = tool.annotations.model_dump(by_alias=True)
    assert annotations["readOnlyHint"] and annotations["idempotentHint"]
    assert not annotations["destructiveHint"] and not annotations["openWorldHint"]


@pytest.mark.parametrize("source_id", [None, "owned-source"])
async def test_source_reclassified_as_credential_never_leaves_forge(
    store, tmp_path, monkeypatch, canon_reads, source_id
):
    from test_learning_invalidation_receipts import retained

    service, record, _ = await admitted_projection(store, tmp_path, remote_service=canon_reads)
    before = await retained(service.ledger, record)
    request_id = uuid4().hex
    monkeypatch.setenv("FORGE_TEST_API_TOKEN", "owned-source")
    with pytest.raises(ValueError, match="credentials") as failure:
        await service.observe(record["id"], source_id=source_id, request_id=request_id)
    assert "owned-source" not in str(failure.value)
    assert canon_reads["calls"] == []
    assert not canon_reads["errors"]
    assert (await service.status(record["id"]))["state"] == "source_admitted"
    assert await retained(service.ledger, record) == before
