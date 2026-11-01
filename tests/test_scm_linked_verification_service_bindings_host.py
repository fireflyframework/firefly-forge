# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Rehashed foreign seals cannot substitute for one candidate and nonprivileged SCM scope."""

import copy
from types import SimpleNamespace
from uuid import UUID

import pytest
from linked_verification_fixtures import candidate, consent
from test_scm_linked_verification_models_host import admission_data

from firefly_forge.execution_requests import digest as request_digest
from firefly_forge.identity import Principal
from firefly_forge.journal import digest
from firefly_forge.linked_verification_models import LinkedVerificationCandidateV1, LinkedVerificationInputV1
from firefly_forge.linked_verifications import LinkedVerifications, _scm_binding
from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied


def binding():
    value = admission_data()
    reviewed = LinkedVerificationCandidateV1.model_validate(candidate())
    request = LinkedVerificationInputV1.model_validate(consent(reviewed.model_dump(mode="json")))
    value.update(candidate_digest=reviewed.digest, request_digest=request_digest(request.model_dump(mode="json")))
    value["scope"]["run_id"] = reviewed.run_id
    value["digest"] = digest({key: item for key, item in value.items() if key != "digest"})
    scope = value["scope"]
    actor = dict(
        user_id=scope["native_user_id"],
        authentication_method="scm_mapping",
        global_admin=False,
        api_audience=None,
        scm_delegation=copy.deepcopy(scope),
    )
    run = dict(id=reviewed.run_id, **{key: scope[key] for key in ("project_id", "repository_id", "component_id")})
    return value, actor, reviewed, request, run


@pytest.mark.parametrize("change", ["candidate", "request", "parent", "scope", "privilege"])
def test_rehashed_foreign_grant_cannot_replace_exact_reviewed_operation(change):
    value, actor, reviewed, request, run = binding()
    assert _scm_binding(value, actor, reviewed, request, run).candidate_digest == reviewed.digest
    if change == "candidate":
        value["candidate_digest"] = "0" * 64
    elif change == "request":
        value["request_digest"] = "0" * 64
    elif change == "parent":
        run["component_id"] = "foreign-component"
    elif change == "scope":
        value["scope"]["proposal_digest"] = "0" * 64
    else:
        actor["global_admin"] = True
    value["digest"] = digest({key: item for key, item in value.items() if key != "digest"})
    with pytest.raises((ValueError, AccessDenied)):
        _scm_binding(value, actor, reviewed, request, run)


async def test_mapping_without_native_verification_authority_never_becomes_an_operator():
    value, *_ = binding()
    scope = value["scope"]
    actor = Principal(
        UUID(scope["native_user_id"]),
        "fixture",
        "verified-owner",
        authentication_method="scm_mapping",
        scm_delegation=scope,
    )
    service = LinkedVerifications(SimpleNamespace(), SimpleNamespace(), SimpleNamespace(), SimpleNamespace())
    token = CURRENT_PRINCIPAL.set(actor)
    try:
        with pytest.raises(AccessDenied, match="unavailable"):
            await service._actor()
    finally:
        CURRENT_PRINCIPAL.reset(token)
