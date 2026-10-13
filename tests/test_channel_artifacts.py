# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import httpx
import pytest

from firefly_forge.channel_artifacts import ReferencePublisher, reference_manifest
from firefly_forge.models import RepositoryInput
from firefly_forge.scm_connections import SCMConnections


def test_reference_paths_are_generated_only_and_bounded():
    good = {"docs/design/reference-journey-01-desktop-light.svg": "<svg/>"}
    manifest, files = reference_manifest("a" * 32, "b" * 64, "design", good)
    assert manifest["files"] and len(files) == 2
    for path in [
        "../../owner.token",
        "src/main.py",
        "docs/design/.env",
        "docs/design/reference-journey-01-desktop-light.svg/../../bad",
    ]:
        with pytest.raises(ValueError):
            reference_manifest("a" * 32, "b" * 64, "design", {path: "private"})


@pytest.mark.parametrize("kind", ["design", "proposal"])
@pytest.mark.parametrize("provider", ["github", "gitlab", "bitbucket"])
async def test_provider_reference_creation_uses_new_branch_and_records_intent_before_write(
    store, monkeypatch, provider, kind
):
    repo = await store.add_repository(
        RepositoryInput(
            name="team/product",
            url="https://" + ("bitbucket.org" if provider == "bitbucket" else provider + ".com") + "/team/product.git",
            provider=provider,
            connection_provider=provider,
            remote_repository_id="42",
        )
    )
    calls = []
    intents = []

    def handle(request):
        calls.append(request)
        assert request.headers["authorization"] == "Bearer scoped-reference-grant"
        if request.method == "POST":
            assert intents
            body = request.content.decode(errors="replace")
            assert "forge/references/" in body or request.url.path.endswith(("/git/trees", "/git/commits"))
            if provider == "github":
                return httpx.Response(201, json={"sha": "c" * 40})
            return httpx.Response(201, json={"id": "c" * 40, "hash": "c" * 40})
        if (
            "forge/references/" in request.url.path
            or "forge%2Freferences" in str(request.url)
            or "docs/forge-references" in str(request.url)
            or "docs/product/proposals" in str(request.url)
        ):
            return httpx.Response(404, json={})
        if provider == "github":
            if "/git/commits/" in request.url.path:
                return httpx.Response(200, json={"tree": {"sha": "d" * 40}})
            return httpx.Response(200, json={"object": {"sha": "a" * 40}})
        return httpx.Response(200, json={"commit": {"id": "a" * 40}, "target": {"hash": "a" * 40}})

    scm = SCMConnections(store, client=httpx.AsyncClient(transport=httpx.MockTransport(handle)))

    async def auth(repository):
        return "scoped-reference-grant", {
            "github": "https://api.github.com",
            "gitlab": "https://gitlab.com/api/v4",
            "bitbucket": "https://api.bitbucket.org/2.0",
        }[provider]

    monkeypatch.setattr(scm, "reference_authentication", auth)

    async def intent(value):
        intents.append(value)

    result = await ReferencePublisher(scm).publish(
        repo,
        "b" * 64,
        kind,
        {"docs/design/reference-journey-01-desktop-light.svg": "<svg/>"}
        if kind == "design"
        else {"docs/product/proposals/" + ("a" * 32) + ".json": '{"status":"unreviewed"}'},
        intent=intent,
    )
    assert result["branch"] == "forge/references/" + ("b" * 64)
    assert result["url"].startswith(repo["url"].removesuffix(".git"))
    assert intents[0]["manifest"]["files"]
    if kind == "proposal":
        assert "docs/product/proposals/" + ("a" * 32) + ".json" in intents[0]["manifest"]["files"]
        assert any("Add unreviewed proposal" in request.content.decode(errors="replace") for request in calls)
    assert all(request.method in {"GET", "POST"} for request in calls)
    assert not any(request.method in {"DELETE", "PATCH", "PUT"} for request in calls)


async def test_existing_reference_branch_is_read_verified_and_never_overwritten(store, monkeypatch):
    import base64

    repo = await store.add_repository(
        RepositoryInput(
            name="team/product",
            url="https://github.com/team/product.git",
            provider="github",
            connection_provider="github",
            remote_repository_id="42",
        )
    )
    sources = {"docs/design/reference-journey-01-desktop-light.svg": "<svg/>"}
    manifest, files = reference_manifest(repo["id"], "b" * 64, "design", sources)
    writes = []
    changed = []

    def handle(request):
        assert request.method == "GET"
        if "/contents/" in request.url.path:
            path = request.url.path.split("/contents/")[1]
            content = "changed" if changed else files[path]
            return httpx.Response(200, json={"content": base64.b64encode(content.encode()).decode()})
        return httpx.Response(200, json={"object": {"sha": "a" * 40}})

    scm = SCMConnections(store, client=httpx.AsyncClient(transport=httpx.MockTransport(handle)))

    async def auth(repo):
        return "reference-grant", "https://api.github.com"

    monkeypatch.setattr(scm, "reference_authentication", auth)

    async def intent(value):
        writes.append(value)

    publisher = ReferencePublisher(scm)
    result = await publisher.publish(repo, "b" * 64, "design", sources, intent=intent)
    assert result["existing"] and not writes
    changed.append(True)
    with pytest.raises(ValueError, match="differs"):
        await publisher.publish(repo, "b" * 64, "design", sources, intent=intent)
    assert not writes


async def test_reference_reader_enforces_streamed_response_bound(store):
    scm = SCMConnections(
        store,
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b"x" * 1001))
        ),
    )
    with pytest.raises(ValueError, match="size limit"):
        await scm._request("GET", "https://api.github.com/example", response_limit=1000)


async def test_github_reference_grant_is_repo_scoped_contents_only_and_separate_from_issues(store):
    from datetime import UTC, datetime, timedelta

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = (
        rsa.generate_private_key(public_exponent=65537, key_size=2048)
        .private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
        .decode()
    )
    permissions = []

    def handle(request):
        if request.method == "POST":
            import json

            payload = json.loads(request.content)
            permissions.append(payload)
            return httpx.Response(
                201,
                json={
                    "token": "grant-" + str(len(permissions)),
                    "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
                },
            )
        return httpx.Response(
            200,
            json={
                "repositories": [
                    {
                        "id": 42,
                        "full_name": "team/product",
                        "clone_url": "https://github.com/team/product.git",
                        "default_branch": "main",
                    }
                ]
            },
        )

    scm = SCMConnections(store, client=httpx.AsyncClient(transport=httpx.MockTransport(handle)))
    await scm.configure("github", {"values": {"app_id": "12", "installation_id": "34", "private_key": key}})
    repo = await scm.connect("github", "42")
    token, api = await scm.reference_authentication(repo)
    assert permissions[-1] == {"permissions": {"contents": "write"}, "repository_ids": [42]}
    issue_token, api = await scm.issue_authentication(repo)
    assert permissions[-1] == {"permissions": {"issues": "write"}, "repository_ids": [42]}
    assert issue_token != token
    with pytest.raises(ValueError, match="grant"):
        await scm.reference_authentication({**repo, "url": "https://attacker.invalid/team/product.git"})
    with pytest.raises(ValueError, match="grant"):
        await scm.reference_authentication({**repo, "name": "team/another-project"})
    with pytest.raises(ValueError, match="grant"):
        await scm.issue_authentication({**repo, "name": "team/another-project"})


def test_proposal_reference_paths_cannot_publish_product_source_or_override_names():
    for path in [
        "docs/product/proposals/current.json",
        "docs/product/proposals/" + "a" * 32 + ".svg",
        "src/product.py",
        "docs/product/proposals/../secrets.json",
    ]:
        with pytest.raises(ValueError):
            reference_manifest("a" * 32, "b" * 64, "proposal", {path: "untrusted"})
