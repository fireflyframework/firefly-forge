# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Published content deduplication retains every native per-path provenance binding."""

import base64
import hashlib
import json
from types import SimpleNamespace

import httpx
import pytest
from framework_knowledge_fixtures import ControlledFlycanon
from test_framework_knowledge_service_host import host_refresh_case
from test_framework_knowledge_upstream_host import official_fixture


class CanonicalContents(ControlledFlycanon):
    def __init__(self, *, unknown_alias=False):
        super().__init__()
        self.unknown_alias = unknown_alias
        self.aliases = 0

    async def handle(self, request):
        if request.method == "POST" and request.url.path == "/api/v1/sources":
            body = json.loads(request.content)
            content_hash = hashlib.sha256(base64.b64decode(body["content_base64"])).hexdigest()
            match = next(
                (
                    v
                    for v in self.sources.values()
                    if v["content_sha256"] == content_hash and v["workspace"] == request.headers["X-Workspace-Id"]
                ),
                None,
            )
            if match is not None:
                self.posts.append(body)
                self.aliases += 1
                if self.unknown_alias:
                    self.unknown_alias = False
                    raise httpx.ReadTimeout("Known canonical content, lost alias response", request=request)
                return httpx.Response(201, json=match)
        return await super().handle(request)


@pytest.mark.parametrize("unknown_alias", [False, True])
async def test_native_aliases_complete_without_replaying_unknown_posts(tmp_path, unknown_alias):
    from firefly_forge.framework_knowledge_models import FrameworkSearchInput
    from firefly_forge.framework_knowledge_worker import FrameworkKnowledgeWorker

    transport = official_fixture(extra_files={"docs": {"guide.md": b"class CurrentService:\n    pass\n"}})[0]
    manager, _ = await host_refresh_case(tmp_path, transport=transport)
    remote = CanonicalContents(unknown_alias=unknown_alias)
    manager.knowledge.transport = remote.transport()
    await FrameworkKnowledgeWorker(manager).advance("f" * 32)
    if unknown_alias:
        held = await manager.get_refresh("pyfly", "f" * 32)
        assert held["state"] == "needs_reconciliation" and held["uncertain_sources"] == 1
        posts = len(remote.posts)
        await FrameworkKnowledgeWorker(manager).advance("f" * 32, observe_only=True)
        assert len(remote.posts) == posts
        await FrameworkKnowledgeWorker(manager).advance("f" * 32)
    final = await manager.get_refresh("pyfly", "f" * 32)
    assert final["state"] == "ready", await manager._get("operation", "f" * 32)
    assert remote.aliases == 1 and len(remote.posts) == 5 and len(remote.sources) == 4
    ref = final["corpus"]["corpus_ref"]
    parts = [r.data for r in manager.records.rows.values() if r.kind == "part" and r.parent == ref]
    shared = [
        p for p in parts if p["content_sha256"] == hashlib.sha256(b"class CurrentService:\n    pass\n").hexdigest()
    ]
    assert len(shared) == 2 and len({p["source_id"] for p in shared}) == 1
    corpus = await manager._get("corpus", ref)

    async def guard():
        await manager._authorize()

    for part in shared:
        result = await manager._search_corpus(
            corpus,
            FrameworkSearchInput(query="service", corpus_ref=ref),
            guard,
            source_ids=[part["source_id"]],
            part_refs={part["part_ref"]},
        )
        assert result["citations"] and all(c["source_ref"] == part["source_ref"] for c in result["citations"])


@pytest.mark.parametrize(
    "mutation", ["hash", "scope", "filename", "pending", "remote_id", "retarget", "missing", "ready_without_outcome"]
)
def test_alias_binding_never_accepts_different_or_unsubmitted_evidence(mutation):
    from firefly_forge.framework_knowledge_sync import canonical_part_ref

    canonical = {
        "part_ref": "a" * 32,
        "corpus_ref": "c" * 32,
        "filename": "canonical.txt",
        "content_sha256": "d" * 64,
        "state": "ready",
        "source_id": "remote",
    }
    alias = {**canonical, "part_ref": "b" * 32, "filename": "alias.txt", "state": "indexing"}
    remote = SimpleNamespace(
        id="remote",
        filename="canonical.txt",
        content_sha256="d" * 64,
        metadata=SimpleNamespace(extra={"forge_framework_part": "a" * 32, "corpus_ref": "c" * 32}),
    )
    if mutation == "hash":
        canonical["content_sha256"] = "e" * 64
    if mutation == "scope":
        canonical["corpus_ref"] = "e" * 32
    if mutation == "filename":
        remote.filename = "other.txt"
    if mutation == "pending":
        canonical["state"] = "pending"
    if mutation == "remote_id":
        canonical["source_id"] = "other"
    if mutation == "retarget":
        alias["canonical_part_ref"] = "e" * 32
    if mutation == "ready_without_outcome":
        canonical["source_id"] = None
    if mutation == "missing":
        canonical = None
    with pytest.raises(ValueError):
        canonical_part_ref(remote, alias, canonical)


async def test_unknown_alias_requires_one_unique_observed_canonical_source():
    from firefly_forge.framework_knowledge_sync import reconcile_part

    canonical = {
        "part_ref": "a" * 32,
        "corpus_ref": "c" * 32,
        "filename": "canonical.txt",
        "content_sha256": "d" * 64,
        "state": "ready",
        "source_id": "remote",
    }
    alias = {
        **canonical,
        "part_ref": "b" * 32,
        "filename": "alias.txt",
        "state": "needs_reconciliation",
        "source_id": None,
    }
    remote = SimpleNamespace(
        id="remote",
        filename="canonical.txt",
        content_sha256="d" * 64,
        metadata=SimpleNamespace(extra={"forge_framework_part": "a" * 32, "corpus_ref": "c" * 32}),
    )

    async def lookup(reference):
        return canonical if reference == canonical["part_ref"] else None

    class Client:
        async def list_sources(self, **kwargs):
            return SimpleNamespace(items=[remote, remote], total=2)

        async def submit_source(self, *args, **kwargs):
            raise AssertionError("Observation must never POST")

    assert await reconcile_part(Client(), alias, lookup_part=lookup) is None
