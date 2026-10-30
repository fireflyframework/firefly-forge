# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Source and memory write surfaces retain typed identities and one-shot dispatch."""

import httpx
import pytest

from firefly_forge.client import ForgeClient
from firefly_forge.mcp_server import create_server

RUN, REPOSITORY, REQUEST = "a" * 32, "b" * 32, "c" * 32
BODY = {"request_id": REQUEST, "filename": "source.md", "content_base64": "YWJj"}


def test_sdk_ingestion_and_readback_keep_exact_ids_without_upstream_retry():
    calls = []
    with ForgeClient("https://forge.example", "controlled-fixture") as client:
        client.http.close()
        client.http = httpx.Client(
            base_url="https://forge.example/api/v1/",
            transport=httpx.MockTransport(
                lambda request: calls.append(request) or httpx.Response(200, json={"status": "outcome_unknown"})
            ),
        )
        client.ingest_koinon_source(RUN, BODY)
        client.upload_koinon_document(REPOSITORY, BODY)
        client.koinon_ingestion(RUN, "source", REQUEST)
        client.observe_koinon_ingestion(RUN, "source", REQUEST, "remote-source")
        client.knowledge_upload(REPOSITORY, REQUEST)
        client.reconcile_knowledge_upload(REPOSITORY, REQUEST, "remote-source")
        assert [request.method for request in calls] == ["POST", "POST", "GET", "POST", "GET", "POST"]
        assert calls[0].url.path.endswith("/ingestion/source")
        assert calls[1].url.path.endswith("/knowledge/koinon/uploads")
        before = len(calls)
        with pytest.raises(ValueError):
            client.observe_koinon_ingestion(RUN, "foreign", REQUEST, "remote-source")
        with pytest.raises(ValueError):
            client.ingest_koinon_source(RUN, {**BODY, "nonce": "invented"})
        assert len(calls) == before


async def test_mcp_native_ingestion_operations_are_typed_and_do_not_offer_approval_nonce():
    tools = {tool.name: tool for tool in await create_server().list_tools()}
    for name in (
        "ingest_koinon_source",
        "upload_koinon_document",
        "observe_koinon_ingestion",
        "reconcile_knowledge_upload",
    ):
        assert tools[name].annotations.read_only_hint is False
        assert tools[name].annotations.idempotent_hint is True
    assert tools["get_koinon_ingestion"].annotations.read_only_hint is True
    assert tools["get_knowledge_upload"].annotations.read_only_hint is True
    schema = tools["ingest_koinon_source"].input_schema
    assert set(schema.get("$defs", {}).get("KoinonSourceInput", schema["properties"]["request"])["required"]) == {
        "request_id",
        "filename",
        "content_base64",
    }
    assert "nonce" not in str(schema)
