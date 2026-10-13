# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""AI attribution tied to actual scoped, immutable conversation receipts."""

from __future__ import annotations

import hashlib
import json
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from firefly_forge.assistant_persistence import (
    ForgeAssistantEventRepository,
    ForgeAssistantSessionRepository,
    ForgeAssistantTurnRepository,
)


class AIOrigin(BaseModel):
    model_config = ConfigDict(extra="forbid")
    conversation_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    turn_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    proposal_digest: str = Field(pattern=r"^[a-f0-9]{64}$")


def content_digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


def receipt_provenance(turn: dict[str, Any], session: dict[str, Any]) -> dict[str, Any] | None:
    if turn.get("status") != "completed" or not turn.get("reply"):
        return None
    proposal = turn["reply"].get("proposal")
    return {
        "kind": "ai_generated",
        "provider": session["agent"],
        "model": session.get("execution_config", {}).get("model")
        or ("bridge/default" if session["agent"] == "bridge" else None),
        "generated_at": turn["updated_at"],
        "conversation_id": session["id"],
        "turn_id": turn["id"],
        "proposal_digest": content_digest(proposal) if proposal is not None else None,
    }


async def validate_origin(store: Any, repository_id: str, definition: Any) -> dict[str, Any]:
    source = definition.ai_origin
    if source is None:
        return {"kind": "manual"}
    async with store._unit():
        session = await ForgeAssistantSessionRepository().find_by_id(UUID(hex=source.conversation_id))
        turn = await ForgeAssistantTurnRepository().find_by_id(UUID(hex=source.turn_id))
        if (
            session is None
            or session.repository_id != UUID(hex=repository_id)
            or turn is None
            or turn.session_id != session.id
        ):
            raise ValueError("AI origin must refer to a completed receipt in this product")
        events = [record.data for record in await ForgeAssistantEventRepository().events(session.id)]
        previous = "0" * 64
        receipt = None
        for sequence, event in enumerate(events, 1):
            if (
                event["sequence"] != sequence
                or event["previous"] != previous
                or content_digest({key: value for key, value in event.items() if key != "digest"}) != event["digest"]
            ):
                raise ValueError("AI origin receipt failed journal verification")
            previous = event["digest"]
            if event["kind"] == "turn_completed" and event["payload"].get("turn_id") == source.turn_id:
                receipt = event["payload"]
        if (
            previous != session.data["journal_head"]
            or receipt is None
            or receipt.get("reply") != turn.data.get("reply")
        ):
            raise ValueError("AI origin requires the original immutable completed reply receipt")
        provenance = receipt_provenance(turn.data, session.data)
        proposal = (turn.data.get("reply") or {}).get("proposal")
        if provenance is None or proposal is None or provenance["proposal_digest"] != source.proposal_digest:
            raise ValueError("AI origin does not match the completed proposal receipt")
        current = definition.model_dump(exclude={"ai_origin"})
        original = {key: value for key, value in proposal.items() if key != "ai_origin"}
        return {**provenance, "kind": "ai_assisted", "edited": content_digest(current) != content_digest(original)}
