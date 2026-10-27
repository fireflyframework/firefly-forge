# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Reviewed semantic admission preserves legacy byte custody and excluded windows."""

import base64
import hashlib

import pytest
from test_framework_knowledge_service_host import host_refresh_case
from test_framework_knowledge_upstream_host import official_fixture

from firefly_forge.framework_knowledge import FrameworkKnowledge
from firefly_forge.framework_knowledge_upstream import OfficialFrameworkClient, source_kind
from firefly_forge.framework_knowledge_worker import FrameworkKnowledgeWorker


async def case(tmp_path, monkeypatch, revision, files):
    policy = FrameworkKnowledge.policy()
    with monkeypatch.context() as scoped:
        scoped.setattr(FrameworkKnowledge, "policy", staticmethod(lambda: {**policy, "collector_revision": revision}))
        return await host_refresh_case(tmp_path, transport=official_fixture(extra_files=files)[0])


async def test_current_defaults_and_svg_inheritance_are_consistent():
    assert FrameworkKnowledge.policy()["collector_revision"] == 3
    assert source_kind("docs/diagram.svg", collector_revision=1) == "configuration"
    assert source_kind("docs/diagram.svg", collector_revision=2) is None
    assert source_kind("docs/diagram.svg", collector_revision=3) is None
    async with OfficialFrameworkClient(transport=official_fixture()[0]) as client:
        assert client.collector_revision == 3


@pytest.mark.parametrize("body", [b"", b"\n", " \t\r\n\u2003\u00a0".encode()])
async def test_semantically_blank_references_have_explicit_verified_exclusion(tmp_path, monkeypatch, body):
    manager, remote = await case(tmp_path, monkeypatch, 3, {"docs": {"blank.txt": body}})

    async def before_post(payload):
        assert base64.b64decode(payload["content_base64"]).decode().strip()

    remote.before_post = before_post
    await FrameworkKnowledgeWorker(manager).advance("f" * 32)
    final = await manager.get_refresh("pyfly", "f" * 32)
    assert final["state"] == "ready", await manager._get("operation", "f" * 32)
    source = next(
        r.data
        for r in manager.records.rows.values()
        if r.kind == "source" and r.data["public"]["path"] == "docs/blank.txt"
    )
    assert source["public"]["state"] == "excluded"
    assert source["public"]["sha256"] == hashlib.sha256(body).hexdigest()
    assert source["public"]["exclusion_reason"] == "semantic_whitespace_only_reference"
    assert source["public"]["parts"] == 0
    assert source["public"]["excluded_parts"] == int(bool(body))
    assert all(p["metadata"]["title"] != "docs/blank.txt" for p in remote.posts)
    parts = [
        r.data
        for r in manager.records.rows.values()
        if r.kind == "part" and r.data["source_ref"] == source["public"]["source_ref"]
    ]
    assert len(parts) == int(bool(body))
    if parts:
        assert parts[0]["state"] == "excluded" and parts[0]["ordinal"] == 1 and parts[0]["source_id"] is None
        assert parts[0]["exclusion_reason"] == "semantic_whitespace_only_reference"
    assert final["counts"]["excluded_parts"] == int(bool(body))


async def test_blank_window_retains_original_ordinal_and_manifest_provenance(tmp_path, monkeypatch):
    body = b"A" * 65536 + b" " * 65536 + b"B"
    manager, remote = await case(tmp_path, monkeypatch, 3, {"docs": {"padded.txt": body}})

    async def before_post(payload):
        assert base64.b64decode(payload["content_base64"]).decode().strip()

    remote.before_post = before_post
    await FrameworkKnowledgeWorker(manager).advance("f" * 32)
    final = await manager.get_refresh("pyfly", "f" * 32)
    assert final["state"] == "ready"
    source = next(
        r.data
        for r in manager.records.rows.values()
        if r.kind == "source" and r.data["public"]["path"] == "docs/padded.txt"
    )
    parts = sorted(
        [
            r.data
            for r in manager.records.rows.values()
            if r.kind == "part" and r.data["source_ref"] == source["public"]["source_ref"]
        ],
        key=lambda p: p["ordinal"],
    )
    assert [p["ordinal"] for p in parts] == [1, 2, 3]
    assert [p["state"] for p in parts] == ["ready", "excluded", "ready"]
    assert parts[1]["content_sha256"] == hashlib.sha256(b" " * 65536).hexdigest()
    assert source["public"]["parts"] == source["public"]["ready_parts"] == 2
    assert source["public"]["excluded_parts"] == 1 and source["public"]["exclusions_sha256"]
    assert source["public"]["sha256"] == hashlib.sha256(body).hexdigest()
    assert final["counts"]["excluded_parts"] == 1 and final["corpus"]["manifest_sha256"]


@pytest.mark.parametrize("revision", [1, 2])
async def test_legacy_collectors_preserve_blank_parts_without_new_exclusion(tmp_path, monkeypatch, revision):
    manager, remote = await case(tmp_path, monkeypatch, revision, {"docs": {"blank.txt": b"\n"}})
    await FrameworkKnowledgeWorker(manager).advance("f" * 32)
    source = next(
        r.data
        for r in manager.records.rows.values()
        if r.kind == "source" and r.data["public"]["path"] == "docs/blank.txt"
    )
    assert source["public"]["parts"] == 1 and source["public"]["state"] != "excluded"
    assert "excluded_parts" not in source["public"]
    assert any(p["metadata"]["title"] == "docs/blank.txt" for p in remote.posts)


@pytest.mark.parametrize("body", [b"\n", b"\xff"])
async def test_invalid_license_holds_instead_of_becoming_an_asset_exclusion(tmp_path, monkeypatch, body):
    manager, remote = await case(tmp_path, monkeypatch, 3, {"LICENSE": body})
    await FrameworkKnowledgeWorker(manager).advance("f" * 32)
    held = await manager.get_refresh("pyfly", "f" * 32)
    assert held["state"] == "needs_reconciliation" and not remote.posts
    source = next(
        r.data for r in manager.records.rows.values() if r.kind == "source" and r.data["public"]["path"] == "LICENSE"
    )
    assert source["public"]["state"] == "pending" and source["public"]["exclusion_reason"] is None


async def test_blank_classification_does_not_hide_a_final_cache_integrity_failure(tmp_path, monkeypatch):
    manager, remote = await case(tmp_path, monkeypatch, 3, {"docs": {"blank.txt": b"\n"}})
    worker = FrameworkKnowledgeWorker(manager)
    original = worker._chunks

    def corrupted(name):
        if name == hashlib.sha256(b"\n").hexdigest():
            yield b"\n"
            raise ValueError("Immutable cache integrity changed after blank bytes")
        yield from original(name)

    worker._chunks = corrupted
    await worker.advance("f" * 32)
    held = await manager.get_refresh("pyfly", "f" * 32)
    assert held["state"] == "needs_reconciliation" and not remote.posts
    source = next(
        r.data
        for r in manager.records.rows.values()
        if r.kind == "source" and r.data["public"]["path"] == "docs/blank.txt"
    )
    assert source["public"]["state"] == "pending" and source["public"]["exclusion_reason"] is None


async def test_excluded_window_tampering_holds_manifest_instead_of_becoming_ready(tmp_path, monkeypatch):
    body = b"A" * 65536 + b" " * 65536 + b"B"
    manager, remote = await case(tmp_path, monkeypatch, 3, {"docs": {"padded.txt": body}})
    changed = False

    async def tamper(payload):
        nonlocal changed
        if changed:
            return
        for row in manager.records.rows.values():
            if row.kind == "part" and row.data["state"] == "excluded":
                row.data["ordinal"] = 99
                changed = True

    remote.before_post = tamper
    await FrameworkKnowledgeWorker(manager).advance("f" * 32)
    held = await manager.get_refresh("pyfly", "f" * 32)
    assert changed and held["state"] == "needs_reconciliation"
    assert held["corpus"]["manifest_sha256"] is None


@pytest.mark.parametrize("failure", [None, "size", "sha256", "blob_oid", "late_integrity"])
def test_exclusion_verification_streams_all_windows_before_acceptance(monkeypatch, failure):
    import firefly_forge.framework_knowledge_worker as module

    chunks = [b"A" * 65536, b" " * 65536, b"B" * 65536]
    sha = hashlib.sha256()
    git = hashlib.sha1(b"blob 196608\0")
    for chunk in chunks:
        sha.update(chunk)
        git.update(chunk)
    public = {"source_ref": "a" * 32, "bytes": 196608, "sha256": sha.hexdigest(), "blob_oid": git.hexdigest()}
    if failure == "size":
        public["bytes"] += 1
    elif failure in {"sha256", "blob_oid"}:
        public[failure] = "0" * len(public[failure])
    seen = []
    completed = False

    def source_chunks(_):
        nonlocal completed
        for number, chunk in enumerate(chunks):
            seen.append(number)
            yield chunk
        completed = True
        if failure == "late_integrity":
            raise ValueError("Final immutable cache identity changed")

    original = module.utf8_parts

    def streaming_parts(parts, *, window):
        assert not seen, "Verifier materialized the source before splitting windows"
        for number, part in enumerate(original(parts, window=window)):
            assert len(seen) == number + 1, "Verifier consumed ahead of the current window"
            yield part

    monkeypatch.setattr(module, "utf8_parts", streaming_parts)
    worker = FrameworkKnowledgeWorker(None)
    monkeypatch.setattr(worker, "_chunks", source_chunks)
    if failure:
        with pytest.raises(ValueError):
            worker._verified_exclusions({"public": public, "cache": "retained"}, 65536)
    else:
        result = worker._verified_exclusions({"public": public, "cache": "retained"}, 65536)
        assert len(result) == 1 and result[0]["ordinal"] == 2
        assert result[0]["content_sha256"] == hashlib.sha256(chunks[1]).hexdigest()
    assert completed and seen == [0, 1, 2]
