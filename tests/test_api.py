# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
from pathlib import Path

import pytest
from pyfly.server.management_server import resolve_management_mode
from starlette.testclient import TestClient

from firefly_forge.api import create_application

TOKEN = "test-only-factory-token-32-characters"


def test_api_requires_operator_token_and_validates_requests(tmp_path: Path):
    app = create_application(tmp_path, TOKEN)
    assert app.state.runtime.config.get("pyfly.management.enabled") is False
    assert app.state.runtime.config.get("pyfly.admin.enabled") is False
    assert resolve_management_mode(app.state.runtime.config, 8787)[0] == "disabled"
    with TestClient(app) as client:
        assert client.get("/api/v1/repositories").status_code == 401
        headers = {"Authorization": f"Bearer {TOKEN}"}
        assert client.get("/api/v1/health", headers=headers).json()["status"] == "ok"
        assert (
            client.post("/api/v1/repositories", headers=headers, json={"name": "test", "url": "--evil"}).status_code
            == 422
        )
        repository = client.post(
            "/api/v1/repositories",
            headers=headers,
            json={"name": "test", "url": "https://github.com/example/demo.git", "provider": "github"},
        ).json()
        assert repository["id"]
        assert client.get("/api/v1/repositories", headers=headers).json()[0]["id"] == repository["id"]
        assert (
            client.post(
                "/api/v1/runs", headers=headers, json={"repository_id": "missing", "brief": "Make product"}
            ).status_code
            == 404
        )
        assert client.get("/api/v1/runs/missing", headers=headers).status_code == 404
        assert client.get("/api/v1/openapi.json", headers=headers).json()["openapi"] == "3.1.0"


def test_empty_token_rejected(tmp_path: Path):
    with pytest.raises(ValueError, match="token"):
        create_application(tmp_path, "")


def test_malformed_json_and_invalid_artifact_paths(tmp_path: Path):
    with TestClient(create_application(tmp_path, TOKEN)) as client:
        headers = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}
        assert client.post("/api/v1/repositories", headers=headers, content="bad-json").status_code == 422
        assert client.get("/api/v1/runs/../../private/artifacts/changes.zip", headers=headers).status_code in (404, 400)


def test_server_excludes_unexpected_host_header(tmp_path: Path):
    with TestClient(create_application(tmp_path, TOKEN)) as client:
        assert client.get("/", headers={"Host": "attacker.invalid"}).status_code == 400
