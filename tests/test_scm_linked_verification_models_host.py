# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Pure scoped SCM verification seals grant no provider or command execution."""

import importlib
import importlib.util

import pytest

from firefly_forge.journal import digest


def contracts():
    name = "firefly_forge.scm_linked_verification_models"
    assert importlib.util.find_spec(name) is not None, "SCM verification needs its own native scoped consent contract"
    return importlib.import_module(name)


def scope_data():
    return {
        "version": 1,
        "transport": "github_rest_poll_linked_verification_v1",
        "proposal_id": "a" * 32,
        "proposal_digest": "b" * 64,
        "project_id": "c" * 32,
        "repository_id": "d" * 32,
        "component_id": "inventory-api",
        "run_id": "e" * 32,
        "native_user_id": "f" * 32,
        "binding_key": "1" * 64,
        "binding_digest": "2" * 64,
        "binding_request_id": "3" * 32,
    }


def admission_data():
    value = {
        "version": 1,
        "scope": scope_data(),
        "candidate_digest": "4" * 64,
        "request_id": "a" * 32,
        "request_digest": "5" * 64,
        "review_digest": "6" * 64,
        "publication_digest": "7" * 64,
        "confirmation_digest": "8" * 64,
        "host_administrator_digest": "9" * 64,
        "grant_digest": "a" * 64,
    }
    return {**value, "digest": digest(value)}


def test_stable_scope_excludes_mutable_candidate_confirmation_and_privileges():
    model = contracts().SCMLinkedVerificationScopeV1.model_validate(scope_data())
    assert model.model_dump(mode="json") == scope_data()
    with pytest.raises(ValueError):
        model.run_id = "b" * 32
    for field in ("candidate_digest", "confirmation_receipt_id", "status", "global_admin", "api_audience"):
        with pytest.raises(ValueError):
            contracts().SCMLinkedVerificationScopeV1.model_validate(scope_data() | {field: "untrusted"})


def test_admission_seal_binds_one_original_request_and_all_authority_records():
    value = admission_data()
    model = contracts().SCMLinkedVerificationAdmissionV1.model_validate(value)
    assert model.model_dump(mode="json") == value
    assert model.request_id == model.scope.proposal_id
    changed = value | {"request_id": "b" * 32}
    changed["digest"] = digest({key: item for key, item in changed.items() if key != "digest"})
    with pytest.raises(ValueError):
        contracts().SCMLinkedVerificationAdmissionV1.model_validate(changed)


@pytest.mark.parametrize(
    "field",
    [
        "candidate_digest",
        "request_digest",
        "review_digest",
        "publication_digest",
        "confirmation_digest",
        "host_administrator_digest",
        "grant_digest",
    ],
)
def test_each_admission_record_digest_is_inside_the_native_seal(field):
    with pytest.raises(ValueError):
        contracts().SCMLinkedVerificationAdmissionV1.model_validate(admission_data() | {field: "0" * 64})


@pytest.mark.parametrize(
    "mutation", [{"version": True}, {"run_id": "../private-scope-canary"}, {"transport": "gitlab"}]
)
def test_scope_refuses_coercion_path_injection_and_unqualified_transport(mutation):
    with pytest.raises(ValueError) as failure:
        contracts().SCMLinkedVerificationScopeV1.model_validate(scope_data() | mutation)
    assert "private-scope-canary" not in str(failure.value)
