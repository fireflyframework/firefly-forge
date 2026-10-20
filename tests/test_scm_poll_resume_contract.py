# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Polling continuation needs new transport-bound consent, never an upgraded mapping."""

import copy
import importlib

import pytest
from pydantic import ValidationError
from test_scm_resume_contract import grant
from test_scm_resume_cut import answered_cut  # noqa: F401

from firefly_forge.journal import digest
from firefly_forge.scm_poll_models import SCMPollActorBindingInputV2
from firefly_forge.scm_resume_models import SCMResumeGrantInputV2


def polling_answer(scope):
    scope = copy.deepcopy(scope)
    actor = dict(
        version=3,
        user_id=scope["native_user_id"],
        authentication_method="scm_mapping",
        provenance=dict(
            provider="github",
            transport="github_rest_poll_v1",
            api_origin="https://api.github.com",
            installation_id="34",
            remote_repository_id="42",
            external_actor_id="7",
            binding_request_id="a" * 32,
            binding_digest=scope["binding_digest"],
            verified_receipt_id="b" * 32,
            connection_revision="c" * 32,
        ),
    )
    scope["run"]["worker_response"]["responding_actor"] = actor
    for entry in scope["entries"]:
        if entry["kind"] == "worker_response":
            entry["payload"]["responding_actor"] = copy.deepcopy(actor)
    previous = "0" * 64
    for entry in scope["entries"]:
        entry["previous_hash"] = previous
        entry["digest"] = digest({k: v for k, v in entry.items() if k != "digest"})
        previous = entry["digest"]
    return scope


async def test_polling_answer_only_has_a_cut_under_transport_specific_resume(answered_cut):  # noqa: F811
    module = importlib.import_module("firefly_forge.scm_resume_cut")
    scope = polling_answer(answered_cut)
    with pytest.raises(ValueError, match="attributed"):
        module.resume_cut(**scope)
    cut = module.polling_resume_cut(**scope)
    assert cut["question_id"] == "question-1" and cut["binding_digest"] == scope["binding_digest"]
    scope["binding_digest"] = "f" * 64
    with pytest.raises(ValueError, match="delegation"):
        module.polling_resume_cut(**scope)


def polling_grant():
    return {**grant(), "version": 3, "transport": "github_rest_poll_v1"}


def test_transport_specific_resume_grant_is_not_an_old_signed_or_polling_consent():
    module = importlib.import_module("firefly_forge.scm_poll_resume_models")
    body = polling_grant()
    assert module.SCMPollResumeGrantInputV3.model_validate(body).model_dump(mode="json") == body
    for old in (SCMResumeGrantInputV2, SCMPollActorBindingInputV2):
        with pytest.raises(ValidationError):
            old.model_validate(body)


@pytest.mark.parametrize(
    "changes",
    [
        {"version": 2},
        {"transport": "signed_webhook_v1"},
        {"transport": None},
        {"acknowledge_same_run_resume": False},
        {"acknowledge_same_run_resume": 1},
        {"allowed_actions": ["respond"]},
        {"allowed_actions": ["publish"]},
        {"component_ids": ["api", "api"]},
        {"agent": "codex"},
        {"repair_budget": 3},
    ],
)
def test_polling_resume_cannot_acquire_implicit_or_expanded_authority(changes):
    model = importlib.import_module("firefly_forge.scm_poll_resume_models").SCMPollResumeGrantInputV3
    with pytest.raises(ValidationError):
        model.model_validate({**polling_grant(), **changes})
