# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""The actual typed reply is complete; a public bound causes repair rather than silent truncation."""

import pytest

from firefly_forge.scm_natural_feedback import natural_card
from firefly_forge.scm_natural_models import NaturalDecision


def test_complete_multiline_model_reply_is_published_instead_of_first_twenty_lines():
    message = "\n".join(f"Concrete line {index}" for index in range(1, 24))
    decision = NaturalDecision(intent="dialogue", message=message)
    body = natural_card(
        {
            "command": {"language": "en"},
            "natural_turn": {"state": "dialogue", "reply": message, "decision": decision.model_dump()},
        },
        lambda text: text,
    )
    assert "Concrete line 23" in body and len(body) <= 6000


def test_reply_too_large_for_a_complete_public_quote_fails_the_typed_contract():
    with pytest.raises(ValueError):
        NaturalDecision(intent="dialogue", message="*" * 3500)
