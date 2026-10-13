# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
import uuid

import pytest

from firefly_forge.collaboration import Collaboration, ProductDefinition
from firefly_forge.models import RepositoryInput
from firefly_forge.product_assistant import AssistantSessionInput, AssistantTurnInput, ProductAssistant
from firefly_forge.product_assistant_runtime import AssistantReply


async def test_ai_draft_origin_is_bound_to_a_completed_scoped_receipt_and_retains_human_edits(store, tmp_path):
    repo = await store.add_repository(RepositoryInput(name="Stock", url=str(tmp_path)))
    product = ProductDefinition(
        customer="Warehouse operator",
        problem="Avoid overselling",
        journey=["Reserve stock"],
        user_stories=["Reserve available units"],
        acceptance=["Never oversell"],
        owners=["Inventory team"],
    )

    async def designer(prompt, settings):
        return AssistantReply(message="Inventory proposal ready for review", proposal=product)

    assistant = ProductAssistant(store, tmp_path / "assistant", designer=designer)
    session = await assistant.create(repo["id"], AssistantSessionInput())
    turn = await assistant.turn(
        repo["id"], session["id"], AssistantTurnInput(request_id=uuid.uuid4().hex, message="Define stock")
    )
    provenance = turn["provenance"]
    assert provenance["provider"] == "bridge" and provenance["kind"] == "ai_generated"
    origin = {key: provenance[key] for key in ("conversation_id", "turn_id", "proposal_digest")}
    draft = {
        **turn["reply"]["proposal"],
        "problem": "Avoid overselling and explain rejected reservations",
        "ai_origin": origin,
    }
    saved = await Collaboration(store).save_product(repo["id"], ProductDefinition.model_validate(draft))
    assert saved["origin"]["kind"] == "ai_assisted" and saved["origin"]["edited"] is True
    assert saved["origin"]["turn_id"] == turn["id"] and saved["origin"]["provider"] == "bridge"
    assert saved["definition"]["problem"] == draft["problem"]
    other = await store.add_repository(RepositoryInput(name="Other product", url=str(tmp_path / "other")))
    with pytest.raises(ValueError, match="origin|receipt"):
        await Collaboration(store).save_product(other["id"], ProductDefinition.model_validate(draft))


async def test_manual_definition_does_not_claim_ai_generation(store, tmp_path):
    repo = await store.add_repository(RepositoryInput(name="Stock", url=str(tmp_path)))
    product = ProductDefinition(
        customer="Operator",
        problem="Avoid overselling",
        journey=["Reserve"],
        user_stories=["Reserve stock"],
        acceptance=["Never oversell"],
        owners=["Team"],
    )
    saved = await Collaboration(store).save_product(repo["id"], product)
    assert saved["origin"] == {"kind": "manual"}
