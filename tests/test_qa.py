# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
import importlib

import pytest


def schema():
    try:
        return importlib.import_module("firefly_forge.qa")
    except ImportError as exc:
        raise AssertionError("Bounded journey QA contract is missing") from exc


def journey(**updates):
    return {
        "journey_id": "reserve-stock",
        "name": "Reserve inventory",
        "path": "/",
        "width": 390,
        "height": 844,
        "theme": "dark",
        "zoom": 100,
        "expected_states": [{"id": "reserved", "expected": "Reservation succeeds and stock decreases"}],
        "actions": [
            {"kind": "fill", "state_id": "quantity", "selector": "#quantity", "value": "2"},
            {"kind": "click", "state_id": "submitted", "selector": "#reserve"},
            {
                "kind": "assert",
                "state_id": "reserved",
                "selector": "#status",
                "assertion": "text",
                "value": "Reserved",
                "screenshot": True,
            },
        ],
        **updates,
    }


def test_journey_is_a_typed_plan_before_any_browser_effect():
    value = schema().BrowserJourney.model_validate(journey())
    assert value.actions[2].kind == "assert"
    assert value.expected_states[0].id == "reserved"


@pytest.mark.parametrize(
    "updates",
    [
        {"actions": [{"kind": "evaluate", "state_id": "bad", "value": "process.exit()"}]},
        {"path": "https://example.com"},
        {"zoom": 201},
        {"actions": [{"kind": "navigate", "state_id": "external", "value": "//example.com"}]},
        {"actions": [{"kind": "click", "state_id": "bad", "selector": "js=alert(1)"}]},
        {"actions": [{"kind": "click", "state_id": str(i), "selector": "button"} for i in range(21)]},
    ],
)
def test_journey_rejects_unsafe_actions_external_navigation_and_unbounded_plans(updates):
    with pytest.raises(ValueError):
        schema().BrowserJourney.model_validate(journey(**updates))


def test_qa_instructions_require_expected_state_reference_and_residual_review():
    text = schema().qa_instructions()
    assert "expected" in text.lower() and "reference" in text.lower() and "not reviewed" in text.lower()
    assert "keyboard" in text.lower() and "loading" in text.lower() and "zoom" in text.lower()


def test_image_history_keeps_latest_five_and_marks_evicted_bytes_not_attached():
    from pydantic_ai.messages import BinaryContent, ModelRequest, UserPromptPart

    messages = [
        ModelRequest(
            parts=[
                UserPromptPart(
                    content=[f"Evidence {i}", BinaryContent(data=b"png" + bytes([i]), media_type="image/png")]
                )
            ]
        )
        for i in range(7)
    ]
    bounded = schema().bounded_image_history(messages)
    images = [
        item
        for message in bounded
        for part in message.parts
        if isinstance(part, UserPromptPart) and not isinstance(part.content, str)
        for item in part.content
        if isinstance(item, BinaryContent)
    ]
    assert len(images) == 5 and images[0].data.endswith(bytes([2]))
    assert "not attached" in str(bounded[0].parts[0].content)
    assert isinstance(messages[0].parts[0].content[1], BinaryContent)


def test_journey_reference_reserves_one_of_five_image_slots():
    actions = [{"kind": "press", "state_id": str(i), "value": "Tab", "screenshot": True} for i in range(5)]
    with pytest.raises(ValueError, match="reference"):
        schema().BrowserJourney.model_validate(journey(actions=actions, reference_id="approved"))


def test_image_history_enforces_decoded_byte_budget_not_just_count():
    from pydantic_ai.messages import BinaryContent, ModelRequest, UserPromptPart

    images = [BinaryContent(data=b"x" * 2_500_000, media_type="image/png") for _ in range(3)]
    bounded = schema().bounded_image_history([ModelRequest(parts=[UserPromptPart(content=images)])])
    retained = [item for item in bounded[0].parts[0].content if isinstance(item, BinaryContent)]
    assert len(retained) == 2 and sum(len(item.data) for item in retained) == 5_000_000
