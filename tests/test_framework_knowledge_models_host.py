# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Official release selection and strict refresh input, without network or services."""

import pytest
from pydantic import ValidationError


def test_calendar_release_versions_normalize_without_accepting_prerelease_or_branch():
    from firefly_forge.framework_knowledge_models import framework_version

    assert framework_version("v26.10.06") == "26.10.6"
    assert framework_version("26.10.11") == "26.10.11"
    for value in ("main", "v26.10.11-rc1", "../main", "26.10", "26.10.11?token=secret", ""):
        with pytest.raises(ValueError):
            framework_version(value)


@pytest.mark.parametrize("field,value", [("make_current", 1), ("make_current", "true"), ("discovery_digest", "wrong")])
def test_refresh_requires_exact_reviewed_metadata_and_strict_choices(field, value):
    from firefly_forge.framework_knowledge_models import FrameworkRefreshInput

    request = {
        "version": 1,
        "discovery_digest": "a" * 64,
        "expected_status_revision": "b" * 64,
        "policy_digest": "c" * 64,
        "make_current": True,
    }
    assert FrameworkRefreshInput.model_validate(request).model_dump() == request
    with pytest.raises(ValidationError):
        FrameworkRefreshInput.model_validate({**request, field: value})
    with pytest.raises(ValidationError):
        FrameworkRefreshInput.model_validate({**request, "upstream_url": "https://private.example.test"})


def test_a_release_cannot_pair_the_wrong_official_repository_with_a_framework():
    from firefly_forge.framework_knowledge_models import FrameworkReleaseV1
    from firefly_forge.journal import digest

    data = {
        "framework": "pyfly",
        "framework_version": "26.10.11",
        "release_tag": "v26.10.11",
        "release_id": 1,
        "repository": "fireflyframework/fireflyframework-pyfly",
        "commit": "a" * 40,
        "tree": "b" * 40,
        "license_path": "LICENSE",
        "license_sha256": "c" * 64,
        "release_url": "https://github.com/fireflyframework/fireflyframework-pyfly/releases/tag/v26.10.11",
        "published_at": "2026-10-24T20:43:09Z",
        "checked_at": "2026-10-26T03:00:00Z",
    }
    assert FrameworkReleaseV1.model_validate({**data, "digest": digest(data)}).framework == "pyfly"
    data.update(
        repository="fireflyframework/fireflyframework-php",
        release_url="https://github.com/fireflyframework/fireflyframework-php/releases/tag/v26.10.11",
    )
    with pytest.raises(ValidationError):
        FrameworkReleaseV1.model_validate({**data, "digest": digest(data)})


def test_search_requires_one_explicit_version_target():
    from firefly_forge.framework_knowledge_models import FrameworkSearchInput

    for choices in ({}, {"corpus_ref": "a" * 32, "framework_version": "26.10.11"}):
        with pytest.raises(ValidationError):
            FrameworkSearchInput(query="service", **choices)
    assert FrameworkSearchInput(query="service", corpus_ref="a" * 32).corpus_ref == "a" * 32


@pytest.mark.parametrize(
    "change",
    [
        {"validation_run_id": "../private"},
        {"runtime": {"runtime": "python", "version": "latest"}},
        {"runtime": {"runtime": "shell", "version": "3.13.13"}},
        {"validation_digest": "unverified"},
        {"unexpected": True},
    ],
)
def test_generation_candidate_contract_requires_exact_native_scope_and_runtime(change):
    from firefly_forge.framework_knowledge_models import FrameworkGenerationCandidateV1

    candidate = {
        "validation_run_id": "a" * 32,
        "repository_name": "Controlled service",
        "project_id": "b" * 32,
        "component_id": "api",
        "created_at": "2026-10-26T01:00:00Z",
        "source_tree": "c" * 40,
        "runtime": {"runtime": "python", "version": "3.13.13"},
        "validation_digest": "d" * 64,
    }
    assert FrameworkGenerationCandidateV1.model_validate(candidate).model_dump() == candidate
    with pytest.raises(ValidationError):
        FrameworkGenerationCandidateV1.model_validate({**candidate, **change})
