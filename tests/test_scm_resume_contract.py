# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Additive opt-in wire contracts; V1 never acquires resume authority."""

import importlib
import importlib.util

import pytest
from pydantic import ValidationError

from firefly_forge.scm_authority_models import SCMActorBindingInputV1


def models():
    assert importlib.util.find_spec("firefly_forge.scm_resume_models") is not None, (
        "Scoped resume V2 is not implemented"
    )
    return importlib.import_module("firefly_forge.scm_resume_models")


def grant():
    return dict(
        version=2,
        request_id="1" * 32,
        expected_previous_digest=None,
        expected_project_binding_digest="2" * 64,
        expected_binding_request_id="3" * 32,
        expected_binding_digest="4" * 64,
        component_ids=["api"],
        allowed_actions=["resume"],
        active=True,
        acknowledge_same_run_resume=True,
    )


def command():
    return dict(
        version=2,
        action="resume",
        run_id="5" * 32,
        expected_grant_digest="6" * 64,
        expected_question_id="question-1",
        expected_response_digest="7" * 64,
        expected_cut_digest="8" * 64,
    )


def test_v1_action_schema_remains_closed_without_resume():
    schema = SCMActorBindingInputV1.model_json_schema()
    assert schema["properties"]["allowed_actions"]["items"]["enum"] == ["respond", "status", "cancel"]
    assert schema["properties"]["version"] == {"const": 1, "title": "Version", "type": "integer"}


def test_v2_requires_fresh_explicit_same_run_admin_consent():
    module = models()
    result = module.SCMResumeGrantInputV2.model_validate(grant())
    assert result.allowed_actions == ["resume"] and result.version == 2
    for acknowledgment in (False, 1, "true", None):
        body = {**grant(), "acknowledge_same_run_resume": acknowledgment}
        with pytest.raises(ValidationError):
            module.SCMResumeGrantInputV2.model_validate(body)
    with pytest.raises(ValidationError):
        module.SCMResumeGrantInputV2.model_validate({**grant(), "version": 1})


@pytest.mark.parametrize("actions", [["resume", "resume"], ["publish"], ["handoff"], ["merge"], ["respond"], []])
def test_resume_grant_cannot_expand_other_actions(actions):
    with pytest.raises(ValidationError):
        models().SCMResumeGrantInputV2.model_validate({**grant(), "allowed_actions": actions})


@pytest.mark.parametrize(
    "name,value",
    [
        ("agent", "codex"),
        ("model_profile_id", "different"),
        ("auto", True),
        ("publish", True),
        ("project_id", "9" * 32),
        ("actor", "another-user"),
    ],
)
def test_resume_command_cannot_change_frozen_authority_or_execution(name, value):
    with pytest.raises(ValidationError):
        models().SCMResumeCommandV2.model_validate({**command(), name: value})


def test_resume_command_pins_exact_grant_question_answer_and_cut():
    model = models().SCMResumeCommandV2
    parsed = model.model_validate(command())
    assert parsed.model_dump(mode="json") == command()
    for field in ("expected_grant_digest", "expected_question_id", "expected_response_digest", "expected_cut_digest"):
        missing = command()
        del missing[field]
        with pytest.raises(ValidationError):
            model.model_validate(missing)
