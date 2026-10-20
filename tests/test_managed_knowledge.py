# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Managed installation must persist usable, authenticated knowledge without resetting operators."""

import json
import stat
from types import SimpleNamespace

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import serialization

from firefly_forge.configuration import ConfigurationStore
from firefly_forge.knowledge import KnowledgeConfiguration, KnowledgeManager, snapshot_knowledge


def bootstrap(tmp_path):
    from firefly_forge.managed_knowledge import bootstrap_knowledge

    return bootstrap_knowledge(ConfigurationStore(tmp_path / "factory"), tmp_path / "service")


def test_new_install_requires_authenticated_retrieval_and_persists_same_secrets(tmp_path):
    first = bootstrap(tmp_path)
    private = ConfigurationStore(tmp_path / "factory")
    manager = KnowledgeManager(SimpleNamespace(configuration=private))
    settings = manager.settings()
    assert settings["enabled"] and settings["required"] and settings["auto_memory"]
    assert settings["endpoint"] == "https://knowledge:8500"
    assert settings["tenant_id"] == "forge"
    assert settings["framework_workspace"] == "frameworks"
    assert len(settings["api_key"]) >= 40
    assert first["state"] == "initialized"
    assert manager.public()["readiness"]["state"] != "connected"
    assert settings["api_key"] not in json.dumps(first)
    service_files = list((tmp_path / "service").iterdir())
    for path in service_files:
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    cert = x509.load_pem_x509_certificate((tmp_path / "service" / "server.crt").read_bytes())
    assert cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value.get_values_for_type(
        x509.DNSName
    ) == ["knowledge"]
    public_key = serialization.load_pem_private_key(
        (tmp_path / "service" / "server.key").read_bytes(), None
    ).public_key()
    assert cert.public_key().public_numbers() == public_key.public_numbers()
    before = {p.name: p.read_bytes() for p in service_files}
    assert bootstrap(tmp_path)["state"] == "existing"
    assert {p.name: p.read_bytes() for p in service_files} == before
    assert manager.settings()["api_key"] == settings["api_key"]


def test_bootstrap_preserves_operator_credentials_and_frozen_run_settings(tmp_path):
    private = ConfigurationStore(tmp_path / "factory")
    manager = KnowledgeManager(SimpleNamespace(configuration=private))
    manager.configure(
        KnowledgeConfiguration(
            values={
                "enabled": True,
                "endpoint": "https://canon.example",
                "tenant_id": "existing",
                "api_key": "owner-key",
                "required": False,
            }
        )
    )
    snapshot = snapshot_knowledge(private)
    before = (private.directory / "knowledge.json").read_bytes()
    assert bootstrap(tmp_path)["state"] == "external"
    assert (private.directory / "knowledge.json").read_bytes() == before
    assert manager.settings({"knowledge_snapshot": snapshot})["api_key"] == "owner-key"
    assert not (tmp_path / "service").exists()


def test_bootstrap_refuses_symlinked_service_directory(tmp_path):
    target = tmp_path / "other"
    target.mkdir()
    (tmp_path / "service").symlink_to(target)
    with pytest.raises(ValueError, match="real private directory"):
        bootstrap(tmp_path)
    assert not list(target.iterdir())


def test_bootstrap_refuses_changed_or_missing_private_service_bundle(tmp_path):
    bootstrap(tmp_path)
    (tmp_path / "service" / "postgres-password").unlink()
    with pytest.raises(ValueError, match="incomplete"):
        bootstrap(tmp_path)


def test_ca_trust_cannot_accept_non_ca_pem(tmp_path):
    manager = KnowledgeManager(SimpleNamespace(configuration=ConfigurationStore(tmp_path)))
    with pytest.raises(ValueError, match="CA certificate"):
        manager.configure(KnowledgeConfiguration(values={"ca_certificate": "not a certificate"}))


def test_tls_trust_keeps_chain_and_hostname_verification_enabled(tmp_path):
    import socket
    import ssl
    import threading

    from firefly_forge.knowledge import knowledge_tls

    bootstrap(tmp_path)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(tmp_path / "service" / "server.crt", tmp_path / "service" / "server.key")
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(2)

    def serve():
        for _ in range(2):
            try:
                connection, _ = listener.accept()
            except OSError:
                return
            try:
                with context.wrap_socket(connection, server_side=True):
                    pass
            except ssl.SSLError:
                connection.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    trust = knowledge_tls((tmp_path / "service" / "ca.crt").read_text())
    try:
        with (
            socket.create_connection(listener.getsockname(), timeout=2) as connection,
            trust.wrap_socket(connection, server_hostname="knowledge"),
        ):
            pass
        with (
            socket.create_connection(listener.getsockname(), timeout=2) as connection,
            pytest.raises(ssl.SSLCertVerificationError),
        ):
            trust.wrap_socket(connection, server_hostname="another-service")
    finally:
        listener.close()
        thread.join(timeout=2)


async def test_managed_initialize_cannot_mark_empty_or_uncertain_retrieval_ready(tmp_path):
    import httpx

    bootstrap(tmp_path)
    private = ConfigurationStore(tmp_path / "factory")

    # Failed source admission is durably recorded and never automatically repeated.
    def unavailable(request):
        if request.method == "GET" and "/workspaces/" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "id": "frameworks",
                    "tenant_id": "forge",
                    "name": "Frameworks",
                    "status": "active",
                    "created_at": "2026-10-13T00:00:00Z",
                    "updated_at": "2026-10-13T00:00:00Z",
                },
            )
        if request.method == "GET" and request.url.path.endswith("/sources"):
            return httpx.Response(200, json={"items": [], "total": 0, "limit": 50, "offset": 0})
        if request.method == "POST" and request.url.path.endswith("/sources"):
            raise httpx.ReadTimeout("Lost source receipt")
        raise AssertionError(str(request.url))

    manager = KnowledgeManager(SimpleNamespace(configuration=private), transport=httpx.MockTransport(unavailable))
    with pytest.raises(ValueError, match="initialization failed"):
        await manager.initialize_managed()
    assert manager.public()["readiness"]["state"] == "failed"
    with pytest.raises(ValueError, match="uncertain"):
        await manager.initialize_managed()


def test_bootstrap_cli_prepares_service_without_disclosing_credentials(tmp_path):
    from click.testing import CliRunner

    from firefly_forge.cli import main

    result = CliRunner().invoke(
        main,
        ["knowledge", "bootstrap", "--data-dir", str(tmp_path / "factory"), "--service-dir", str(tmp_path / "service")],
    )
    assert result.exit_code == 0, result.output
    settings = KnowledgeManager(SimpleNamespace(configuration=ConfigurationStore(tmp_path / "factory"))).settings()
    assert settings["api_key"] not in result.output
    assert "initialized" in result.output


async def test_managed_first_project_gets_its_own_workspace_before_retrieval(tmp_path):
    import httpx

    bootstrap(tmp_path)
    requests = []

    async def repository(identifier):
        return {"id": identifier}

    def server(request):
        requests.append((request.method, request.url.path, request.headers["X-Workspace-Id"]))
        if request.url.path.endswith("/search"):
            return httpx.Response(200, json={"elapsed_ms": 1, "hits": []})
        if request.method == "GET":
            return httpx.Response(404, json={"detail": "missing"})
        body = json.loads(request.content)
        return httpx.Response(
            201,
            json={
                "id": body["id"],
                "tenant_id": "forge",
                "name": body["name"],
                "status": "active",
                "created_at": "2026-10-13T00:00:00Z",
                "updated_at": "2026-10-13T00:00:00Z",
            },
        )

    manager = KnowledgeManager(
        SimpleNamespace(configuration=ConfigurationStore(tmp_path / "factory"), repository=repository),
        transport=httpx.MockTransport(server),
    )
    await manager.search("a" * 32, "inventory")
    assert requests[0] == ("GET", "/api/v1/workspaces/forge-" + "a" * 32, "forge-" + "a" * 32)
    assert requests[1] == ("POST", "/api/v1/workspaces", "forge-" + "a" * 32)
    assert requests[2:] == [("POST", "/api/v1/search", "forge-" + "a" * 32), ("POST", "/api/v1/search", "frameworks")]


async def test_service_api_client_does_not_replay_browser_csrf_cookies(tmp_path):
    import httpx
    from flycanon_sdk import WorkspaceCreate

    seen = []

    def handler(request):
        seen.append(request)
        if request.method == "GET":
            return httpx.Response(
                404, json={"detail": "missing"}, headers={"Set-Cookie": "XSRF-TOKEN=browser-cookie; Path=/"}
            )
        assert "cookie" not in request.headers
        return httpx.Response(
            201,
            json={
                "id": "frameworks",
                "tenant_id": "forge",
                "name": "Frameworks",
                "status": "active",
                "created_at": "2026-10-13T00:00:00Z",
                "updated_at": "2026-10-13T00:00:00Z",
            },
        )

    manager = KnowledgeManager(
        SimpleNamespace(configuration=ConfigurationStore(tmp_path)), transport=httpx.MockTransport(handler)
    )
    manager.configure(
        KnowledgeConfiguration(
            values={
                "enabled": True,
                "endpoint": "https://knowledge:8500",
                "tenant_id": "forge",
                "api_key": "fixture-key",
            }
        )
    )
    from flycanon_sdk import CanonAPIError

    async with manager.client(manager.settings(), "frameworks") as client:
        with pytest.raises(CanonAPIError):
            await client.get_workspace("frameworks")
        await client.create_workspace(WorkspaceCreate(id="frameworks", name="Frameworks"))
    assert seen[1].headers["X-API-Key"] == "fixture-key"


async def test_uninitialized_factory_blocks_required_knowledge_instead_of_empty_success(tmp_path):
    async def repository(identifier):
        return {"id": identifier}

    manager = KnowledgeManager(SimpleNamespace(configuration=ConfigurationStore(tmp_path), repository=repository))
    with pytest.raises(ValueError, match="Initialize required flycanon"):
        await manager.search("a" * 32, "build inventory")
    assert manager.public()["enabled"] and manager.public()["required"]
    assert manager.public()["readiness"]["state"] == "missing"


def test_explicit_legacy_disabled_configuration_and_snapshot_remain_disabled(tmp_path):
    private = ConfigurationStore(tmp_path)
    private._write(private.directory / "knowledge.json", {"revision": "old", "settings": {"enabled": False}})
    manager = KnowledgeManager(SimpleNamespace(configuration=private))
    frozen = snapshot_knowledge(private)
    assert manager.settings()["enabled"] is False
    assert manager.settings({"knowledge_snapshot": frozen})["required"] is False


async def test_concurrent_initializers_never_admit_the_same_document_twice(tmp_path):
    import asyncio
    import base64
    import hashlib

    import httpx

    from firefly_forge.knowledge import KnowledgeConnection
    from firefly_forge.managed_knowledge import ManagedKnowledgeProvisioner

    bootstrap(tmp_path)
    private = ConfigurationStore(tmp_path / "factory")
    entered = asyncio.Event()
    release = asyncio.Event()
    admitted = []

    async def handler(request):
        if request.url.path.endswith("/workspaces/frameworks"):
            return httpx.Response(
                200,
                json={
                    "id": "frameworks",
                    "tenant_id": "forge",
                    "name": "Frameworks",
                    "status": "active",
                    "created_at": "2026-10-13T00:00:00Z",
                    "updated_at": "2026-10-13T00:00:00Z",
                },
            )
        if request.method == "GET" and request.url.path.endswith("/sources"):
            return httpx.Response(200, json={"items": [], "total": 0, "limit": 50, "offset": 0})
        body = json.loads(request.content)
        if request.url.path.endswith("/sources"):
            admitted.append(body)
            if len(admitted) == 1:
                entered.set()
                await release.wait()
            return httpx.Response(
                201,
                json={
                    "id": str(len(admitted)),
                    "kind": "text",
                    "status": "ingested",
                    "filename": body["filename"],
                    "content_sha256": hashlib.sha256(base64.b64decode(body["content_base64"])).hexdigest(),
                    "content_bytes": len(base64.b64decode(body["content_base64"])),
                    "n_chunks": 1,
                    "created_at": "2026-10-13T00:00:00Z",
                    "updated_at": "2026-10-13T00:00:00Z",
                },
            )
        return httpx.Response(
            200,
            json={
                "elapsed_ms": 1,
                "hits": [
                    {
                        "source_id": body["source_ids"][0],
                        "chunk_id": "chunk",
                        "content": "Cited source body",
                        "score": 1,
                    }
                ],
            },
        )

    connection = KnowledgeConnection(private)
    transport = httpx.MockTransport(handler)
    first = ManagedKnowledgeProvisioner(connection, transport=transport)
    second = ManagedKnowledgeProvisioner(connection, transport=transport)
    task = asyncio.create_task(first.initialize())
    await asyncio.wait_for(entered.wait(), timeout=2)
    try:
        with pytest.raises(ValueError, match="uncertain"):
            await second.initialize()
        assert len(admitted) == 1
    finally:
        release.set()
    await task
    assert len(admitted) == 15
    assert len({body["filename"] for body in admitted}) == 15
    assert first.readiness["state"] == "connected"
    for body in admitted[1:]:
        assert body["metadata"]["extra"]["version"] == (
            "26.10.6" if body["filename"].startswith("pyfly-") else "26.10.1"
        )
        assert body["metadata"]["extra"]["license"] == "Apache-2.0"
        assert body["uri"].startswith("https://github.com/fireflyframework/")


def test_modified_framework_manual_is_rejected_before_admission(tmp_path, monkeypatch):
    import shutil
    from pathlib import Path

    import firefly_forge.managed_knowledge as module

    directory = tmp_path / "package" / "resources" / "knowledge"
    shutil.copytree(Path(module.__file__).parent / "resources" / "knowledge", directory)
    monkeypatch.setattr(module, "__file__", str(tmp_path / "package" / "managed_knowledge.py"))
    with (directory / "pyfly-docs-getting-started.md").open("a") as target:
        target.write("Modified source")
    with pytest.raises(ValueError, match="integrity"):
        module.framework_documents()


async def test_campaign_default_and_managed_knowledge_vector_matches_child_revision(store, tmp_path):
    from firefly_forge.engine import Engine
    from firefly_forge.journal import Journal
    from firefly_forge.managed_knowledge import bootstrap_knowledge
    from firefly_forge.models import RepositoryInput, RunInput
    from firefly_forge.project_campaigns import ProjectCampaigns

    repo = await store.add_repository(RepositoryInput(name="Campaign source", url=str(tmp_path)))
    engine = Engine(store, tmp_path / "jobs")
    campaigns = ProjectCampaigns(store, engine, Journal(store))
    for managed in (False, True):
        if managed:
            bootstrap_knowledge(store.configuration, tmp_path / "service")
        vector = campaigns._configuration_vector(repo["id"], {"codex"})
        run = await store.create_run(RunInput(repository_id=repo["id"], brief="Bounded managed campaign"))
        campaigns._validate_admission_snapshots(run, vector)
        manager = KnowledgeManager(store)
        frozen = manager.settings(run)
        assert frozen["enabled"] and frozen["required"] and frozen["auto_memory"]
        if managed:
            assert frozen["endpoint"] == "https://knowledge:8500" and frozen["api_key"]
        else:
            with pytest.raises(ValueError, match="Initialize required flycanon"):
                await manager.retrieve(run)


def test_old_campaign_admission_keeps_original_pre_ca_knowledge_digest(tmp_path):
    from firefly_forge.knowledge import KnowledgeSettings
    from firefly_forge.project_campaigns import ProjectCampaigns, digest

    private = ConfigurationStore(tmp_path)
    # A previously frozen disabled revision predates the new optional TLS-CA field.
    legacy = KnowledgeSettings(enabled=False).model_dump(exclude={"ca_certificate"})
    revision = "a" * 32
    private._write(private.revisions / f"knowledge-{revision}.json", legacy)
    provider = private.snapshot("codex")
    roles = {"digest": "role-digest", "revision": "role-revision"}
    configuration = {
        "policy": "policy",
        "role_revision": "role-revision",
        "brand": digest({}),
        "credentials": {"codex": digest(private.load_snapshot(provider["config_revision"]))},
        "knowledge_settings": digest(legacy),
    }
    run = {
        "policy_snapshot": {"digest": "policy"},
        "role_snapshot": roles,
        "branding_snapshot": {},
        "config_revision": provider["config_revision"],
        "agent": "codex",
        "knowledge_snapshot": {"revision": revision},
    }
    campaigns = ProjectCampaigns(SimpleNamespace(configuration=private), None, None)
    before = (private.revisions / f"knowledge-{revision}.json").read_bytes()
    campaigns._validate_admission_snapshots(run, configuration)
    assert (private.revisions / f"knowledge-{revision}.json").read_bytes() == before
    with pytest.raises(ValueError, match="tool policy"):
        campaigns._validate_admission_snapshots({**run, "tool_policy_snapshot": {"digest": "current"}}, configuration)
    with pytest.raises(ValueError, match="tool policy"):
        campaigns._validate_admission_snapshots(run, {**configuration, "tool_policy": "current"})
