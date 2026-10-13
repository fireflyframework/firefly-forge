# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Operator-authenticated AI product dialogue and reviewable proposal history."""

from typing import Any
from urllib.parse import quote

from pyfly.container import rest_controller
from pyfly.web import Body, PathVar, get_mapping, post_mapping, request_mapping
from starlette.responses import Response

from firefly_forge.assistant_attachments import AssistantAttachments, AttachmentInput
from firefly_forge.product_assistant import (
    AssistantRecoveryInput,
    AssistantSessionInput,
    AssistantTurnInput,
    ProductAssistant,
)


@rest_controller
@request_mapping("/api/v1/repositories/{repository_id}/assistant/conversations")
class AssistantController:
    def __init__(self, assistant: ProductAssistant, attachments: AssistantAttachments) -> None:
        self.manager = assistant
        self.attachments = attachments

    @get_mapping("/{conversation_id}/attachments")
    async def files(self, repository_id: PathVar[str], conversation_id: PathVar[str]) -> list[dict[str, Any]]:
        return await self.attachments.list(repository_id, conversation_id)

    @post_mapping("/{conversation_id}/attachments", status_code=201)
    async def upload(
        self, repository_id: PathVar[str], conversation_id: PathVar[str], body: Body[AttachmentInput]
    ) -> dict[str, Any]:
        return await self.attachments.admit(repository_id, conversation_id, body)

    @get_mapping("/{conversation_id}/attachments/{attachment_id}")
    async def file(
        self, repository_id: PathVar[str], conversation_id: PathVar[str], attachment_id: PathVar[str]
    ) -> dict[str, Any]:
        return await self.attachments.get(repository_id, conversation_id, attachment_id)

    @get_mapping("/{conversation_id}/attachments/{attachment_id}/content")
    async def content(
        self, repository_id: PathVar[str], conversation_id: PathVar[str], attachment_id: PathVar[str]
    ) -> Response:
        item = (await self.attachments.freeze(repository_id, conversation_id, [attachment_id]))[0]
        return Response(
            item["raw_bytes"],
            media_type=item["mime_type"],
            headers={"Content-Disposition": "attachment; filename*=UTF-8''" + quote(item["filename"], safe="")},
        )

    @get_mapping("")
    async def sessions(self, repository_id: PathVar[str]) -> list[dict[str, Any]]:
        return await self.manager.sessions(repository_id)

    @post_mapping("", status_code=201)
    async def create(self, repository_id: PathVar[str], body: Body[AssistantSessionInput]) -> dict[str, Any]:
        return await self.manager.create(repository_id, body)

    @get_mapping("/{conversation_id}")
    async def session(self, repository_id: PathVar[str], conversation_id: PathVar[str]) -> dict[str, Any]:
        return await self.manager.session(repository_id, conversation_id)

    @get_mapping("/{conversation_id}/capabilities")
    async def capabilities(self, repository_id: PathVar[str], conversation_id: PathVar[str]) -> dict[str, Any]:
        return await self.manager.capabilities(repository_id, conversation_id)

    @post_mapping("/{conversation_id}/turns", status_code=201)
    async def turn(
        self, repository_id: PathVar[str], conversation_id: PathVar[str], body: Body[AssistantTurnInput]
    ) -> dict[str, Any]:
        return await self.manager.turn(repository_id, conversation_id, body)

    @post_mapping("/{conversation_id}/turns/{turn_id}/recover")
    async def recover(
        self,
        repository_id: PathVar[str],
        conversation_id: PathVar[str],
        turn_id: PathVar[str],
        body: Body[AssistantRecoveryInput],
    ) -> dict[str, Any]:
        return await self.manager.recover_turn(repository_id, conversation_id, turn_id, body)
