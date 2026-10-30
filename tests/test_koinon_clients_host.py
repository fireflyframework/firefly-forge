# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Factory SDK and MCP preserve Koinon identities and explicit SQL-only operations."""

import json

import httpx
import pytest

from firefly_forge.client import ForgeClient
from firefly_forge.mcp_server import create_server

REPOSITORY, RUN, REQUEST = "a" * 32, "b" * 32, "c" * 32


def test_native_platform_client_validates_scope_before_authenticated_api_requests():
    seen = []

    def handle(request):
        assert request.headers["Authorization"] == "Bearer factory-fixture"
        seen.append(request)
        return httpx.Response(200, json={"provider": "koinon", "status": "outcome_unknown"})

    with ForgeClient("https://forge.example", "factory-fixture") as client:
        client.http.close()
        client.http = httpx.Client(
            base_url="https://forge.example/api/v1/",
            headers={"Authorization": "Bearer factory-fixture"},
            transport=httpx.MockTransport(handle),
        )
        client.knowledge_schema(REPOSITORY, "dataset")
        client.knowledge_chunks(REPOSITORY, "document", offset=3, limit=17, expected_identity="d" * 64)
        client.query_knowledge_sql(RUN, {"request_id": REQUEST, "dataset_id": "dataset", "sql": "SELECT 1"})
        client.knowledge_sql_request(RUN, REQUEST)
        assert [r.method for r in seen] == ["GET", "GET", "POST", "GET"]
        assert seen[0].url.params["repository_id"] == REPOSITORY
        assert seen[1].url.params["expected_identity"] == "d" * 64
        assert json.loads(seen[2].content) == {"request_id": REQUEST, "dataset_id": "dataset", "sql": "SELECT 1"}
        assert seen[3].url.path.endswith(f"/{RUN}/knowledge/queries/{REQUEST}")
        for call in (
            lambda: client.knowledge_schema("foreign/path", "dataset"),
            lambda: client.query_knowledge_sql(
                RUN, {"request_id": REQUEST, "dataset_id": "dataset", "question": "Generate SQL"}
            ),
            lambda: client.knowledge_chunks(REPOSITORY, "document", offset=True),
        ):
            with pytest.raises(ValueError):
                call()
        assert len(seen) == 4


async def test_new_mcp_tools_keep_typed_sql_and_read_only_observation_contracts():
    tools = {x.name: x for x in await create_server().list_tools()}
    schema = tools["get_project_knowledge_schema"]
    assert schema.annotations.read_only_hint is True
    sql = tools["execute_project_knowledge_sql"]
    definitions = sql.input_schema.get("$defs", {})
    body = definitions.get("KoinonSqlInput", sql.input_schema["properties"]["request"])
    assert set(body["required"]) == {"request_id", "dataset_id", "sql"}
    assert body["additionalProperties"] is False and "question" not in body["properties"]
    assert tools["get_project_knowledge_sql_request"].annotations.read_only_hint is True
    assert tools["read_project_knowledge_chunks"].annotations.read_only_hint is True
    assert "configure_koinon_knowledge" in tools and "check_koinon_knowledge" in tools


def test_invalid_configuration_error_does_not_echo_unstored_credentials():
    with ForgeClient("https://forge.example", "factory-fixture") as client, pytest.raises(ValueError) as raised:
        client.configure_koinon_knowledge({"access_token": "private-unstored-token", "provider": "wrong"})
    assert "private-unstored-token" not in str(raised.value)


@pytest.mark.parametrize(
    "path,method,permission",
    [
        ("knowledge/schema?repository_id=" + REPOSITORY, "GET", "read"),
        ("knowledge/chunks?repository_id=" + REPOSITORY, "GET", "read"),
        ("runs/" + RUN + "/knowledge/queries", "POST", "execute"),
        ("runs/" + RUN + "/knowledge/queries/" + REQUEST, "GET", "read"),
    ],
)
async def test_current_project_grants_classify_new_knowledge_routes(path, method, permission):
    from types import SimpleNamespace

    from starlette.requests import Request
    from test_koinon_knowledge_host import ACTOR

    from firefly_forge.access_routes import AccessRoutes

    calls = []

    async def admin(user):
        return False

    async def require(user, scope, needed):
        assert user == ACTOR
        calls.append((scope, needed))

    async def run(identifier):
        assert identifier == RUN
        return {"project_id": "project"}

    async def receive():
        return {"type": "http.request", "body": b"{}"}

    route, _, query = path.partition("?")
    request = Request(
        {"type": "http", "method": method, "path": "/api/v1/" + route, "query_string": query.encode(), "headers": []},
        receive,
    )
    access = SimpleNamespace(current_administrator=admin, require_repository=require, require_project=require)
    await AccessRoutes(access, SimpleNamespace(get_run=run)).authorize(ACTOR, request)
    assert calls == [(REPOSITORY if path.startswith("knowledge/") else "project", permission)]


def test_openapi_exposes_typed_connection_sql_and_versioned_chunk_parameters():
    from firefly_forge.api import openapi_document

    value = openapi_document()
    sql = value["components"]["schemas"]["KoinonSqlInput"]
    assert set(sql["required"]) == {"request_id", "dataset_id", "sql"}
    assert sql["additionalProperties"] is False
    paths = value["paths"]
    chunk = paths["/api/v1/knowledge/chunks"]["get"]
    assert {parameter["name"] for parameter in chunk["parameters"]} == {
        "repository_id",
        "document_id",
        "offset",
        "limit",
        "expected_identity",
    }
    assert paths["/api/v1/knowledge/koinon/configuration"]["put"]["requestBody"]["content"]["application/json"][
        "schema"
    ]["$ref"].endswith("/KoinonConnectionInput")
