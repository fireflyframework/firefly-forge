# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Loopback provider and Forge servers exercise authenticated routes without external writes."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import socket
import threading
import time
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
import pytest
import uvicorn
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from firefly_forge.api import create_application
from firefly_forge.scm_connections import SCMConnections

OPERATOR = "fixture-only-forge-operator-token-32"
SECRET = "fixture-only-webhook-secret"


class LoopbackProviderTransport(httpx.AsyncBaseTransport):
    def __init__(self, origin):
        self.origin = httpx.URL(origin)

    async def handle_async_request(self, request):
        target = request.url.copy_with(scheme="http", host="127.0.0.1", port=self.origin.port)
        headers = {**request.headers, "host": request.url.host}
        async with httpx.AsyncClient(trust_env=False) as client:
            response = await client.request(
                request.method, target, headers=headers, content=await request.aread(), follow_redirects=False
            )
        return httpx.Response(response.status_code, headers=response.headers, content=response.content, request=request)


@contextmanager
def forge_server(app):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="critical", access_log=False, ws="none"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 15
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert server.started, "Forge HTTP server did not start"
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=15)
        listener.close()
        assert not thread.is_alive(), "Forge HTTP server did not stop"


def comment(provider, comment_id):
    if provider == "github":
        body = {
            "action": "created",
            "repository": {"id": 42},
            "sender": {"id": 7},
            "issue": {"number": 12, "pull_request": {}},
            "comment": {"id": comment_id, "body": "/forge project"},
        }
        headers = {"X-GitHub-Event": "issue_comment", "X-GitHub-Delivery": f"delivery-{comment_id}"}
    elif provider == "gitlab":
        body = {
            "object_kind": "note",
            "project": {"id": 42},
            "user": {"id": 7},
            "object_attributes": {"id": comment_id, "note": "/forge project", "noteable_type": "MergeRequest"},
            "merge_request": {"iid": 12},
        }
        headers = {"X-Gitlab-Event": "Note Hook", "webhook-id": f"delivery-{comment_id}", "X-Gitlab-Token": SECRET}
    else:
        body = {
            "repository": {"uuid": "{repository-42}"},
            "actor": {"uuid": "{actor-7}"},
            "pullrequest": {"id": 12},
            "comment": {"id": comment_id, "content": {"raw": "/forge project"}},
        }
        headers = {"X-Event-Key": "pullrequest:comment_created", "X-Request-UUID": f"delivery-{comment_id}"}
    raw = json.dumps(body).encode()
    if provider != "gitlab":
        header = "X-Hub-Signature-256" if provider == "github" else "X-Hub-Signature"
        headers[header] = "sha256=" + hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()
    return raw, headers


@pytest.mark.parametrize("provider", ["github", "gitlab", "bitbucket"])
def test_authenticated_provider_http_signed_ingress_feedback_and_reconciliation(tmp_path, postgres_schema, provider):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    host = {
        "github": "github.enterprise.example",
        "gitlab": "gitlab.selfmanaged.example",
        "bitbucket": "bitbucket.org",
    }[provider]
    state = {
        "writes": [],
        "errors": [],
        "revoked": False,
        "drop_response": False,
        "refreshes": 0,
        "notes": {},
        "drop_readback": False,
    }
    expiry = (datetime.now(UTC) + timedelta(hours=1)).isoformat()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.handle_provider()

        def do_POST(self):
            self.handle_provider()

        def handle_provider(self):
            try:
                path = urlsplit(self.path).path
                content = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                assert OPERATOR not in self.headers.get("Authorization", "")
                if provider == "github" and path == "/api/v3/app/installations/34/access_tokens":
                    claims = jwt.decode(
                        self.headers["Authorization"].split()[1], key.public_key(), algorithms=["RS256"]
                    )
                    assert claims["iss"] == "12"
                    permissions = json.loads(content)["permissions"]
                    assert permissions in [{"metadata": "read"}, {"pull_requests": "write"}]
                    result = {"token": "provider-fixture-token", "expires_at": expiry}
                elif provider == "gitlab" and path == "/oauth/token":
                    form = parse_qs(content.decode())
                    assert self.headers["Host"] == host and form["client_id"] == ["fixture-client"]
                    if form["grant_type"] == ["authorization_code"]:
                        assert len(form["code_verifier"][0]) >= 43
                        result = {"access_token": "initial-token", "refresh_token": "fixture-refresh", "expires_in": 1}
                    else:
                        assert form["refresh_token"] == ["fixture-refresh"]
                        state["refreshes"] += 1
                        result = {
                            "access_token": "provider-fixture-token",
                            "refresh_token": "rotated-refresh",
                            "expires_in": 3600,
                        }
                elif provider == "bitbucket" and path == "/site/oauth2/access_token":
                    assert self.headers["Host"] == "bitbucket.org"
                    assert (
                        self.headers["Authorization"]
                        == "Basic " + base64.b64encode(b"fixture-client:fixture-secret").decode()
                    )
                    assert parse_qs(content.decode())["grant_type"] == ["client_credentials"]
                    result = {
                        "access_token": "provider-fixture-token",
                        "expires_in": 3600,
                        "scope": "repository repository:write pullrequest pullrequest:write",
                    }
                else:
                    assert self.headers["Authorization"] == "Bearer provider-fixture-token"
                    if self.command == "GET":
                        if provider == "github":
                            assert path == "/api/v3/installation/repositories" and self.headers["Host"] == host
                            entries = [
                                {
                                    "id": 42,
                                    "full_name": "team/product",
                                    "clone_url": f"https://{host}/team/product.git",
                                    "default_branch": "main",
                                }
                            ]
                            result = {"repositories": [] if state["revoked"] else entries}
                        elif provider == "gitlab":
                            assert self.headers["Host"] == host
                            if path.startswith("/api/v4/projects/42/merge_requests/12/notes/"):
                                if state["drop_readback"]:
                                    self.close_connection = True
                                    return
                                result = state["notes"][int(path.rsplit("/", 1)[1])]
                            else:
                                assert path == "/api/v4/projects"
                                result = (
                                    []
                                    if state["revoked"]
                                    else [
                                        {
                                            "id": 42,
                                            "path_with_namespace": "team/product",
                                            "http_url_to_repo": f"https://{host}/team/product.git",
                                            "default_branch": "main",
                                        }
                                    ]
                                )
                        else:
                            assert path == "/2.0/repositories/team" and self.headers["Host"] == "api.bitbucket.org"
                            result = {
                                "values": []
                                if state["revoked"]
                                else [
                                    {
                                        "uuid": "{repository-42}",
                                        "full_name": "team/product",
                                        "mainbranch": {"name": "main"},
                                        "links": {
                                            "clone": [
                                                {"name": "https", "href": "https://bitbucket.org/team/product.git"}
                                            ]
                                        },
                                    }
                                ]
                            }
                    else:
                        expected = {
                            "github": "/api/v3/repos/team/product/issues/12/comments",
                            "gitlab": "/api/v4/projects/42/merge_requests/12/notes",
                            "bitbucket": "/2.0/repositories/team/product/pullrequests/12/comments",
                        }[provider]
                        assert path == expected
                        payload = json.loads(content)
                        message = payload["content"]["raw"] if provider == "bitbucket" else payload["body"]
                        assert "The project action was recorded." in message
                        assert "Request: `project`" in message and "Receipt: `" in message
                        state["writes"].append(payload)
                        if state["drop_response"]:
                            self.close_connection = True
                            return
                        result = {
                            "id": len(state["writes"]),
                            "html_url": f"https://{host}/team/product/comments/{len(state['writes'])}",
                        }
                        if provider == "gitlab":
                            result = {
                                "id": len(state["writes"]),
                                "body": message,
                                "project_id": 42,
                                "noteable_id": 999,
                                "noteable_iid": 12,
                                "noteable_type": "MergeRequest",
                                "author": {"id": 123, "username": "service-bot"},
                            }
                            state["notes"][result["id"]] = result
                encoded = json.dumps(result).encode()
                self.send_response(201 if self.command == "POST" else 200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)
            except Exception as error:
                state["errors"].append(str(error))
                self.send_response(500)
                self.end_headers()

    provider_server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    provider_thread = threading.Thread(target=provider_server.serve_forever, daemon=True)
    provider_thread.start()
    app = create_application(tmp_path / "factory", OPERATOR, role="api")
    try:
        with forge_server(app) as origin, httpx.Client(base_url=origin, trust_env=False, timeout=20) as client:
            connection = app.state.runtime.context.get_bean(SCMConnections)
            connection._client = httpx.AsyncClient(
                transport=LoopbackProviderTransport(f"http://127.0.0.1:{provider_server.server_port}")
            )
            auth = {"Authorization": "Bearer " + OPERATOR}
            path = f"/api/v1/scm/connections/{provider}"
            assert client.get(path + "/repositories").status_code == 401
            values = (
                {"app_id": "12", "installation_id": "34", "private_key": pem, "host": "https://" + host}
                if provider == "github"
                else {"client_id": "fixture-client", "client_secret": "fixture-secret", "workspace": "team"}
                if provider == "bitbucket"
                else {
                    "client_id": "fixture-client",
                    "host": "https://" + host,
                    "redirect_uri": origin + path + "/callback",
                }
            )
            response = client.put(path, headers=auth, json={"values": values})
            assert response.status_code == 200, response.text
            if provider == "gitlab":
                authorization = client.post(path + "/authorize", headers=auth)
                assert authorization.status_code == 200
                query = parse_qs(urlsplit(authorization.json()["authorization_url"]).query)
                assert query["code_challenge_method"] == ["S256"]
                callback = client.get(path + "/callback", params={"state": query["state"][0], "code": "fixture-code"})
                assert callback.status_code == 303
                assert (
                    client.get(
                        path + "/callback", params={"state": query["state"][0], "code": "fixture-code"}
                    ).status_code
                    == 409
                )
            remote = "{repository-42}" if provider == "bitbucket" else "42"
            response = client.post(path + "/repositories/connect", headers=auth, json={"repository_id": remote})
            assert response.status_code == 201, response.text
            repo = response.json()
            collaboration = f"/api/v1/repositories/{repo['id']}/collaboration"
            actor = "{actor-7}" if provider == "bitbucket" else "7"
            response = client.put(
                collaboration,
                headers=auth,
                json={
                    "enabled": True,
                    "trusted_actors": [actor],
                    "webhook_secret": SECRET,
                    "channel_commands": ["project"],
                },
            )
            assert response.status_code == 200, response.text
            ingress = f"/api/v1/webhooks/{provider}/{repo['id']}"
            raw, signed = comment(provider, 91)
            invalid = {**signed, "X-Gitlab-Token": "wrong-secret"} if provider == "gitlab" else signed
            denied = client.post(ingress, content=raw + b" ", headers=invalid).json()
            assert denied["reason"] == "invalid_signature"
            delivered = client.post(ingress, content=raw, headers=signed)
            assert delivered.status_code == 202, delivered.text
            receipt = delivered.json()
            process = f"/api/v1/collaboration/receipts/{receipt['id']}/process"
            assert client.post(process).status_code == 401
            result = client.post(process, headers=auth)
            assert result.status_code == 200, result.text
            assert result.json()["feedback_status"] == "sent"
            if provider == "gitlab":
                assert result.json()["feedback_remote"] == {
                    "id": 1,
                    "number": 1,
                    "url": f"https://{host}/team/product/-/merge_requests/12#note_1",
                }
            assert client.post(ingress, content=raw, headers=signed).json()["id"] == receipt["id"]
            assert client.post(process, headers=auth).json()["feedback_status"] == "sent"
            assert len(state["writes"]) == 1
            accepted = 1
            if provider == "gitlab":
                state["drop_readback"] = True
                raw, signed = comment(provider, 94)
                receipt = client.post(ingress, content=raw, headers=signed).json()
                note_process = f"/api/v1/collaboration/receipts/{receipt['id']}/process"
                uncertain = client.post(note_process, headers=auth).json()
                assert uncertain["feedback_status"] == "needs_reconciliation"
                assert uncertain["feedback_remote"]["id"] == 2
                assert uncertain["feedback_remote"]["url"].endswith("/merge_requests/12#note_2")
                assert client.post(note_process, headers=auth).json() == uncertain
                assert len(state["writes"]) == 2
                state["drop_readback"] = False
                accepted = 2
            state["drop_response"] = True
            raw, signed = comment(provider, 92)
            receipt = client.post(ingress, content=raw, headers=signed).json()
            process = f"/api/v1/collaboration/receipts/{receipt['id']}/process"
            assert client.post(process, headers=auth).json()["feedback_status"] == "needs_reconciliation"
            assert client.post(process, headers=auth).json()["feedback_status"] == "needs_reconciliation"
            reconcile = f"/api/v1/collaboration/receipts/{receipt['id']}/reconcile"
            assert (
                client.post(
                    reconcile,
                    headers=auth,
                    json={"outcome": "posted", "note": "Fixture server recorded the comment before disconnect."},
                ).status_code
                == 200
            )
            assert client.post(process, headers=auth).json()["feedback_status"] == "sent"
            assert len(state["writes"]) == accepted + 1
            state["revoked"] = True
            raw, signed = comment(provider, 93)
            receipt = client.post(ingress, content=raw, headers=signed).json()
            result = client.post(f"/api/v1/collaboration/receipts/{receipt['id']}/process", headers=auth).json()
            assert result["feedback_status"] == "needs_reconciliation"
            assert len(state["writes"]) == accepted + 1
            assert client.get("/api/v1/runs", headers=auth).json() == []
            assert not state["errors"], state["errors"]
            if provider == "gitlab":
                assert state["refreshes"] == 1
    finally:
        provider_server.shutdown()
        provider_server.server_close()
        provider_thread.join(timeout=5)
