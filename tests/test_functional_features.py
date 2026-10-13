# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
import json

import pytest

from firefly_forge.collaboration import ProductDefinition, customer_instruction
from firefly_forge.functional_features import feature_contract


def product(**changes):
    value = {
        "customer": "Stock operators",
        "problem": "Concurrent reservations oversell stock",
        "journey": ["Reserve stock", "Cancel reservation"],
        "user_stories": ["As an operator I reserve available stock"],
        "acceptance": ["Reservations never oversell", "Cancellation restores available stock"],
        "owners": ["Inventory team"],
        "features": [
            {
                "id": "reserve-stock",
                "name": "Reserve available stock",
                "journey_refs": ["Reserve stock"],
                "acceptance_refs": ["acceptance-1"],
                "behaviors": [
                    {
                        "id": "available",
                        "given": "Authorized operator and enough available stock",
                        "when": "The operator requests a reservation",
                        "then": "Persist a reservation and reduce available stock atomically",
                        "failure_paths": ["Insufficient stock returns conflict without a partial reservation"],
                    }
                ],
            }
        ],
    }
    value.update(changes)
    return ProductDefinition.model_validate(value)


def test_feature_references_and_behavior_ids_must_be_real_and_unique():
    original = product().model_dump()
    for field, invalid in [("journey_refs", ["Invented journey"]), ("acceptance_refs", ["acceptance-99"])]:
        bad = json.loads(json.dumps(original))
        bad["features"][0][field] = invalid
        with pytest.raises(ValueError, match="references"):
            ProductDefinition.model_validate(bad)
    original["features"][0]["behaviors"] *= 2
    with pytest.raises(ValueError, match="unique"):
        ProductDefinition.model_validate(original)


def test_pending_journey_cannot_be_marked_functionally_reviewed():
    pending = feature_contract(product())
    assert pending["status"] == "pending"
    assert pending["pending_fields"] == [{"journey": "Cancel reservation", "field": "features"}]
    with pytest.raises(ValueError, match="every journey"):
        product(functional_reviewed=True)


def test_frozen_implementer_instruction_carries_expected_behavior_and_failure_paths():
    value = product(journey=["Reserve stock"], functional_reviewed=True)
    contract = feature_contract(value)
    assert contract["status"] == "ready" and contract["journeys"][0]["feature_ids"] == ["reserve-stock"]
    prompt = customer_instruction({"revision": 7, "definition": value.model_dump()})
    assert "Given/When/Then" in prompt and "without a partial reservation" in prompt
    assert "functional_reviewed" in prompt
