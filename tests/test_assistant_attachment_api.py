# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Real PyFly HTTP admission, conversation scoping and bounded file transport."""

import base64
import hashlib
import uuid

from starlette.testclient import TestClient

from firefly_forge.api import create_application

TOKEN = "test-only-attachment-operator-token-32-characters"


def test_file_http_roundtrip_replay_scope_and_public_receipts(tmp_path, postgres_schema):
    with TestClient(create_application(tmp_path, TOKEN), client=("127.0.0.1", 50000)) as client:
        headers = {"Authorization": f"Bearer {TOKEN}"}
        repo = client.post("/api/v1/repositories", headers=headers, json={"name": "Files", "url": str(tmp_path)}).json()
        conversations = f"/api/v1/repositories/{repo['id']}/assistant/conversations"
        session = client.post(conversations, headers=headers, json={}).json()
        other = client.post(conversations, headers=headers, json={}).json()
        route = f"{conversations}/{session['id']}/attachments"
        raw = b"Customer journey: reserve stock without overselling.\n" * 14_000
        request = {
            "request_id": uuid.uuid4().hex,
            "filename": "Customer journeys.md",
            "content_base64": base64.b64encode(raw).decode(),
        }
        assert len(request["content_base64"]) > 100_000
        assert client.post(route, json=request).status_code == 401
        response = client.post(route, headers=headers, json=request)
        assert response.status_code == 201, response.text
        receipt = response.json()
        assert receipt["sha256"] == hashlib.sha256(raw).hexdigest()
        assert receipt["truncated"] is True and receipt["text_chars"] == 30_000
        assert not {"raw_bytes", "extracted_text", "canonical_image", "text_sha256"}.intersection(receipt)
        assert client.post(route, headers=headers, json=request).json() == receipt
        assert client.get(route, headers=headers).json() == [receipt]
        assert client.get(f"{route}/{receipt['id']}", headers=headers).json() == receipt
        download = client.get(f"{route}/{receipt['id']}/content", headers=headers)
        assert download.status_code == 200 and download.content == raw
        assert download.headers["content-disposition"].startswith("attachment;")
        assert "Customer%20journeys.md" in download.headers["content-disposition"]
        assert client.get(f"{route}/{receipt['id']}/content").status_code == 401
        foreign = f"{conversations}/{other['id']}/attachments/{receipt['id']}"
        assert client.get(foreign, headers=headers).status_code == 404
        assert client.get(foreign + "/content", headers=headers).status_code == 404
        changed = {**request, "content_base64": base64.b64encode(b"different journey").decode()}
        assert client.post(route, headers=headers, json=changed).status_code == 409
        invalid = {**request, "request_id": uuid.uuid4().hex, "filename": "invalid.png"}
        assert client.post(route, headers=headers, json=invalid).status_code == 409
        assert client.get(route, headers=headers).json() == [receipt]
        assert client.get(f"{conversations}/{session['id']}", headers=headers).json()["turns"] == []
        assert client.get("/api/v1/runs", headers=headers).json() == []


def test_file_stream_limit_cannot_bypass_request_budget(tmp_path, postgres_schema):
    with TestClient(create_application(tmp_path, TOKEN), client=("127.0.0.1", 50000)) as client:
        response = client.post(
            "/api/v1/repositories/" + "a" * 32 + "/assistant/conversations/" + "b" * 32 + "/attachments",
            headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"},
            content=iter([b" " * 800_000, b" " * 800_000]),
        )
        assert response.status_code == 413
