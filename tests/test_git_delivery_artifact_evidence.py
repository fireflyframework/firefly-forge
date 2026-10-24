# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Large checkpoint evidence stays outside WAL and extends the real Git chain."""

import json

import pytest
from test_git_delivery_local import delivery_case as delivery_case
from test_git_delivery_local import git


async def test_large_checkpoint_uses_a_small_typed_artifact_reference_not_an_inline_patch(delivery_case):
    case = delivery_case()
    case.state["scanned_bytes"] = 0

    async def scanner(*, settings=None, assert_current=None):
        async def scan(chunk, final):
            await case.service.store.assert_ownership()
            if assert_current is not None:
                await assert_current()
            case.state["scanned_bytes"] += len(chunk)

        scan.finish_document = lambda: None
        return scan

    case.service.store.source_patch_scanner = scanner
    for index in range(8):
        (case.root / f"feature-{index}.txt").write_text((f"feature-{index}: " + "x" * 100 + "\n") * 6000)
    receipt = await case.service.checkpoint(
        "1" * 32, request_id="a" * 32, expected_head=case.base, actor={"user_id": "4" * 32}
    )
    artifact = receipt["source_evidence"]["diff_artifact"]
    assert artifact["byte_count"] > 4 * 1024**2 and artifact["file_count"] == 8
    assert receipt["changed_file_count"] == 8 and receipt["patch_sha256"] == artifact["sha256"]
    assert git(case.root, "rev-parse", "HEAD^") == case.base
    assert case.state["scanned_bytes"] >= artifact["byte_count"]
    assert len(json.dumps(case.state["entries"]).encode()) < 16384
    assert all("patch" not in entry["payload"] for entry in case.state["entries"])


async def test_many_files_use_exact_total_and_a_labeled_preview_without_a_project_size_cap(delivery_case):
    case = delivery_case()
    for index in range(240):
        (case.root / f"feature-{index:04d}.txt").write_text(f"Feature {index}\n")
    receipt = await case.service.checkpoint(
        "1" * 32, request_id="a" * 32, expected_head=case.base, actor={"user_id": "4" * 32}
    )
    assert receipt["changed_file_count"] == 240
    assert len(receipt["changed_files"]) == 100 and receipt["changed_files_preview_truncated"] is True
    assert receipt["source_evidence"]["diff_artifact"]["file_count"] == 240
    assert len(json.dumps(case.state["entries"]).encode()) < 16384
    assert (await case.service.view("1" * 32, actor={"user_id": "4" * 32}))["items"][0]["changed_file_count"] == 240


async def test_explicit_project_storage_budget_stops_before_git_intent_or_commit(delivery_case):
    case = delivery_case()
    case.state["run"]["git_delivery_policy"]["diff_budget"] = {"max_bytes": 1024}
    (case.root / "large.txt").write_text("A long source line\n" * 10000)
    with pytest.raises(ValueError, match="budget"):
        await case.service.checkpoint(
            "1" * 32, request_id="a" * 32, expected_head=case.base, actor={"user_id": "4" * 32}
        )
    assert git(case.root, "rev-parse", "HEAD") == case.base and not case.state["entries"]


async def test_component_validation_visits_files_after_the_first_preview_page(delivery_case):
    case = delivery_case()
    case.state["run"]["component_snapshot"] = {"path": "api"}
    folder = case.root / "api"
    folder.mkdir()
    for index in range(210):
        (folder / f"feature-{index:04d}.txt").write_text("inside\n")
    (case.root / "z-outside.txt").write_text("outside selected component\n")
    with pytest.raises(ValueError, match="component"):
        await case.service.checkpoint(
            "1" * 32, request_id="a" * 32, expected_head=case.base, actor={"user_id": "4" * 32}
        )
    assert git(case.root, "rev-parse", "HEAD") == case.base and not case.state["entries"]


async def test_artifact_receipt_cannot_be_rebound_even_with_recomputed_outer_hashes(delivery_case):
    case = delivery_case()
    (case.root / "product.py").write_text("value = 2\n")
    await case.service.checkpoint("1" * 32, request_id="a" * 32, expected_head=case.base, actor={"user_id": "4" * 32})
    entry = case.state["entries"][-1]
    payload = entry["payload"]
    payload["source_evidence"]["diff_artifact"]["artifact_id"] = "f" * 32
    payload["checkpoint_digest"] = case.module.delivery_digest(
        {key: value for key, value in payload.items() if key != "checkpoint_digest"}
    )
    entry["digest"] = case.module.delivery_digest({key: value for key, value in entry.items() if key != "digest"})
    with pytest.raises(ValueError, match="artifact.*intent"):
        await case.service.view("1" * 32, actor={"user_id": "4" * 32})


async def test_later_checkpoint_retains_only_its_parent_delta_and_preserves_original_baseline(delivery_case):
    from firefly_forge.git_diff_artifacts import read_diff_index_page
    from firefly_forge.git_diff_models import GitDiffRefV1

    case = delivery_case()
    actor = {"user_id": "4" * 32}
    (case.root / "first.txt").write_text("First completed unit\n")
    first = await case.service.checkpoint("1" * 32, request_id="a" * 32, expected_head=case.base, actor=actor)
    (case.root / "second.txt").write_text("Next completed unit\n")
    second = await case.service.checkpoint("1" * 32, request_id="b" * 32, expected_head=first["revision"], actor=actor)
    ref = GitDiffRefV1.model_validate(second["source_evidence"]["diff_artifact"])
    root = case.service.engine.workspaces.resolve() / ".diff-artifacts" / ("1" * 32)
    assert [item.path for item in read_diff_index_page(root, ref, 0).files] == ["second.txt"]
    assert second["changed_file_count"] == 1 and second["parent_revision"] == first["revision"]
    assert second["original_base_revision"] == case.base == case.state["run"]["base_revision"]


async def test_automatic_storage_budget_applies_to_each_parent_delta_not_the_product_history(delivery_case):
    case = delivery_case()
    case.state["run"]["git_delivery_policy"]["diff_budget"] = {"max_bytes": 70000}
    (case.root / "first.txt").write_text(("first " + "x" * 100 + "\n") * 500)
    first = await case.service.automatic("1" * 32)
    assert first["source_evidence"]["diff_artifact"]["byte_count"] < 70000
    (case.root / "second.txt").write_text(("second " + "y" * 100 + "\n") * 500)
    case.state["run"]["implementation_rounds"] = 2
    second = await case.service.automatic("1" * 32)
    assert second["parent_revision"] == first["revision"]
    assert second["original_base_revision"] == case.base
    assert second["changed_file_count"] == 1
    assert second["source_evidence"]["diff_artifact"]["byte_count"] < 70000
    assert await case.service.automatic("1" * 32) is None
