# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Managed installation authority is distinct from native user or SCM identity."""

import hashlib
from types import SimpleNamespace

import pytest
from test_framework_knowledge_service_host import Records, Store


async def test_managed_scope_requires_exact_installation_and_cannot_escape_task(tmp_path):
    import asyncio

    from firefly_forge.configuration import ConfigurationStore
    from firefly_forge.framework_knowledge import FrameworkKnowledge
    from firefly_forge.framework_knowledge_bootstrap import managed_framework_scope
    from firefly_forge.project_access import AccessDenied

    records = Records()
    store = Store(records)
    store.configuration = ConfigurationStore(tmp_path)

    async def owned():
        pass

    store.assert_ownership = owned
    settings = {
        "enabled": True,
        "api_key": "private-fixture",
        "endpoint": "https://knowledge:8500",
        "tenant_id": "forge",
        "framework_workspace": "frameworks",
        "ca_certificate": "fixture-ca",
    }
    knowledge = SimpleNamespace(settings=lambda: dict(settings))
    manager = FrameworkKnowledge(store, knowledge, records=records)
    files = {name: "a" * 64 for name in ("server.crt", "server.key", "postgres-password", "service.json")}
    files["ca.crt"] = hashlib.sha256(settings["ca_certificate"].encode()).hexdigest()
    store.configuration._write(store.configuration.directory / "managed-knowledge.json", {"version": 1, "files": files})
    async with managed_framework_scope(manager) as actor:
        assert actor["authentication_method"] == "managed_framework_knowledge" and "user_id" not in actor
        assert await manager._authorize(admin=True, actor=actor) == actor
        with pytest.raises(AccessDenied):
            await asyncio.create_task(manager._authorize(admin=True, actor=actor))
        settings["endpoint"] = "https://changed.example"
        with pytest.raises(AccessDenied):
            async with managed_framework_scope(manager):
                pass
    with pytest.raises(AccessDenied):
        await manager._authorize(admin=True, actor=actor)


async def test_external_connection_never_acquires_implicit_managed_import_grant(tmp_path):
    from firefly_forge.configuration import ConfigurationStore
    from firefly_forge.framework_knowledge import FrameworkKnowledge
    from firefly_forge.framework_knowledge_bootstrap import managed_framework_scope

    records = Records()
    store = Store(records)
    store.configuration = ConfigurationStore(tmp_path)
    manager = FrameworkKnowledge(store, SimpleNamespace(), records=records)
    async with managed_framework_scope(manager) as actor:
        assert actor is None
    assert not records.rows


async def test_managed_release_check_queues_once_and_honors_cadence(tmp_path):
    from test_framework_knowledge_upstream_host import official_fixture

    from firefly_forge.configuration import ConfigurationStore
    from firefly_forge.framework_knowledge import FrameworkKnowledge
    from firefly_forge.product_provenance import content_digest

    records = Records()
    store = Store(records)
    store.configuration = ConfigurationStore(tmp_path)

    async def owned():
        pass

    store.assert_ownership = owned
    settings = {
        "enabled": True,
        "api_key": "private-fixture",
        "endpoint": "https://knowledge:8500",
        "tenant_id": "forge",
        "framework_workspace": "frameworks",
        "ca_certificate": "fixture-ca",
    }
    manager = FrameworkKnowledge(
        store, SimpleNamespace(settings=lambda: dict(settings)), records=records, transport=official_fixture()[0]
    )
    files = {name: "a" * 64 for name in ("server.crt", "server.key", "postgres-password", "service.json")}
    files["ca.crt"] = hashlib.sha256(settings["ca_certificate"].encode()).hexdigest()
    store.configuration._write(store.configuration.directory / "managed-knowledge.json", {"version": 1, "files": files})
    manager._connection_snapshot = lambda: ({"revision": "d" * 32}, content_digest(settings))
    await manager.managed_check("pyfly")
    operations = [row for row in records.rows.values() if row.kind == "operation"]
    assert len(operations) == 1 and operations[0].data["public"]["state"] == "queued"
    assert operations[0].data["actor"]["authentication_method"] == "managed_framework_knowledge"
    await manager.managed_check("pyfly")
    assert len([r for r in records.rows.values() if r.kind == "operation"]) == 1


async def test_current_admin_can_cancel_managed_operation_with_retained_installation_authority(tmp_path):
    from firefly_forge.configuration import ConfigurationStore
    from firefly_forge.framework_knowledge import FrameworkKnowledge
    from firefly_forge.framework_knowledge_bootstrap import authorize_managed_framework, managed_framework_scope
    from firefly_forge.framework_knowledge_models import FrameworkRefreshCancelInput

    records = Records()
    store = Store(records)
    store.configuration = ConfigurationStore(tmp_path)

    async def owned():
        pass

    store.assert_ownership = owned
    settings = {
        "enabled": True,
        "api_key": "fixture",
        "endpoint": "https://knowledge:8500",
        "tenant_id": "forge",
        "framework_workspace": "frameworks",
        "ca_certificate": "fixture-ca",
    }
    manager = FrameworkKnowledge(store, SimpleNamespace(settings=lambda: dict(settings)), records=records)
    marker = {name: "a" * 64 for name in ("server.crt", "server.key", "postgres-password", "service.json")}
    marker["ca.crt"] = hashlib.sha256(settings["ca_certificate"].encode()).hexdigest()
    store.configuration._write(
        store.configuration.directory / "managed-knowledge.json", {"version": 1, "files": marker}
    )
    async with managed_framework_scope(manager) as original:
        await manager._put(
            "operation",
            "f" * 32,
            "pyfly",
            {
                "actor": original,
                "public": {
                    "request_id": "f" * 32,
                    "framework": "pyfly",
                    "state": "queued",
                    "revision": "a" * 64,
                    "corpus": None,
                    "uncertain_sources": 0,
                    "remaining_budget": {"requests": 10},
                    "spent_budget": {"requests": 0},
                },
            },
            "pyfly",
        )

    async def authorize(*, admin=False, actor=None):
        if actor and actor.get("authentication_method") == "managed_framework_knowledge":
            return await authorize_managed_framework(manager, actor)
        return {"user_id": "b" * 32, "global_admin": True}

    manager._authorize = authorize
    result = await manager.cancel("pyfly", "f" * 32, FrameworkRefreshCancelInput(version=1, expected_revision="a" * 64))
    assert result["state"] == "cancelled"
    retained = await manager._get("operation", "f" * 32)
    assert retained["actor"] == original and "user_id" not in retained["actor"]
