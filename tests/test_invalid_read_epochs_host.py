# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Frozen legacy policy identities keep strict guards outside continuity's scoped migration."""

import copy
from uuid import uuid4

import pytest
from test_original_selection_readmission_host import readmission_request

pytest_plugins = ["test_original_selection_readmission_host"]


def freeze_legacy(c, *, unknown=False):
    from firefly_forge.checkpoint_mode_runtime import mode_reference
    from firefly_forge.invalid_read_epochs import LEGACY_EPOCH
    from firefly_forge.journal import digest
    from firefly_forge.original_selection_runtime import selection_reference

    rows = c.store.records.rows
    mode = next(r.data["payload"] for r in rows if r.kind == "checkpoint_mode_adoption")
    receipt = next(r.data["payload"] for r in rows if r.kind == "original_selection_readmission")
    candidate = mode["candidate"]
    candidate.update(
        policy_digest=LEGACY_EPOCH["original_mode"],
        acknowledgement_policy_digest=LEGACY_EPOCH["original_acknowledgement"],
    )
    candidate["digest"] = digest({k: v for k, v in candidate.items() if k != "digest"})
    mode["request"].update(candidate_digest=candidate["digest"], policy_digest=candidate["policy_digest"])
    previous = "0" * 64
    seen = {}
    for row in rows:
        value = row.data
        value["previous_hash"] = previous
        if row.kind == "checkpoint_mode_intent":
            value["payload"] = {
                "candidate_digest": candidate["digest"],
                "request": mode["request"],
                "actor": candidate["actor"],
            }
        elif row.kind == "checkpoint_mode_adoption":
            mode["adoption_intent_digest"] = seen["checkpoint_mode_intent"]["digest"]
        elif row.kind == "checkpoint_mode_claim":
            value["payload"]["adoption_digest"] = seen["checkpoint_mode_adoption"]["digest"]
        elif row.kind == "original_selection_intent":
            selected = receipt["candidate"]
            selected.update(
                policy_digest="0" * 64 if unknown else LEGACY_EPOCH["selection"],
                checkpoint_mode_policy_digest=LEGACY_EPOCH["mode"],
                acknowledgement_policy_digest=LEGACY_EPOCH["acknowledgement"],
                original_mode_policy_digest=LEGACY_EPOCH["original_mode"],
                original_acknowledgement_policy_digest=LEGACY_EPOCH["original_acknowledgement"],
            )
            proof = selected["proof"]
            proof.update(
                mode_receipt_digest=seen["checkpoint_mode_adoption"]["digest"],
                mode_claim_digest=seen["checkpoint_mode_claim"]["digest"],
                journal_digest=previous,
            )
            proof["digest"] = digest({k: v for k, v in proof.items() if k != "digest"})
            selected["digest"] = digest({k: v for k, v in selected.items() if k != "digest"})
            receipt["request"].update(candidate_digest=selected["digest"], policy_digest=selected["policy_digest"])
            value["payload"] = {
                "candidate_digest": selected["digest"],
                "request": receipt["request"],
                "actor": selected["actor"],
            }
        elif row.kind == "original_selection_readmission":
            receipt["readmission_intent_digest"] = seen["original_selection_intent"]["digest"]
        value["digest"] = digest({k: v for k, v in value.items() if k != "digest"})
        previous = value["digest"]
        row.digest, row.previous_hash = previous, value["previous_hash"]
        seen[row.kind] = value
    c.store.run.data["checkpoint_mode_adoption"] = mode_reference(seen["checkpoint_mode_adoption"])
    c.store.run.data["original_selection_readmission"] = selection_reference(seen["original_selection_readmission"])
    c.store.tasks.rows[0].data["checkpoint_mode_claim_digest"] = seen["checkpoint_mode_claim"]["digest"]


@pytest.fixture
async def legacy_epoch_case(selection_case, monkeypatch):
    from firefly_forge import invalid_read_continuities as native
    from firefly_forge.invalid_read_lease import ContinuityLeasePolicyV1, write_policy

    c = selection_case
    candidate = await c.selection_service.candidate(c.run_id)
    await c.selection_service.request(c.run_id, uuid4().hex, readmission_request(candidate))
    freeze_legacy(c)
    write_policy(c.store.configuration, ContinuityLeasePolicyV1(version=1, enabled=True))
    monkeypatch.setattr(
        native, "current_profile_seal", lambda: {"profile_id": "fixture-only-current-closure", "digest": "8" * 64}
    )
    return c


async def test_frozen_legacy_epoch_prepares_only_inside_explicit_continuity_scope(legacy_epoch_case):
    from firefly_forge.checkpoint_mode_runtime import assert_mode_actors, mode_adoption_scope, prepare_current_mode
    from firefly_forge.invalid_read_epochs import LEGACY_EPOCH, epoch_scope, epoch_snapshot

    c = legacy_epoch_case
    run = await c.store.get_run(c.run_id)
    entries = await c.journal.entries(c.run_id)
    before = copy.deepcopy((run, entries))
    with pytest.raises(ValueError, match="policy epoch"):
        await prepare_current_mode(c.journal, c.run_id)
    epoch = epoch_snapshot(c.store, run, entries)
    with epoch_scope(epoch):
        prepared = await prepare_current_mode(c.journal, c.run_id)
        assert prepared.policy_digest == LEGACY_EPOCH["mode"]
        assert prepared.selection.policy_digest == LEGACY_EPOCH["selection"]
        with mode_adoption_scope(prepared):
            await assert_mode_actors(c.journal, run, entries)
    with pytest.raises(ValueError, match="policy epoch"):
        await prepare_current_mode(c.journal, c.run_id)
    assert (await c.store.get_run(c.run_id), await c.journal.entries(c.run_id)) == before


@pytest.mark.parametrize("change", ["disabled", "unqualified", "unknown", "scope", "actor", "configuration"])
async def test_legacy_epoch_never_bypasses_current_guards(legacy_epoch_case, monkeypatch, change):
    from firefly_forge import invalid_read_continuities as native
    from firefly_forge.checkpoint_mode_runtime import prepare_current_mode
    from firefly_forge.invalid_read_epochs import epoch_scope, epoch_snapshot
    from firefly_forge.invalid_read_lease import ContinuityLeasePolicyV1, write_policy
    from firefly_forge.project_access import AccessDenied

    c = legacy_epoch_case
    if change == "disabled":
        write_policy(c.store.configuration, ContinuityLeasePolicyV1(version=1, enabled=False))
    elif change == "unqualified":

        def refuse():
            raise ValueError("Current closure is unqualified")

        monkeypatch.setattr(native, "current_profile_seal", refuse)
    elif change == "unknown":
        freeze_legacy(c, unknown=True)
    elif change == "scope":
        c.store.run.data["brief"] = "Changed execution scope"
    elif change == "actor":
        c.allowed = False
    else:
        from firefly_forge.checkpoint_mode_runtime import effective_revision

        revision = effective_revision(await c.store.get_run(c.run_id))
        settings = c.store.configuration.load_snapshot(revision)
        c.store.configuration._write(
            c.store.configuration.revisions / (revision + ".json"), settings | {"FORGE_BRIDGE_MODEL": "changed/model"}
        )
    with pytest.raises((ValueError, AccessDenied)):
        epoch = epoch_snapshot(c.store, await c.store.get_run(c.run_id), await c.journal.entries(c.run_id))
        with epoch_scope(epoch):
            await prepare_current_mode(c.journal, c.run_id)


async def test_policy_revocation_after_epoch_preparation_cannot_reuse_legacy_alias(legacy_epoch_case):
    from firefly_forge.checkpoint_mode_runtime import prepare_current_mode
    from firefly_forge.invalid_read_epochs import epoch_scope, epoch_snapshot
    from firefly_forge.invalid_read_lease import ContinuityLeasePolicyV1, write_policy

    c = legacy_epoch_case
    epoch = epoch_snapshot(c.store, await c.store.get_run(c.run_id), await c.journal.entries(c.run_id))
    with epoch_scope(epoch):
        write_policy(c.store.configuration, ContinuityLeasePolicyV1(version=1, enabled=False))
        with pytest.raises(ValueError):
            await prepare_current_mode(c.journal, c.run_id)
