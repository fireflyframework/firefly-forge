# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Typed SDK observations reject foreign or altered source windows."""

import copy
from uuid import UUID

import httpx
import pytest
from test_source_explorer_host import prepared

from firefly_forge.client import ForgeClient
from firefly_forge.identity import Principal
from firefly_forge.project_access import CURRENT_PRINCIPAL
from firefly_forge.source_explorer import SourceExplorerController


async def payload(tmp_path):
    explorer, ref, _, _, _ = await prepared(tmp_path)
    token = CURRENT_PRINCIPAL.set(Principal(UUID(int=7), "issuer", "user"))
    try:
        body = await explorer.file(
            ref.run_id, ref.snapshot_id, ref.catalog.manifest_sha256, page=0, entry_index=0, offset=0, limit=5
        )
        catalog = await explorer.catalog(ref.run_id, ref.snapshot_id, ref.catalog.manifest_sha256, page=0)
        response = await SourceExplorerController(explorer).file(
            ref.run_id, ref.snapshot_id, 0, ref.catalog.manifest_sha256, page=0, limit=5
        )
        assert response.headers["Cache-Control"] == "private, no-store"
        return ref, body, catalog
    finally:
        CURRENT_PRINCIPAL.reset(token)


async def test_sdk_carries_exact_snapshot_manifest_and_validates_file_window(tmp_path):
    ref, body, catalog = await payload(tmp_path)
    requests = []

    def handle(request):
        requests.append(request)
        assert request.method == "GET" and request.url.params["expected_manifest"] == ref.catalog.manifest_sha256
        return httpx.Response(200, json=catalog if request.url.path.endswith("/catalog") else body)

    with ForgeClient("https://factory.invalid", "controlled-token") as client:
        client.http.close()
        client.http = httpx.Client(base_url="https://factory.invalid/api/v1/", transport=httpx.MockTransport(handle))
        page = client.run_source_catalog(ref.run_id, ref.snapshot_id, ref.catalog.manifest_sha256, page=0)
        data = client.run_source_file(
            ref.run_id, ref.snapshot_id, ref.catalog.manifest_sha256, page=0, entry_index=0, limit=5
        )
    assert page.catalog.entries[0].path == "app.py" and data.text == "print"
    assert len(requests) == 2 and all("/source/revisions/" + ref.snapshot_id in r.url.path for r in requests)


@pytest.mark.parametrize(
    "mutation",
    ["run", "snapshot", "manifest", "offset", "page", "index", "content", "digest", "text", "binary", "extent"],
)
async def test_sdk_refuses_foreign_or_forged_source_response_without_retry(tmp_path, mutation):
    ref, body, _ = await payload(tmp_path)
    body = copy.deepcopy(body)
    if mutation in {"run", "snapshot", "manifest"}:
        if mutation == "manifest":
            body["reference"]["catalog"]["manifest_sha256"] = "f" * 64
        else:
            body["reference"]["run_id" if mutation == "run" else "snapshot_id"] = "f" * 32
    elif mutation == "offset":
        body["offset"] = 1
    elif mutation == "page":
        body["catalog_page"] = 1
    elif mutation == "index":
        body["entry_index"] = 1
    elif mutation == "content":
        body["content_base64"] = "ZmFrZQ=="
    elif mutation == "digest":
        body["sha256"] = "f" * 64
    elif mutation == "text":
        body["text"] = "invented"
    elif mutation == "binary":
        body["binary"] = True
    elif mutation == "extent":
        body["entry"]["byte_count"] = 1
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(200, json=body)

    with ForgeClient("https://factory.invalid", "controlled-token") as client:
        client.http.close()
        client.http = httpx.Client(base_url="https://factory.invalid/api/v1/", transport=httpx.MockTransport(handle))
        with pytest.raises(ValueError):
            client.run_source_file(
                ref.run_id, ref.snapshot_id, ref.catalog.manifest_sha256, page=0, entry_index=0, limit=5
            )
    assert len(calls) == 1
