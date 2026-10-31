# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""A Git tree cannot substitute for the complete admitted public source cut."""

import copy
import hashlib
from uuid import UUID

import pytest
from test_runner_object_artifacts_host import allowed, authority_fence
from test_source_explorer_host import prepared

from firefly_forge.git_deliveries import GitDeliveries, delivery_digest
from firefly_forge.git_delivery_models import GitDeliveryViewV1
from firefly_forge.identity import Principal
from firefly_forge.project_access import CURRENT_PRINCIPAL


def checkpoint(reference, source_digest):
    request = {
        "request_id": "d" * 32,
        "expected_head": reference.head,
        "expected_source_tree": None,
        "actor": {"user_id": UUID(int=7).hex},
    }
    if source_digest is not None:
        request["expected_source_digest"] = source_digest
    intent = {
        **request,
        "request_digest": delivery_digest(request),
        "sequence": 1,
        "original_base_revision": reference.head,
        "parent_revision": reference.head,
        "source_tree": reference.tree,
        "patch_sha256": hashlib.sha256(b"+recorded\n").hexdigest(),
        "changed_files": ["app.py"],
    }
    receipt = {
        "version": 1,
        "run_id": reference.run_id,
        "request_id": request["request_id"],
        "sequence": 1,
        "original_base_revision": reference.head,
        "parent_revision": reference.head,
        "revision": "e" * 40,
        "source_tree": reference.tree,
        "task_id": "implement",
        "attempt": 1,
        "changed_file_count": 1,
        "changed_files": intent["changed_files"],
        "patch_sha256": intent["patch_sha256"],
        "branch": reference.branch,
        "remote_status": "not_requested",
        "created_at": "2026-10-30T00:00:00Z",
    }
    receipt["checkpoint_digest"] = delivery_digest(receipt)
    view = GitDeliveryViewV1.model_validate(
        {k: v for k, v in receipt.items() if k in GitDeliveryViewV1.model_fields}
    ).model_dump(mode="json")
    entries = [
        {
            "kind": "git_delivery_intent",
            "payload": intent,
            "sequence": 1,
            "idempotency_key": "git:" + request["request_id"],
        },
        {
            "kind": "git_delivery_receipt",
            "payload": receipt,
            "sequence": 2,
            "idempotency_key": "git:" + request["request_id"],
        },
    ]
    return entries, view


async def bind(explorer, state, entries, view):
    state.receipts = [view]

    async def retained(run_id):
        assert run_id == state.run["id"]
        return copy.deepcopy(entries)

    explorer.deliveries._entries = retained
    explorer.deliveries._records = GitDeliveries._records


@pytest.mark.parametrize("digest", [None, "f" * 64])
async def test_same_tree_without_exact_admitted_source_digest_is_not_checkpoint_provenance(tmp_path, digest):
    explorer, ref, state, _, _ = await prepared(tmp_path)
    entries, view = checkpoint(ref, digest)
    await bind(explorer, state, entries, view)
    token = CURRENT_PRINCIPAL.set(Principal(UUID(int=7), "issuer", "user"))
    try:
        with pytest.raises(ValueError, match="checkpoint"):
            await explorer.catalog(ref.run_id, ref.snapshot_id, ref.catalog.manifest_sha256, request_id="d" * 32)
    finally:
        CURRENT_PRINCIPAL.reset(token)


async def test_same_git_tree_different_ignored_public_source_does_not_relabel_checkpoint(tmp_path):
    explorer, first, state, publisher, workspace = await prepared(tmp_path)
    (workspace / ".gitignore").write_text("ignored-public.py\n")
    (workspace / "ignored-public.py").write_text("recorded = 1\n")
    first = await publisher.publish(state.run, workspace, first.head, first.tree, authority_fence(), authorize=allowed)
    entries, view = checkpoint(first, first.catalog.source_digest)
    await bind(explorer, state, entries, view)
    token = CURRENT_PRINCIPAL.set(Principal(UUID(int=7), "issuer", "user"))
    try:
        await explorer.catalog(first.run_id, first.snapshot_id, first.catalog.manifest_sha256, request_id="d" * 32)
        (workspace / "ignored-public.py").write_text("recorded = 2\n")
        second = await publisher.publish(
            state.run, workspace, first.head, first.tree, authority_fence(), authorize=allowed
        )
        assert second.tree == first.tree and second.catalog.source_digest != first.catalog.source_digest
        with pytest.raises(ValueError, match="checkpoint"):
            await explorer.catalog(
                second.run_id, second.snapshot_id, second.catalog.manifest_sha256, request_id="d" * 32
            )
    finally:
        CURRENT_PRINCIPAL.reset(token)


@pytest.mark.parametrize("mutation", ["request_digest", "foreign_run", "receipt", "duplicate", "changed_after_read"])
async def test_checkpoint_source_proof_rejects_broken_or_changed_native_custody(tmp_path, mutation):
    explorer, ref, state, _, _ = await prepared(tmp_path)
    entries, view = checkpoint(ref, ref.catalog.source_digest)
    if mutation == "request_digest":
        entries[0]["payload"]["request_digest"] = "0" * 64
    elif mutation == "foreign_run":
        entries[1]["payload"]["run_id"] = "f" * 32
        payload = entries[1]["payload"]
        payload["checkpoint_digest"] = delivery_digest({k: v for k, v in payload.items() if k != "checkpoint_digest"})
        view["checkpoint_digest"] = payload["checkpoint_digest"]
    elif mutation == "receipt":
        entries[1]["payload"]["checkpoint_digest"] = "0" * 64
    elif mutation == "duplicate":
        entries.insert(1, copy.deepcopy(entries[0]))
    await bind(explorer, state, entries, view)
    if mutation == "changed_after_read":
        retained = explorer.deliveries._entries
        calls = 0

        async def changed(run_id):
            nonlocal calls
            calls += 1
            if calls > 1:
                entries[0]["payload"]["actor"] = {"user_id": UUID(int=9).hex}
                request = {
                    key: entries[0]["payload"][key]
                    for key in (
                        "request_id",
                        "expected_head",
                        "expected_source_tree",
                        "actor",
                        "expected_source_digest",
                    )
                }
                entries[0]["payload"]["request_digest"] = delivery_digest(request)
            return await retained(run_id)

        explorer.deliveries._entries = changed
    token = CURRENT_PRINCIPAL.set(Principal(UUID(int=7), "issuer", "user"))
    try:
        with pytest.raises(ValueError):
            await explorer.catalog(ref.run_id, ref.snapshot_id, ref.catalog.manifest_sha256, request_id="d" * 32)
    finally:
        CURRENT_PRINCIPAL.reset(token)
