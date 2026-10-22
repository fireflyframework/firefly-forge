# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Controlled HTTP transport for the typed immutable screenshot client; no server."""

import hashlib

import httpx
import pytest

from firefly_forge.client import ForgeClient

RUN = "a" * 32
PNG = b"\x89PNG\r\n\x1a\npublic-fixture"
IMAGE = {
    "id": "b" * 64,
    "filename": "choice.png",
    "mime_type": "image/png",
    "bytes": len(PNG),
    "sha256": hashlib.sha256(PNG).hexdigest(),
}


def test_sdk_lists_strict_opaque_metadata_and_returns_only_exact_admitted_bytes():
    from firefly_forge.worker_evidence import WorkerImage

    calls = []

    def transport(request):
        calls.append(request)
        assert request.headers["authorization"] == "Bearer public-fixture-token"
        assert "token=" not in str(request.url)
        if request.url.path.endswith("/worker-evidence"):
            assert dict(request.url.params) == {"request_id": "ownership"}
            return httpx.Response(200, json=[IMAGE])
        assert request.url.path.endswith("/worker-evidence/" + IMAGE["id"])
        return httpx.Response(
            200, content=PNG, headers={"Content-Type": IMAGE["mime_type"], "X-Content-SHA256": IMAGE["sha256"]}
        )

    with ForgeClient("https://forge.example.test", "public-fixture-token") as client:
        client.http.close()
        client.http = httpx.Client(
            base_url="https://forge.example.test/api/v1/",
            headers={"Authorization": "Bearer public-fixture-token"},
            transport=httpx.MockTransport(transport),
        )
        metadata = client.worker_evidence(RUN, "ownership")
        assert metadata == [WorkerImage.model_validate(IMAGE)]
        assert client.worker_image(RUN, metadata[0]) == PNG
        assert len(calls) == 2 and all(request.method == "GET" for request in calls)


@pytest.mark.parametrize("case", ["hash", "size", "mime", "oversize", "denied"])
def test_sdk_does_not_return_unverified_or_unauthorized_image_bytes(case):
    headers = {"Content-Type": IMAGE["mime_type"], "X-Content-SHA256": IMAGE["sha256"]}
    raw = PNG
    if case == "hash":
        raw = PNG[:-1] + b"x"
    if case == "size":
        raw = PNG[:-1]
    if case == "mime":
        headers["Content-Type"] = "image/svg+xml"
    if case == "oversize":
        raw = b"x" * 1_000_001
    with ForgeClient("https://forge.example.test", "public-fixture-token") as client:
        client.http.close()
        client.http = httpx.Client(
            base_url="https://forge.example.test/api/v1/",
            transport=httpx.MockTransport(
                lambda _: httpx.Response(403 if case == "denied" else 200, content=raw, headers=headers)
            ),
        )
        with pytest.raises((ValueError, RuntimeError)):
            client.worker_image(RUN, IMAGE)
