# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Collector changes create new custody while legacy uncertain work stays immutable."""

import copy

import pytest
from test_framework_knowledge_service_host import host_refresh_case
from test_framework_knowledge_upstream_host import official_fixture

from firefly_forge.framework_knowledge import FrameworkKnowledge
from firefly_forge.framework_knowledge_models import (
    FrameworkCheckUpdatesInput,
    FrameworkCorpusReferenceV1,
    FrameworkRefreshCancelInput,
    FrameworkRefreshInput,
)
from firefly_forge.framework_knowledge_upstream import OfficialFrameworkClient, source_kind
from firefly_forge.framework_knowledge_worker import FrameworkKnowledgeWorker


@pytest.mark.parametrize("revision", [0, 4, True, "2", None])
def test_unreviewed_collector_revisions_are_rejected(revision):
    with pytest.raises(ValueError, match="collector|revision"):
        source_kind("docs/reference.md", collector_revision=revision)
    with pytest.raises(ValueError, match="collector|revision"):
        OfficialFrameworkClient(collector_revision=revision)


@pytest.mark.parametrize("path", ["book/art/figures/12-saga.svg", "docs/diagrams/chain.SVG", "examples/diagram.svg"])
def test_reviewed_collector_selects_assets_without_reinterpreting_legacy(path):
    assert source_kind(path, collector_revision=1) == ("example" if path.startswith("examples/") else "configuration")
    assert source_kind(path, collector_revision=2) is None


@pytest.mark.parametrize("selected", [2, 3])
async def test_new_collector_has_distinct_corpus_and_preserves_cancelled_legacy_unknowns(
    tmp_path, monkeypatch, selected
):
    original_policy = FrameworkKnowledge.policy
    legacy = original_policy()
    legacy.pop("collector_revision", None)
    monkeypatch.setattr(FrameworkKnowledge, "policy", staticmethod(lambda: copy.deepcopy(legacy)))
    transport = official_fixture(extra_files={"docs": {"diagram.svg": b'<svg xmlns="http://www.w3.org/2000/svg" />'}})[
        0
    ]
    manager, remote = await host_refresh_case(tmp_path, transport=transport)
    monkeypatch.setattr(
        FrameworkKnowledge, "policy", staticmethod(lambda: {**original_policy(), "collector_revision": selected})
    )
    old = await manager._get("operation", "f" * 32)
    old_ref = old["public"]["corpus"]["corpus_ref"]
    old["public"]["corpus"].pop("collector_revision", None)
    manager.records.rows["operation", "f" * 32].data = old
    manager.records.rows["corpus", old_ref].data["public"].pop("collector_revision", None)
    assert FrameworkCorpusReferenceV1.model_validate(old["public"]["corpus"]).collector_revision == 1
    remote.unknown_before_create = True
    await FrameworkKnowledgeWorker(manager).advance("f" * 32)
    held = await manager.get_refresh("pyfly", "f" * 32)
    assert held["uncertain_sources"] == 1
    svg = next(
        r
        for r in manager.records.rows.values()
        if r.kind == "source" and r.parent == old_ref and r.data["public"]["path"] == "docs/diagram.svg"
    )
    assert svg.data["public"]["kind"] == "configuration" and svg.data["public"]["parts"] == 1
    before = copy.deepcopy(
        {k: r.data for k, r in manager.records.rows.items() if r.kind in {"part", "source"} and r.parent == old_ref}
    )
    spent = copy.deepcopy(held["spent_budget"])
    cancelled = await manager.cancel(
        "pyfly", "f" * 32, FrameworkRefreshCancelInput(version=1, expected_revision=held["revision"])
    )
    assert (
        cancelled["state"] == "cancelled" and cancelled["uncertain_sources"] == 1 and cancelled["spent_budget"] == spent
    )
    cancelled_rows = copy.deepcopy(manager.records.rows)
    observed = await manager.observe("pyfly", "f" * 32)
    assert observed == cancelled
    assert {k: r.data for k, r in manager.records.rows.items()} == {k: r.data for k, r in cancelled_rows.items()}
    assert len(remote.posts) == 1
    discovered = await manager.check_updates("pyfly", FrameworkCheckUpdatesInput())
    fresh = await manager.refresh(
        "pyfly",
        "e" * 32,
        FrameworkRefreshInput(
            version=1,
            discovery_digest=discovered["release"]["digest"],
            expected_status_revision=discovered["status"]["revision"],
            policy_digest=discovered["policy_digest"],
        ),
    )
    assert fresh["corpus"]["corpus_ref"] != old_ref
    assert fresh["corpus"]["collector_revision"] == selected
    assert fresh["corpus"]["commit"] == held["corpus"]["commit"]
    operation = await manager._get("operation", "e" * 32)
    assert operation["policy"]["collector_revision"] == selected
    remote.unknown_before_create = False
    await FrameworkKnowledgeWorker(manager).advance("e" * 32)
    final = await manager.get_refresh("pyfly", "e" * 32)
    assert final["state"] == "ready"
    corpus = await manager._get("corpus", final["corpus"]["corpus_ref"])
    assert corpus["manifest_provenance"]["collector_revision"] == selected
    assert corpus["manifest_provenance"]["policy_digest"] == operation["policy_digest"]
    assert corpus["policy"]["collector_revision"] == selected
    assert {
        k: r.data for k, r in manager.records.rows.items() if r.kind in {"part", "source"} and r.parent == old_ref
    } == before
    assert (await manager.get_refresh("pyfly", "f" * 32)) == cancelled
    assert len(remote.posts) == 5


async def test_worker_rejects_unsupported_retained_policy_before_lease_or_remote_io(tmp_path):
    manager, remote = await host_refresh_case(tmp_path)
    operation = await manager._get("operation", "f" * 32)
    operation["policy"]["collector_revision"] = 4
    manager.records.rows["operation", "f" * 32].data = operation
    before = copy.deepcopy(operation)
    with pytest.raises(ValueError, match="collector|revision"):
        await FrameworkKnowledgeWorker(manager).advance("f" * 32)
    assert await manager._get("operation", "f" * 32) == before
    assert not remote.requests


async def test_cancelled_observation_is_read_only_even_with_an_unknown_source(tmp_path):
    manager, remote = await host_refresh_case(tmp_path)
    remote.unknown_before_create = True
    await FrameworkKnowledgeWorker(manager).advance("f" * 32)
    held = await manager.get_refresh("pyfly", "f" * 32)
    cancelled = await manager.cancel(
        "pyfly", "f" * 32, FrameworkRefreshCancelInput(version=1, expected_revision=held["revision"])
    )
    rows = copy.deepcopy({key: row.data for key, row in manager.records.rows.items()})
    requests = len(remote.requests)
    assert await manager.observe("pyfly", "f" * 32) == cancelled
    assert {key: row.data for key, row in manager.records.rows.items()} == rows
    assert len(remote.requests) == requests


@pytest.mark.parametrize("selected", [2, 3])
async def test_managed_grant_protocol_stays_one_while_policy_identity_advances(tmp_path, monkeypatch, selected):
    import hashlib
    from types import SimpleNamespace

    from test_framework_knowledge_service_host import Records, Store

    from firefly_forge.configuration import ConfigurationStore
    from firefly_forge.framework_knowledge_bootstrap import managed_framework_scope, managed_grant_body
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
    grant = managed_grant_body(manager)
    assert grant["collector_version"] == 1
    original_policy = FrameworkKnowledge.policy
    legacy = original_policy()
    legacy.pop("collector_revision", None)
    monkeypatch.setattr(FrameworkKnowledge, "policy", staticmethod(lambda: copy.deepcopy(legacy)))
    await manager.managed_check("pyfly")
    first = next(r for r in records.rows.values() if r.kind == "operation")
    async with managed_framework_scope(manager):
        await manager.cancel(
            "pyfly",
            first.key,
            FrameworkRefreshCancelInput(version=1, expected_revision=first.data["public"]["revision"]),
        )
    records.rows["status", "pyfly"].data.pop("last_check_attempt", None)
    monkeypatch.setattr(
        FrameworkKnowledge, "policy", staticmethod(lambda: {**original_policy(), "collector_revision": selected})
    )
    assert managed_grant_body(manager) == grant
    await manager.managed_check("pyfly")
    operations = [r for r in records.rows.values() if r.kind == "operation"]
    assert len(operations) == 2
    assert len({r.key for r in operations}) == len({r.data["public"]["corpus"]["corpus_ref"] for r in operations}) == 2
    assert {r.data["public"]["corpus"]["collector_revision"] for r in operations} == {1, selected}
    assert managed_grant_body(manager) == grant


async def test_known_outcome_is_reconciled_before_cancellation_and_resumes_without_replay(tmp_path):
    from firefly_forge.framework_knowledge_models import FrameworkRefreshResumeInput

    manager, remote = await host_refresh_case(tmp_path)
    remote.unknown_after_create = True
    await FrameworkKnowledgeWorker(manager).advance("f" * 32)
    held = await manager.get_refresh("pyfly", "f" * 32)
    assert held["uncertain_sources"] == 1 and len(remote.posts) == 1
    remote.unknown_after_create = False
    await manager.observe("pyfly", "f" * 32)
    observed = await manager.get_refresh("pyfly", "f" * 32)
    assert observed["uncertain_sources"] == 0 and len(remote.posts) == 1
    cancelled = await manager.cancel(
        "pyfly", "f" * 32, FrameworkRefreshCancelInput(version=1, expected_revision=observed["revision"])
    )
    assert cancelled["spent_budget"] == observed["spent_budget"]
    assert "resume" in cancelled["allowed_actions"]
    resumed = await manager.resume(
        "pyfly", "f" * 32, FrameworkRefreshResumeInput(version=1, expected_revision=cancelled["revision"])
    )
    assert resumed["spent_budget"] == cancelled["spent_budget"]
    await FrameworkKnowledgeWorker(manager).advance("f" * 32)
    complete = await manager.get_refresh("pyfly", "f" * 32)
    assert complete["state"] == "ready" and len(remote.posts) == 5
    assert complete["spent_budget"]["requests"] > held["spent_budget"]["requests"]


@pytest.mark.parametrize("observe_only", [False, True])
async def test_cancelled_worker_is_terminal_without_claiming_or_spending(tmp_path, observe_only):
    manager, remote = await host_refresh_case(tmp_path)
    remote.unknown_before_create = True
    await FrameworkKnowledgeWorker(manager).advance("f" * 32)
    held = await manager.get_refresh("pyfly", "f" * 32)
    await manager.cancel("pyfly", "f" * 32, FrameworkRefreshCancelInput(version=1, expected_revision=held["revision"]))
    rows = copy.deepcopy({key: row.data for key, row in manager.records.rows.items()})
    requests = len(remote.requests)
    await FrameworkKnowledgeWorker(manager).advance("f" * 32, observe_only=observe_only)
    assert {key: row.data for key, row in manager.records.rows.items()} == rows
    assert len(remote.requests) == requests
