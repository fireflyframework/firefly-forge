# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Verification children must not rewrite product completion or their predecessor."""

import copy

from firefly_forge.github_projects_projection import project_run


def test_successful_test_child_does_not_complete_failed_product() -> None:
    parent = {"id": "1" * 32, "status": "failed", "operation": "build", "issue_context": {"number": 4}}
    before = copy.deepcopy(parent)
    child = {
        "id": "2" * 32,
        "status": "succeeded",
        "operation": "test",
        "publish": False,
        "source_checkpoint_snapshot": {"reference": {"run_id": parent["id"]}, "digest": "a" * 64},
    }
    projection = project_run(child)
    assert projection.state == "paused"
    assert projection.operation == "test"
    assert projection.delivery_scope == "checkpoint_verification"
    assert projection.source_checkpoint == "a" * 64
    assert projection.product_completion_verified is False
    assert parent == before


def test_new_implementation_progress_does_not_rewrite_parent() -> None:
    parent = {"id": "1" * 32, "status": "failed", "operation": "build"}
    before = copy.deepcopy(parent)
    child = {
        "id": "3" * 32,
        "status": "running",
        "operation": "maintain",
        "source_checkpoint_snapshot": {"digest": "b" * 64},
    }
    projection = project_run(child)
    assert projection.state == "running"
    assert projection.delivery_scope == "checkpoint_maintenance"
    assert projection.product_completion_verified is False
    assert parent == before


def test_success_without_product_completion_authority_is_not_done() -> None:
    projection = project_run(
        {"id": "4" * 32, "status": "succeeded", "operation": "build", "product_completion_verified": True}
    )
    assert projection.state == "paused"
    assert projection.product_completion_verified is False
