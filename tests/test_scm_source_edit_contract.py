# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Source-only SCM consent is separate from ordinary resume and assistant grants."""

import importlib
from datetime import UTC, datetime, timedelta

import pytest


def body():
    return dict(
        version=1,
        transport="github_rest_poll_v1",
        request_id="a" * 32,
        expected_previous_digest=None,
        expected_project_binding_digest="b" * 64,
        expected_binding_request_id="c" * 32,
        expected_binding_digest="d" * 64,
        component_ids=["api"],
        issue_number=4,
        run_id="e" * 32,
        activation=dict(
            version=1,
            request_id="f" * 32,
            candidate_digest="1" * 64,
            acknowledge_environment_only=True,
            acknowledge_resume_existing_task=True,
            acknowledge_existing_read_scope=True,
            acknowledge_no_commands=True,
        ),
        expected_source_profile_digest="2" * 64,
        expires_at=(datetime.now(UTC) + timedelta(minutes=15)).isoformat(),
        active=True,
        acknowledge_same_run_source_only=True,
    )


def test_separate_source_trigger_contract_and_parser():
    module = importlib.import_module("firefly_forge.scm_source_edit_models")
    import json

    from firefly_forge.collaboration import RepositoryCommand

    value = module.SCMSourceEditGrantInputV1.model_validate(body())
    assert value.model_dump(mode="json") == body() | {"expires_at": value.expires_at}
    command = RepositoryCommand.parse(
        "/forge source_edit " + json.dumps(dict(version=1, run_id="e" * 32, expected_grant_digest="3" * 64))
    )
    assert command.action == "source_edit" and command.version == 1


@pytest.mark.parametrize(
    "change",
    [
        {"version": True},
        {"active": 1},
        {"acknowledge_same_run_source_only": 1},
        {"commands": []},
        {"component_ids": ["api", "api"]},
        {"expires_at": "2026-10-23T02:00:00"},
        {
            "activation": dict(
                version=1,
                request_id="f" * 32,
                candidate_digest="1" * 64,
                acknowledge_environment_only=True,
                acknowledge_resume_existing_task=True,
                acknowledge_existing_read_scope=True,
                acknowledge_no_commands=False,
            )
        },
    ],
)
def test_no_coercion_or_budget_expansion(change):
    model = importlib.import_module("firefly_forge.scm_source_edit_models").SCMSourceEditGrantInputV1
    with pytest.raises(ValueError):
        model.model_validate(body() | change)


def test_malformed_expiration_is_not_reflected_in_validation_errors():
    model = importlib.import_module("firefly_forge.scm_source_edit_models").SCMSourceEditGrantInputV1
    with pytest.raises(ValueError) as failure:
        model.model_validate(body() | {"expires_at": "do-not-reflect"})
    assert "do-not-reflect" not in str(failure.value)
