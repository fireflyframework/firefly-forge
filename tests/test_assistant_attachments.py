# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
import asyncio
import base64
import hashlib
import importlib
import io
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError


def module():
    return importlib.import_module("firefly_forge.assistant_attachments")


def upload(content=b"Reserve inventory", filename="requirements.txt", request_id=None):
    return module().AttachmentInput(
        request_id=request_id or uuid4().hex,
        filename=filename,
        content_base64=base64.b64encode(content).decode(),
    )


async def setup(store, decoder=None):
    from firefly_forge.assistant_persistence import ForgeAssistantSessionEntity, ForgeAssistantSessionRepository
    from firefly_forge.models import RepositoryInput
    from firefly_forge.store import identifier

    repo = await store.add_repository(RepositoryInput(name="Product", url="https://github.com/team/product.git"))
    session_id = uuid4()
    async with store._unit():
        await ForgeAssistantSessionRepository().save(
            ForgeAssistantSessionEntity(
                id=session_id,
                repository_id=identifier(repo["id"]),
                data={"id": session_id.hex, "repository_id": repo["id"]},
            )
        )
    service = module().AssistantAttachments(store, decoder=decoder)
    return service, repo["id"], session_id.hex


async def plain_decoder(_filename, raw):
    return {"kind": "document", "mime_type": "text/plain", "text": raw.decode(), "image": None, "truncated": False}


async def test_real_document_decoder_stores_private_content_and_public_metadata_only(store):
    service, repo, session = await setup(store)
    request = upload(b"Customer can reserve inventory", filename="diseño.txt")
    receipt = await service.admit(repo, session, request)
    assert receipt["filename"] == "diseño.txt" and receipt["status"] == "ready"
    assert receipt["sha256"] == hashlib.sha256(b"Customer can reserve inventory").hexdigest()
    assert receipt["text_chars"] == len("Customer can reserve inventory")
    assert await service.get(repo, session, receipt["id"]) == receipt
    assert await service.list(repo, session) == [receipt]
    assert not {"text", "image", "raw_bytes", "content_base64"}.intersection(receipt)
    selected = (await service.freeze(repo, session, [receipt["id"]]))[0]
    assert selected["text"] == "Customer can reserve inventory"
    assert selected["raw_bytes"] == b"Customer can reserve inventory"
    assert selected["image"] is None


async def test_exact_request_reuses_receipt_without_decoding_and_conflicting_replay_fails(store):
    calls = 0

    async def decode(filename, raw):
        nonlocal calls
        calls += 1
        return await plain_decoder(filename, raw)

    service, repo, session = await setup(store, decode)
    request = upload()
    receipt = await service.admit(repo, session, request)
    assert await service.admit(repo, session, request) == receipt and calls == 1
    for changed in (
        request.model_copy(update={"filename": "changed.txt"}),
        upload(b"Changed", request_id=request.request_id),
    ):
        with pytest.raises(ValueError, match="replay"):
            await service.admit(repo, session, changed)
    assert len(await service.list(repo, session)) == 1 and calls == 1


async def test_other_repository_or_conversation_cannot_read_or_freeze_attachment(store):
    service, repo, session = await setup(store, plain_decoder)
    _, other_repo, other_session = await setup(store, plain_decoder)
    receipt = await service.admit(repo, session, upload())
    for repository_id, session_id in ((other_repo, session), (repo, other_session)):
        with pytest.raises(KeyError):
            await service.get(repository_id, session_id, receipt["id"])
        with pytest.raises(KeyError):
            await service.freeze(repository_id, session_id, [receipt["id"]])
    with pytest.raises(KeyError):
        await service.freeze(other_repo, other_session, [receipt["id"]])


async def test_shared_repository_cannot_cross_project_attachment_permissions(store):
    from firefly_forge.assistant_persistence import ForgeAssistantSessionRepository
    from firefly_forge.identity import Principal
    from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied, ProjectAccess
    from firefly_forge.project_models import ProjectDefinition
    from firefly_forge.projects import Projects
    from firefly_forge.store import identifier

    service, repo, session = await setup(store, plain_decoder)
    projects = []
    for name in ("Owned project", "Other project"):
        projects.append(
            await Projects(store).configure(
                ProjectDefinition(
                    name=name,
                    description="Scoped document context",
                    topology="monorepo",
                    owners=["Platform"],
                    components=[dict(id="api", name="API", repository_id=repo, responsibility="API")],
                )
            )
        )
    user = Principal(uuid4(), "https://id.example/realms/forge", uuid4().hex)
    access = ProjectAccess(store)
    await access.set_membership(projects[0]["id"], user, "developer")
    async with store._unit():
        row = await ForgeAssistantSessionRepository().find_by_id(identifier(session))
        row.data = {**row.data, "project_id": projects[1]["id"]}
        await ForgeAssistantSessionRepository().save(row)
    context = CURRENT_PRINCIPAL.set(user)
    try:
        with pytest.raises(AccessDenied):
            await service.admit(repo, session, upload())
    finally:
        CURRENT_PRINCIPAL.reset(context)
    assert await service.list(repo, session) == []


async def test_suspension_during_decoding_prevents_even_admin_attachment_admission(store):
    from firefly_forge.identity import Principal
    from firefly_forge.identity_persistence import ForgeIdentityUserEntity, ForgeIdentityUserRepository
    from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied

    user = Principal(uuid4(), "https://id.example/realms/forge", uuid4().hex, api_roles=frozenset({"forge-admin"}))
    async with store._unit():
        await ForgeIdentityUserRepository().save(
            ForgeIdentityUserEntity(id=user.id, issuer=user.issuer, subject=user.subject)
        )

    async def suspend(filename, raw):
        result = await plain_decoder(filename, raw)
        async with store._unit():
            row = await ForgeIdentityUserRepository().find_by_id(user.id)
            row.enabled = False
            await ForgeIdentityUserRepository().save(row)
        return result

    service, repo, session = await setup(store, suspend)
    context = CURRENT_PRINCIPAL.set(user)
    try:
        with pytest.raises(AccessDenied):
            await service.admit(repo, session, upload())
    finally:
        CURRENT_PRINCIPAL.reset(context)
    assert await service.list(repo, session) == []


async def test_concurrent_session_capacity_never_admits_a_twenty_first_file(store):
    service, repo, session = await setup(store, plain_decoder)
    for _ in range(19):
        await service.admit(repo, session, upload())
    outcomes = await asyncio.gather(
        service.admit(repo, session, upload(b"First final attachment")),
        service.admit(repo, session, upload(b"Second final attachment")),
        return_exceptions=True,
    )
    assert sum(isinstance(value, dict) for value in outcomes) == 1
    assert sum(isinstance(value, ValueError) for value in outcomes) == 1
    assert len(await service.list(repo, session)) == 20


async def test_invalid_base64_size_filename_or_decoder_never_writes_rows(store):
    service, repo, session = await setup(store, plain_decoder)
    for filename in ("../journey.txt", "/tmp/file.txt", "file\\private.txt", "bad\x7f.txt", ".env"):
        with pytest.raises(ValueError):
            upload(filename=filename)
    for value in ("%%%", base64.b64encode(b"x" * 1_000_001).decode()):
        with pytest.raises(ValueError):
            await service.admit(repo, session, upload().model_copy(update={"content_base64": value}))

    async def broken(_filename, _raw):
        raise ValueError("private document parser text must not escape")

    service.decoder = broken
    with pytest.raises(ValueError) as error:
        await service.admit(repo, session, upload())
    assert "private document parser text" not in str(error.value)
    assert await service.list(repo, session) == []


async def test_known_credentials_rejected_before_storage_and_after_registry_changes(store):
    service, repo, session = await setup(store, plain_decoder)
    registry = {"FORGE_TEST_TOKEN": "private-attachment-token"}
    service.credential_registry = lambda: registry
    for request in (upload(b"private-attachment-token"), upload(filename="private-attachment-token.txt")):
        with pytest.raises(ValueError, match="credential"):
            await service.admit(repo, session, request)
    assert await service.list(repo, session) == []
    registry.clear()
    receipt = await service.admit(repo, session, upload(b"future-private-token"))
    registry["FORGE_TEST_TOKEN"] = "future-private-token"
    with pytest.raises(ValueError, match="credential"):
        await service.freeze(repo, session, [receipt["id"]])


async def test_real_image_decoder_keeps_distinct_raw_and_clean_hashes(store):
    from PIL import Image, PngImagePlugin

    stream = io.BytesIO()
    metadata = PngImagePlugin.PngInfo()
    metadata.add_text("comment", "Remove this image metadata")
    Image.new("RGB", (16, 16), "white").save(stream, format="PNG", pnginfo=metadata)
    raw = stream.getvalue()
    service, repo, session = await setup(store)
    receipt = await service.admit(repo, session, upload(raw, "reference.png"))
    selected = (await service.freeze(repo, session, [receipt["id"]]))[0]
    assert selected["kind"] == "image" and selected["text"] == ""
    assert b"Remove this image metadata" not in selected["image"]
    assert selected["sha256"] == hashlib.sha256(raw).hexdigest()
    assert selected["image_sha256"] == hashlib.sha256(selected["image"]).hexdigest()
    assert selected["image_sha256"] != selected["sha256"]


async def test_selection_is_bounded_and_native_rows_are_immutable(store):
    service, repo, session = await setup(store, plain_decoder)
    receipt = await service.admit(repo, session, upload())
    with pytest.raises(ValueError):
        await service.freeze(repo, session, [receipt["id"], receipt["id"]])
    with pytest.raises(ValueError):
        await service.freeze(repo, session, [uuid4().hex for _ in range(9)])
    with pytest.raises(DBAPIError):
        async with store._unit() as native:
            await native.execute(text("UPDATE forge_assistant_attachments SET raw_bytes=decode('00','hex')"))


@pytest.mark.parametrize("field", ["raw_bytes", "canonical_image", "extracted_text"])
async def test_freeze_refuses_content_or_image_hash_tampering(store, monkeypatch, field):
    from firefly_forge.assistant_attachment_persistence import ForgeAssistantAttachmentRepository

    async def image(_filename, _raw):
        return {
            "kind": "image",
            "mime_type": "image/png",
            "text": "",
            "image": b"canonical fixture",
            "truncated": False,
        }

    service, repo, session = await setup(store, image)
    receipt = await service.admit(repo, session, upload(b"raw fixture", "reference.png"))
    original = ForgeAssistantAttachmentRepository.selected

    async def damaged(self, session_id, ids):
        rows = await original(self, session_id, ids)
        setattr(rows[0], field, "" if field == "extracted_text" else b"tampered storage")
        if field == "extracted_text":
            rows[0].extracted_text = "Changed derived content"
            rows[0].data = {**rows[0].data, "text_chars": len(rows[0].extracted_text)}
        return rows

    monkeypatch.setattr(ForgeAssistantAttachmentRepository, "selected", damaged)
    with pytest.raises(ValueError, match="hash"):
        await service.freeze(repo, session, [receipt["id"]])
