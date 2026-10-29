# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
import asyncio
import base64
import json

import httpx
import pytest

from firefly_forge.knowledge import KnowledgeConfiguration, KnowledgeManager
from firefly_forge.knowledge_uploads import KnowledgeUploadInput, KnowledgeUploads
from firefly_forge.models import RepositoryInput


def upload(content=b"Product reference for warehouse inventory."):
    return KnowledgeUploadInput(
        filename="product.md", title="Product reference", content_base64=base64.b64encode(content).decode()
    )


async def setup(store, tmp_path, failed=False):
    repo = await store.add_repository(RepositoryInput(name="Inventory", url=str(tmp_path)))
    calls = []
    admissions = None
    timestamp = "2026-10-12T00:00:00Z"

    async def handler(request):
        assert request.headers["X-Workspace-Id"] == "forge-" + repo["id"]
        if "/workspaces/" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "id": "forge-" + repo["id"],
                    "tenant_id": "firefly",
                    "name": "Inventory",
                    "status": "active",
                    "created_at": timestamp,
                    "updated_at": timestamp,
                },
            )
        if request.method == "POST":
            calls.append(request)
            assert (await admissions.uploads(repo["id"]))[0]["status"] == "submitting"
            if failed:
                raise httpx.ReadTimeout("private-key-must-not-leak", request=request)
            await asyncio.sleep(0.02)
        return httpx.Response(
            200,
            json={
                "id": "source1",
                "kind": "text",
                "status": "ingested",
                "filename": "product.md",
                "content_sha256": __import__("hashlib").sha256(base64.b64decode(upload().content_base64)).hexdigest(),
                "content_bytes": len(upload().content()),
                "n_chunks": 1,
                "created_at": timestamp,
                "updated_at": timestamp,
            },
        )

    knowledge = KnowledgeManager(store, transport=httpx.MockTransport(handler))
    knowledge.configure(
        KnowledgeConfiguration(
            values={
                "enabled": True,
                "endpoint": "http://127.0.0.1:18500",
                "tenant_id": "firefly",
                "api_key": "private-key-must-not-leak",
            }
        )
    )
    admissions = KnowledgeUploads(store, knowledge)
    return repo, admissions, calls


async def test_admission_intent_precedes_scoped_effect_and_concurrent_duplicates_write_once(store, tmp_path):
    repo, admissions, calls = await setup(store, tmp_path)
    results = await asyncio.gather(admissions.upload(repo["id"], upload()), admissions.upload(repo["id"], upload()))
    assert len(calls) == 1 and len({result["id"] for result in results}) == 1
    result = await admissions.admission(repo["id"], results[0]["id"])
    assert result["status"] == "submitted" and result["source_status"] == "ingested"
    assert [entry["status"] for entry in result["history"]] == ["submitting", "submitted"]
    assert "content_base64" not in json.dumps(result) and "private-key-must-not-leak" not in json.dumps(result)
    with pytest.raises(KeyError):
        await admissions.admission("b" * 32, result["id"])


async def test_unknown_remote_outcome_is_retained_without_repeating_the_write(store, tmp_path):
    repo, admissions, calls = await setup(store, tmp_path, failed=True)
    result = await admissions.upload(repo["id"], upload())
    assert result["status"] == "needs_reconciliation" and "private-key" not in json.dumps(result)
    second = await KnowledgeUploads(store, admissions.knowledge).upload(repo["id"], upload())
    assert second["id"] == result["id"] and len(calls) == 1
    from firefly_forge.knowledge_uploads import KnowledgeUploadResolution

    reconciled = await admissions.reconcile(repo["id"], result["id"], KnowledgeUploadResolution(source_id="source1"))
    assert reconciled["status"] == "submitted" and len(calls) == 1


async def test_interrupted_admission_recovers_as_unknown_without_an_automatic_replay(store, tmp_path):
    repo, admissions, calls = await setup(store, tmp_path)
    original = admissions.knowledge._assert_ownership

    async def interrupted():
        raise asyncio.CancelledError()

    admissions.knowledge._assert_ownership = interrupted
    with pytest.raises(asyncio.CancelledError):
        await admissions.upload(repo["id"], upload())
    admissions.knowledge._assert_ownership = original
    recovered = KnowledgeUploads(store, admissions.knowledge)
    records = await recovered.uploads(repo["id"])
    assert records[0]["status"] == "needs_reconciliation"
    assert (await recovered.upload(repo["id"], upload()))["id"] == records[0]["id"]
    assert not calls


@pytest.mark.parametrize(
    "filename,content",
    [
        ("../escape.md", b"x"),
        ("secret.env", b"x"),
        ("image.png", b"x"),
        ("empty.txt", b""),
        ("large.txt", b"x" * 1000001),
        ("binary.txt", b"\x00x"),
        ("encoding.md", b"\xff"),
    ],
    ids=["traversal", "private-environment", "binary-extension", "empty", "oversized", "nul", "invalid-utf8"],
)
def test_only_bounded_utf8_documents_can_be_admitted(filename, content):
    with pytest.raises(ValueError):
        KnowledgeUploadInput(
            filename=filename, title="Reference", content_base64=base64.b64encode(content).decode()
        ).content()


async def test_registered_secret_document_is_rejected_before_source_effect(store, tmp_path):
    repo, admissions, calls = await setup(store, tmp_path)
    with pytest.raises(ValueError, match="credentials"):
        await admissions.upload(repo["id"], upload(b"private-key-must-not-leak"))
    assert not calls and await admissions.uploads(repo["id"]) == []
