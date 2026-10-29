# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Offline maintenance admits explicit path projections, never ambient replacements."""

import pytest
from pydantic import ValidationError

from firefly_forge.native_location_models import (
    NativeLocationAcknowledgmentV1,
    NativeLocationPlanV1,
    VerificationRuntimeLocationV1,
)


def plan():
    return {
        "version": 1,
        "source_storage": "/retained/forge-data",
        "target_storage": "/development/forge-data",
        "workspaces": [
            {
                "run_id": "1" * 32,
                "source": "/retained/forge-data/workspaces/" + "1" * 32,
                "target": "/development/forge-data/workspaces/" + "1" * 32,
            }
        ],
        "verification_runtimes": [],
    }


def test_path_plan_keeps_exact_logical_and_physical_names():
    value = NativeLocationPlanV1.model_validate(plan())
    assert value.source_storage == "/retained/forge-data"
    assert value.target_storage == "/development/forge-data"
    assert value.workspaces[0].run_id == "1" * 32


@pytest.mark.parametrize(
    "path", ["relative", "/", "/retained/../other", "/retained//data", "/retained/data/", "/retained/data\n"]
)
def test_location_plan_refuses_ambiguous_paths(path):
    value = plan()
    value["target_storage"] = path
    with pytest.raises(ValidationError):
        NativeLocationPlanV1.model_validate(value)


@pytest.mark.parametrize(
    "change", ["same", "nested_target", "workspace_escape", "foreign_run", "duplicate_run", "version_bool", "unknown"]
)
def test_plan_refuses_namespace_and_type_confusion(change):
    value = plan()
    if change == "same":
        value["target_storage"] = value["source_storage"]
    elif change == "nested_target":
        value["target_storage"] = value["source_storage"] + "/new"
    elif change == "workspace_escape":
        value["workspaces"][0]["target"] = "/development/other"
    elif change == "foreign_run":
        value["workspaces"][0]["run_id"] = "2" * 32
    elif change == "duplicate_run":
        value["workspaces"].append(dict(value["workspaces"][0]))
    elif change == "version_bool":
        value["version"] = True
    else:
        value["allow_new_identity"] = True
    with pytest.raises(ValidationError):
        NativeLocationPlanV1.model_validate(value)


def runtime():
    return {
        "run_id": "1" * 32,
        "source": "/retained/python",
        "target": "/development/python",
        "mode": "byte_identical",
        "source_content_digest": "2" * 64,
        "target_content_digest": "2" * 64,
        "approved_baseline_sha256": None,
    }


def test_changed_verification_runtime_requires_an_explicit_distinct_baseline():
    original = runtime()
    assert VerificationRuntimeLocationV1.model_validate(original).mode == "byte_identical"
    original["target_content_digest"] = "3" * 64
    with pytest.raises(ValidationError):
        VerificationRuntimeLocationV1.model_validate(original)
    original["mode"] = "verification_runtime_baseline"
    with pytest.raises(ValidationError):
        VerificationRuntimeLocationV1.model_validate(original)
    original["approved_baseline_sha256"] = "4" * 64
    assert VerificationRuntimeLocationV1.model_validate(original).mode == "verification_runtime_baseline"


@pytest.mark.parametrize(
    "field", ["acknowledge_stopped_runtime", "acknowledge_history_preserved", "acknowledge_no_execution_grant"]
)
@pytest.mark.parametrize("coerced", [1, "true", False])
def test_offline_operator_acknowledgments_are_exact(field, coerced):
    value = {
        "version": 1,
        "request_id": "1" * 32,
        "candidate_digest": "2" * 64,
        "acknowledge_stopped_runtime": True,
        "acknowledge_history_preserved": True,
        "acknowledge_no_execution_grant": True,
    }
    value[field] = coerced
    with pytest.raises(ValidationError):
        NativeLocationAcknowledgmentV1.model_validate(value)


def test_required_file_manifest_preserves_unavailable_history_and_refuses_missing_global_inputs():
    from firefly_forge.native_location_models import NativeLocationFilesV1

    required = [
        {"path": name, "sha256": "a" * 64}
        for name in (
            "runtime-storage.identity",
            "configuration/providers.json",
            "configuration/source-budget-policy.json",
        )
    ]
    value = {
        "version": 1,
        "required_files": required,
        "unavailable_files": [{"path": "old/history.json", "sha256": None}],
    }
    assert NativeLocationFilesV1.model_validate(value).unavailable_files[0].path == "old/history.json"
    for name in ("configuration/providers.json", "configuration/source-budget-policy.json"):
        with pytest.raises(ValueError):
            NativeLocationFilesV1.model_validate(
                {**value, "required_files": [v for v in required if v["path"] != name]}
            )
    with pytest.raises(ValueError):
        NativeLocationFilesV1.model_validate({**value, "unavailable_files": [required[0]]})
    with pytest.raises(ValueError):
        NativeLocationFilesV1.model_validate({**value, "unavailable_files": [{"path": "../escape", "sha256": None}]})
