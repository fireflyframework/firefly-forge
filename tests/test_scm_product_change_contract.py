# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Strict additive product-edit capability; no provider or database access."""

import importlib
import importlib.util
import json

import pytest
from pydantic import ValidationError

from firefly_forge.collaboration import RepositoryCommand
from firefly_forge.scm_authority_models import SCMActorBindingInputV1
from firefly_forge.scm_resume_models import SCMResumeGrantInputV2


def models():
    assert importlib.util.find_spec("firefly_forge.scm_product_change_models") is not None, (
        "Product edit contracts missing"
    )
    return importlib.import_module("firefly_forge.scm_product_change_models")


def grant():
    return dict(
        version=1,
        request_id="1" * 32,
        expected_previous_digest=None,
        expected_project_binding_digest="2" * 64,
        expected_binding_request_id="3" * 32,
        expected_binding_digest="4" * 64,
        component_ids=["api", "web"],
        allowed_actions=["product_change"],
        active=True,
        acknowledge_product_definition_edit=True,
    )


def command():
    return dict(
        version=1,
        action="product_change",
        project_id="5" * 32,
        expected_project_binding_digest="2" * 64,
        expected_grant_request_id="6" * 32,
        expected_grant_digest="7" * 64,
        change=dict(request_id="8" * 32, base_revision=1, base_digest="9" * 64, append_journeys=["Escalate ticket"]),
    )


def test_explicit_edit_is_not_a_v1_reply_or_v2_resume_grant():
    module = models()
    assert module.SCMProductChangeGrantInputV1.model_validate(grant()).allowed_actions == ["product_change"]
    assert SCMActorBindingInputV1.model_json_schema()["properties"]["allowed_actions"]["items"]["enum"] == [
        "respond",
        "status",
        "cancel",
    ]
    assert SCMResumeGrantInputV2.model_json_schema()["properties"]["allowed_actions"]["items"]["const"] == "resume"
    parsed = RepositoryCommand.parse(
        "/forge product_change " + json.dumps({k: v for k, v in command().items() if k != "action"})
    )
    assert parsed.model_dump(mode="json") == module.SCMProductChangeCommandV1.model_validate(command()).model_dump(
        mode="json"
    )


@pytest.mark.parametrize("value", [True, False, 1.0, "1", 2, None])
def test_version_is_exact_integer(value):
    for kind, data in [("SCMProductChangeGrantInputV1", grant()), ("SCMProductChangeCommandV1", command())]:
        with pytest.raises(ValidationError):
            getattr(models(), kind).model_validate({**data, "version": value})


@pytest.mark.parametrize(
    "update",
    [
        {"acknowledge_product_definition_edit": False},
        {"acknowledge_product_definition_edit": 1},
        {"component_ids": []},
        {"component_ids": ["api", "api"]},
        {"allowed_actions": ["respond"]},
        {"allowed_actions": ["product_change", "product_change"]},
        {"active": "true"},
        {"native_user_id": "a" * 32},
        {"actor": "owner"},
        {"expected_binding_digest": "invalid"},
    ],
)
def test_grant_refuses_implicit_authority_or_unbounded_scope(update):
    with pytest.raises(ValidationError):
        models().SCMProductChangeGrantInputV1.model_validate({**grant(), **update})


@pytest.mark.parametrize("update", [{"run_id": "a" * 32}, {"publish": True}, {"actor": "owner"}, {"agent": "bridge"}])
def test_product_command_cannot_authorize_execution_or_actor_labels(update):
    with pytest.raises(ValidationError):
        models().SCMProductChangeCommandV1.model_validate({**command(), **update})


def test_nested_change_is_revalidated_strictly_and_identity_is_never_generated():
    data = command()
    for key, value in [("base_revision", True), ("request_id", None), ("request_id", "")]:
        with pytest.raises(ValidationError):
            models().SCMProductChangeCommandV1.model_validate({**data, "change": {**data["change"], key: value}})


def test_native_constructor_annotations_resolve_without_a_circular_runtime_import():
    from typing import get_type_hints

    from firefly_forge.collaboration import Collaboration

    assert "product_change_authority" in get_type_hints(Collaboration.__init__)


def test_new_scm_change_bound_counts_utf8_bytes_not_only_python_characters():
    data = command()
    data["change"]["append_journeys"] = ["中" * 3499 + chr(65 + n) for n in range(20)]
    with pytest.raises(ValidationError, match="80|byte"):
        models().SCMProductChangeCommandV1.model_validate(data)
