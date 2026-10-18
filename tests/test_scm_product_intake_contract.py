# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Explicit source-channel intake never supplies actor or execution authority."""

import json

import pytest

from firefly_forge.collaboration import RepositoryCommand


def intake():
    return dict(
        version=1,
        request_id="1" * 32,
        expected_consent_request_id="2" * 32,
        expected_consent_digest="3" * 64,
        name="Stock",
        description="Track stock movements",
        component=dict(id="app", name="Stock API", responsibility="Track stock", backend="pyfly", frontend="none"),
        definition=dict(
            customer="Warehouse operator",
            problem="Stock counts drift",
            journey=["Count stock"],
            user_stories=["As an operator, I count stock"],
            acceptance=["The count is retained"],
            owners=["Warehouse team"],
        ),
    )


def parse(body):
    return RepositoryCommand.parse("/forge intake " + json.dumps(body))


def test_explicit_intake_is_parsed_without_an_invented_repository_or_run():
    command = parse(intake())
    assert command.action == "intake" and command.request_id == "1" * 32
    assert command.definition.customer == "Warehouse operator"
    assert command.component.id == "app"


@pytest.mark.parametrize("value", [True, False, 1.0, "1", 2, None])
def test_intake_version_never_coerces_to_authority(value):
    with pytest.raises(ValueError):
        parse({**intake(), "version": value})


@pytest.mark.parametrize(
    "extra",
    [
        {"actor": "owner"},
        {"repository_id": "a" * 32},
        {"url": "https://other.example/x"},
        {"publish": True},
        {"run_id": "a" * 32},
        {"agent": "bridge"},
        {"action": "product"},
    ],
)
def test_intake_does_not_accept_execution_or_external_actor_overrides(extra):
    with pytest.raises(ValueError):
        parse({**intake(), **extra})


@pytest.mark.parametrize("field", ["ai_origin", "architecture", "design_review", "functional_reviewed"])
def test_intake_cannot_claim_completed_review_or_model_provenance(field):
    definition = {**intake()["definition"], field: True if field == "functional_reviewed" else {}}
    with pytest.raises(ValueError):
        parse({**intake(), "definition": definition})


def test_intake_does_not_accept_operator_commands_or_foreign_component_ids():
    body = intake()
    with pytest.raises(ValueError):
        parse({**body, "component": {**body["component"], "verification": [{"argv": ["sh", "-c", "other"]}]}})
    with pytest.raises(ValueError):
        parse({**body, "component": {**body["component"], "repository_id": "a" * 32}})
