# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Elastic diff policy retains historical bytes and separates volume from RPC pages."""

import json
from pathlib import Path

import pytest

from firefly_forge.git_delivery_models import GitDeliveryPolicyV1, GitDeliveryViewV1


def test_old_policy_bytes_remain_unchanged_and_a_new_budget_can_exceed_rpc_payload_size():
    old = {"version": 1, "enabled": True, "every_units": 1, "on_pause": True, "remote": "local", "max_checkpoints": 50}
    assert GitDeliveryPolicyV1.model_validate(old).model_dump(mode="json") == old
    configured = GitDeliveryPolicyV1.model_validate({**old, "diff_budget": {"max_bytes": 40 * 1024**3}})
    assert configured.diff_budget.max_bytes == 40 * 1024**3
    assert configured.diff_budget.chunk_bytes == 65536
    assert GitDeliveryPolicyV1.model_validate({**old, "diff_budget": {"max_bytes": None}}).diff_budget.max_bytes is None


def test_checkpoint_view_keeps_large_file_counts_as_small_typed_artifact_references():
    fixture = json.loads((Path(__file__).parent.parent / "studio/tests/git-delivery-contract-fixture.json").read_text())
    row = fixture["deliveries"]["draft"]["items"][0]
    ref = {
        "version": 1,
        "artifact_id": "a" * 32,
        "manifest_sha256": "b" * 64,
        "sha256": row["patch_sha256"],
        "byte_count": 50_000_000,
        "file_count": 20_000,
        "index_page_count": 200,
    }
    view = GitDeliveryViewV1.model_validate(
        {**row, "changed_file_count": 20_000, "source_evidence": {"version": 2, "diff_artifact": ref}}
    )
    assert view.source_evidence.diff_artifact.file_count == 20_000
    assert "patch" not in view.model_dump(mode="json")
    with pytest.raises(ValueError):
        GitDeliveryViewV1.model_validate({**row, "source_evidence": {"version": 2, "diff_artifact": ref}})
