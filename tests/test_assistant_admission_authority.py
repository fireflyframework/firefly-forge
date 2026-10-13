# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Async context collection cannot retain revoked conversation authority."""

import base64
from uuid import uuid4

import pytest

from firefly_forge.assistant_attachments import AssistantAttachments, AttachmentInput
from firefly_forge.identity import Principal
from firefly_forge.models import RepositoryInput
from firefly_forge.product_assistant import AssistantSessionInput, AssistantTurnInput, ProductAssistant
from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied, ProjectAccess
from firefly_forge.project_models import ProjectDefinition
from firefly_forge.projects import Projects


async def setup(store, tmp_path):
    repo = await store.add_repository(RepositoryInput(name="Authority boundary", url=str(tmp_path)))
    project = await Projects(store).configure(
        ProjectDefinition(
            name="Current product authority",
            description="Async context collection uses the current project grant",
            topology="monorepo",
            owners=["Team"],
            components=[dict(id="api", name="API", repository_id=repo["id"], responsibility="API")],
        )
    )
    user = Principal(uuid4(), "https://id.example/realms/forge", uuid4().hex)
    access = ProjectAccess(store)
    await access.set_membership(project["id"], user, "developer")
    manager = ProductAssistant(store, tmp_path / "assistant")
    session = await manager.create(repo["id"], AssistantSessionInput(project_id=project["id"]))
    return manager, repo["id"], session["id"], project["id"], user, access


async def test_downgrade_during_file_freeze_denies_turn_before_admission(store, tmp_path, monkeypatch):
    manager, repo, sid, project, user, access = await setup(store, tmp_path)
    attachments = AssistantAttachments(store)
    file = await attachments.admit(
        repo,
        sid,
        AttachmentInput(
            request_id="a" * 32, filename="notes.txt", content_base64=base64.b64encode(b"Customer notes").decode()
        ),
    )
    original = attachments.freeze

    async def downgrade(*args):
        selected = await original(*args)
        await access.set_membership(project, user, "viewer")
        return selected

    monkeypatch.setattr(attachments, "freeze", downgrade)
    manager.attachments = attachments
    context = CURRENT_PRINCIPAL.set(user)
    try:
        with pytest.raises(AccessDenied):
            await manager.turn(
                repo,
                sid,
                AssistantTurnInput(request_id="b" * 32, message="Review the notes", attachment_ids=[file["id"]]),
            )
    finally:
        CURRENT_PRINCIPAL.reset(context)
    observed = await manager.session(repo, sid)
    assert observed["turns"] == []
    assert [entry["kind"] for entry in observed["journal"]["entries"]] == ["conversation_created"]


async def test_capability_read_rechecks_access_after_provider_await(store, tmp_path, monkeypatch):
    from firefly_forge.providers import ConfiguredProvider

    manager, repo, sid, project, user, access = await setup(store, tmp_path)

    class Provider(ConfiguredProvider):
        async def image_input_capability(self, **kwargs):
            await access.set_membership(project, user, None)
            return {"status": "verified", "config_revision": "3" * 64}

    monkeypatch.setattr("firefly_forge.providers.get_provider", lambda agent, settings: Provider(settings))
    context = CURRENT_PRINCIPAL.set(user)
    try:
        with pytest.raises(AccessDenied):
            await manager.capabilities(repo, sid)
    finally:
        CURRENT_PRINCIPAL.reset(context)
    assert (await manager.session(repo, sid))["turns"] == []
