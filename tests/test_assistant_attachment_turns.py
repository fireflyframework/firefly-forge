# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Conversation files reach the model through scoped, replay-safe frozen context."""

import base64
import hashlib
import importlib
import uuid

import pytest

from firefly_forge.models import RepositoryInput
from firefly_forge.product_assistant import AssistantSessionInput, AssistantTurnInput, ProductAssistant
from firefly_forge.product_assistant_runtime import AssistantReply


async def prepared(store, tmp_path, designer):
    service = importlib.import_module("firefly_forge.assistant_attachments")
    attachments = service.AssistantAttachments(store)
    assistant = ProductAssistant(store, tmp_path / "assistant", designer=designer, attachments=attachments)
    repo = await store.add_repository(RepositoryInput(name="Inventory", url=str(tmp_path)))
    session = await assistant.create(repo["id"], AssistantSessionInput())
    text = b"Warehouse reservation rule: never reserve more than 14 items."
    file = await attachments.admit(
        repo["id"],
        session["id"],
        service.AttachmentInput(
            request_id=uuid.uuid4().hex, filename="journey.md", content_base64=base64.b64encode(text).decode()
        ),
    )
    return assistant, repo, session, file, text


async def test_frozen_file_context_replay_and_followup_selection(store, tmp_path):
    prompts = []

    async def designer(prompt, settings):
        prompts.append(prompt)
        return AssistantReply(message="Reservation behavior is ready for review")

    assistant, repo, session, file, text = await prepared(store, tmp_path, designer)
    request = AssistantTurnInput(
        request_id="c" * 32, message="Define reservation behavior", attachment_ids=[file["id"]]
    )
    result = await assistant.turn(repo["id"], session["id"], request)
    assert result["status"] == "completed"
    assert "never reserve more than 14 items" in prompts[0]
    assert result["context"]["attachments"][0]["sha256"] == hashlib.sha256(text).hexdigest()
    assert "raw_bytes" not in result["context"]["attachments"][0]
    assert await assistant.turn(repo["id"], session["id"], request) == result and len(prompts) == 1
    with pytest.raises(ValueError, match="different"):
        await assistant.turn(repo["id"], session["id"], request.model_copy(update={"attachment_ids": []}))
    followup = await assistant.turn(
        repo["id"], session["id"], AssistantTurnInput(request_id="d" * 32, message="Refine failure paths")
    )
    assert followup["context"]["attachments"][0]["id"] == file["id"]
    detached = await assistant.turn(
        repo["id"],
        session["id"],
        AssistantTurnInput(request_id="e" * 32, message="Discuss naming only", attachment_ids=[]),
    )
    assert detached["context"]["attachments"] == []
    assert (await assistant.session(repo["id"], session["id"]))["journal"]["verified"]


async def test_foreign_conversation_file_never_creates_turn_intent(store, tmp_path):
    calls = []

    async def designer(prompt, settings):
        calls.append(prompt)
        return AssistantReply(message="Not called")

    assistant, repo, session, file, _ = await prepared(store, tmp_path, designer)
    other = await assistant.create(repo["id"], AssistantSessionInput())
    with pytest.raises((ValueError, KeyError)):
        await assistant.turn(
            repo["id"],
            other["id"],
            AssistantTurnInput(request_id="f" * 32, message="Read another conversation", attachment_ids=[file["id"]]),
        )
    assert calls == [] and (await assistant.session(repo["id"], other["id"]))["turns"] == []


async def test_unverified_image_does_not_admit_a_model_turn(store, tmp_path):
    import io

    from PIL import Image

    service = importlib.import_module("firefly_forge.assistant_attachments")
    calls = []

    async def designer(prompt, settings, **kwargs):
        calls.append(prompt)
        return AssistantReply(message="Not called")

    assistant, repo, session, _, _ = await prepared(store, tmp_path, designer)
    stream = io.BytesIO()
    Image.new("RGB", (24, 24), "white").save(stream, format="PNG")
    image = await assistant.attachments.admit(
        repo["id"],
        session["id"],
        service.AttachmentInput(
            request_id=uuid.uuid4().hex,
            filename="wireframe.png",
            content_base64=base64.b64encode(stream.getvalue()).decode(),
        ),
    )
    with pytest.raises(ValueError, match="Verify image input"):
        await assistant.turn(
            repo["id"],
            session["id"],
            AssistantTurnInput(request_id="7" * 32, message="Inspect layout", attachment_ids=[image["id"]]),
        )
    assert calls == [] and (await assistant.session(repo["id"], session["id"]))["turns"] == []


async def test_verified_image_and_file_citation_reach_frozen_turn(store, tmp_path, monkeypatch):
    import io

    from PIL import Image

    from firefly_forge import providers

    class VerifiedBridge(providers.BridgeProvider):
        async def image_input_capability(self):
            return {"status": "verified", "model": "bridge/default", "config_revision": "a" * 64}

    monkeypatch.setattr(providers, "get_provider", lambda *args: VerifiedBridge({}))
    received = []

    async def designer(prompt, settings, *, images, image_input_receipt):
        assert image_input_receipt["status"] == "verified"
        received.extend(images)
        return AssistantReply(message="The image layout is ready for review", citation_ids=[images[0]["id"]])

    assistant, repo, session, _, _ = await prepared(store, tmp_path, designer)
    service = importlib.import_module("firefly_forge.assistant_attachments")
    stream = io.BytesIO()
    Image.new("RGB", (24, 24), "white").save(stream, format="PNG")
    image = await assistant.attachments.admit(
        repo["id"],
        session["id"],
        service.AttachmentInput(
            request_id=uuid.uuid4().hex,
            filename="wireframe.png",
            content_base64=base64.b64encode(stream.getvalue()).decode(),
        ),
    )
    result = await assistant.turn(
        repo["id"],
        session["id"],
        AssistantTurnInput(request_id="8" * 32, message="Inspect layout", attachment_ids=[image["id"]]),
    )
    assert result["status"] == "completed" and received[0]["image"].startswith(b"\x89PNG")
    assert result["context"]["image_input_receipt"]["status"] == "verified"
    assert result["reply"]["citation_ids"] == [image["id"]]
    assert not any(key in result["context"]["attachments"][0] for key in ("image", "raw_bytes", "text"))


async def test_firefly_runtime_supplies_actual_binary_content_to_multimodal_model():
    import io

    from PIL import Image
    from pydantic_ai import BinaryContent
    from pydantic_ai.messages import ModelResponse, TextPart, UserPromptPart
    from pydantic_ai.models.function import FunctionModel

    from firefly_forge.product_assistant_runtime import design_reply

    observed = []

    async def function(messages, info):
        for part in messages[-1].parts:
            if isinstance(part, UserPromptPart) and isinstance(part.content, list):
                observed.extend(value for value in part.content if isinstance(value, BinaryContent))
        return ModelResponse(parts=[TextPart('{"message":"Actual image input received"}')])

    image = io.BytesIO()
    Image.new("RGB", (24, 24), "white").save(image, format="PNG")
    image_bytes = image.getvalue()
    result = await design_reply(
        "Inspect customer image",
        {"ASSISTANT_PROVIDER": "bridge"},
        model=FunctionModel(function),
        images=[{"image": image_bytes, "mime_type": "image/png"}],
    )
    assert result.message == "Actual image input received"
    assert observed[0].data == image_bytes and observed[0].media_type == "image/png"


async def test_assistant_bridge_transport_carries_verified_image_revision(monkeypatch):
    import io
    import json

    import httpx
    import openai
    from PIL import Image

    from firefly_forge.product_assistant_runtime import design_reply

    original = openai.AsyncOpenAI
    seen = []

    async def transport(request):
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "id": "image-receipt",
                "object": "chat.completion",
                "created": 0,
                "model": "bridge/default",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": '{"message":"Wireframe received"}'},
                        "finish_reason": "stop",
                    }
                ],
            },
        )

    class Client(original):
        def __init__(self, **kwargs):
            super().__init__(**kwargs, http_client=httpx.AsyncClient(transport=httpx.MockTransport(transport)))

    monkeypatch.setattr(openai, "AsyncOpenAI", Client)
    stream = io.BytesIO()
    Image.new("RGB", (24, 24), "white").save(stream, format="PNG")
    receipt = {"status": "verified", "model": "bridge/default", "config_revision": "a" * 64}
    reply = await design_reply(
        "Review this wireframe",
        {
            "ASSISTANT_PROVIDER": "bridge",
            "FORGE_BRIDGE_URL": "http://127.0.0.1:18766",
            "FORGE_BRIDGE_TOKEN": "test-only-bridge-token",
        },
        images=[{"image": stream.getvalue(), "mime_type": "image/png"}],
        image_input_receipt=receipt,
    )
    assert reply.message == "Wireframe received"
    assert len(seen) == 1 and seen[0].headers["X-Bridge-Image-Revision"] == receipt["config_revision"]
    body = json.loads(seen[0].content)
    images = [
        part
        for message in body["messages"]
        if isinstance(message["content"], list)
        for part in message["content"]
        if part["type"] == "image_url"
    ]
    assert len(images) == 1 and images[0]["image_url"]["url"].startswith("data:image/png;base64,")


@pytest.mark.parametrize("subject", ["developer", "administrator"])
async def test_revocation_during_context_retrieval_prevents_assistant_model_dispatch(store, tmp_path, subject):
    from uuid import uuid4

    from firefly_forge.identity import Principal
    from firefly_forge.identity_persistence import ForgeIdentityUserEntity, ForgeIdentityUserRepository
    from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied, ProjectAccess
    from firefly_forge.project_models import ProjectDefinition
    from firefly_forge.projects import Projects

    repo = await store.add_repository(RepositoryInput(name="Inventory", url=str(tmp_path)))
    project = await Projects(store).configure(
        ProjectDefinition(
            name="Inventory",
            description="Reservation journeys",
            topology="monorepo",
            owners=["Platform"],
            components=[dict(id="inventory", name="Inventory", repository_id=repo["id"], responsibility="Reserve")],
        )
    )
    user = Principal(
        uuid4(),
        "https://identity.example/realms/forge",
        uuid4().hex,
        api_roles=frozenset({"forge-admin"}) if subject == "administrator" else frozenset(),
    )
    access = ProjectAccess(store)
    await access.set_membership(project["id"], user, "developer")
    calls = []

    class Knowledge:
        async def retrieve(self, request):
            if subject == "developer":
                await access.set_membership(project["id"], user, "viewer")
            else:
                async with store._unit():
                    identities = ForgeIdentityUserRepository()
                    row = await identities.find_by_id(user.id)
                    assert isinstance(row, ForgeIdentityUserEntity)
                    row.enabled = False
                    await identities.save(row)
            return {"citations": []}

    async def designer(prompt, settings):
        calls.append(prompt)
        return AssistantReply(message="Must not run after revocation")

    assistant = ProductAssistant(store, tmp_path / "assistant", knowledge=Knowledge(), designer=designer)
    actor = CURRENT_PRINCIPAL.set(user)
    try:
        session = await assistant.create(repo["id"], AssistantSessionInput(project_id=project["id"]))
        with pytest.raises(AccessDenied):
            await assistant.turn(
                repo["id"],
                session["id"],
                AssistantTurnInput(request_id=uuid.uuid4().hex, message="Discuss reservations"),
            )
        assert calls == [], "Revoked or suspended actors cannot dispatch a new assistant invocation"
    finally:
        CURRENT_PRINCIPAL.reset(actor)
    retained = await assistant.session(repo["id"], session["id"])
    assert retained["turns"][0]["status"] == "failed" and retained["journal"]["verified"]
