# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Observed-effect native consent preserves the original unknown WAL and never reapplies Git."""
# ruff: noqa: F811, F401

import copy
import os
import stat
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import NAMESPACE_URL, uuid4, uuid5

import pytest
from test_source_edit_continuations_host import controlled_metadata, source_environment, source_request
from test_workspace_dependency_reconciliation import git, legacy_workspace
from test_workspace_reconciliation_journal import environment_journal
from test_workspace_reconciliation_service import reconciliation_service

from firefly_forge import source_edit_model_catalog, workspace_reconciliations
from firefly_forge.identity import Principal
from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied
from firefly_forge.scm_source_edit_models import SealedSourceEditAdmission
from firefly_forge.source_edit_continuations import source_edit_receipt
from firefly_forge.source_edit_observed_reconciliation import (
    ObservedSourceReadmissionInput,
    ObservedSourceReconciliation,
)


async def existing_effect(env, tmp_path, monkeypatch):
    env = await source_environment(env, tmp_path, monkeypatch)
    catalog = tmp_path / "old-package/resources/native/codex-0.163.0/source-only-model.json"
    catalog.parent.mkdir(parents=True)
    catalog.write_bytes(source_edit_model_catalog.CATALOG_PATH.read_bytes())
    monkeypatch.setattr(source_edit_model_catalog, "CATALOG_PATH", catalog)
    candidate = await env.service.source_candidate(env.root.name)
    body = source_request(candidate)
    apply = workspace_reconciliations.apply_dependency_plan

    def lose_result(workspace, plan):
        apply(workspace, plan)
        raise OSError("Controlled lost completion after exact native effect")

    monkeypatch.setattr(workspace_reconciliations, "apply_dependency_plan", lose_result)
    caller = Principal(uuid4(), "fixture-issuer", "mapped-source-user", authentication_method="scm_mapping")

    async def verified():
        assert CURRENT_PRINCIPAL.get() == caller

    admission = SealedSourceEditAdmission(
        run_id=env.root.name,
        native_actor=candidate["actor"],
        caller=caller,
        request=body,
        evidence={"version": 1, "transport": "github_rest_poll_source_edit_v1", "receipt_id": "a" * 32},
        verify=verified,
    )
    token = CURRENT_PRINCIPAL.set(caller)
    try:
        with pytest.raises(OSError, match="Controlled lost completion"):
            await env.service.activate_source_edit_from_scm(admission)
    finally:
        CURRENT_PRINCIPAL.reset(token)
    original = copy.deepcopy(env.journal.records.rows[-1].data)
    assert original["kind"] == "workspace_reconciliation_intent"

    def never_replay(*args):
        raise AssertionError("Observed readmission must never repeat the environment effect")

    monkeypatch.setattr(workspace_reconciliations, "apply_dependency_plan", never_replay)
    return env, body.request_id, original, catalog


def request(review, old_path):
    return ObservedSourceReadmissionInput(
        version=1,
        request_id=uuid4().hex,
        original_intent_digest=review["original_intent_digest"],
        observed_candidate_digest=review["digest"],
        admission_contract_digest=review["admission_contract_digest"],
        previous_catalog_path=str(old_path),
        valid_for_seconds=900,
        acknowledge_observed_effect_without_replay=True,
        acknowledge_same_task_and_original_budgets=True,
        acknowledge_new_finite_activation_deadline=True,
        acknowledge_original_scm_outcome_remains_unknown=True,
    )


@pytest.mark.parametrize("relocate", [False, True])
async def test_expired_complete_effect_has_new_explicit_consent_and_standard_native_receipt(
    reconciliation_service, tmp_path, monkeypatch, relocate
):
    env, original_request, original, catalog = await existing_effect(reconciliation_service, tmp_path, monkeypatch)
    if relocate:
        successor = tmp_path / "new-package/resources/native/codex-0.163.0/source-only-model.json"
        successor.parent.mkdir(parents=True)
        successor.write_bytes(catalog.read_bytes())
        monkeypatch.setattr(source_edit_model_catalog, "CATALOG_PATH", successor)
    expired = original["payload"]["expires_at_ms"] + 1
    monkeypatch.setattr("firefly_forge.source_edit_continuations.clock_ms", lambda: expired)
    service = ObservedSourceReconciliation(env.service)
    review = await service.candidate(env.root.name, original_request, str(catalog))
    body = request(review, catalog)
    before_run = copy.deepcopy(env.store.run.data)
    index_before = (env.root / ".git/index").read_bytes()
    receipt = await service.admit(env.root.name, original_request, body)
    assert receipt["expires_at_ms"] == expired + 900000
    assert receipt["candidate"]["observed_readmission"]["original_intent_digest"] == original["digest"]
    assert "scm_source_activation" in original["payload"]
    assert "scm_source_activation" not in receipt
    assert receipt["candidate"]["source_edit"]["remaining_commands"] == 7
    assert (env.root / ".git/index").read_bytes() == index_before
    assert next(row.data for row in env.journal.records.rows if row.data["digest"] == original["digest"]) == original
    for field in (
        "attempt",
        "implementation_rounds",
        "repair_budget",
        "commands",
        "sandbox_snapshot",
        "execution_actor",
    ):
        assert env.store.run.data[field] == before_run[field]
    assert env.task.attempt == 2 and env.store.run.status == "queued"
    assert await source_edit_receipt(env.recoveries, await env.store.get_run(env.root.name), qualify=True) == receipt
    from firefly_forge.persistence import ForgeJournalEntity

    kind_bound = ForgeJournalEntity.__table__.c.kind.type.length
    assert all(len(row.data["kind"]) <= kind_bound for row in env.journal.records.rows)
    assert [row.data["kind"] for row in env.journal.records.rows][-4:] == [
        "workspace_observed_readmission",
        "workspace_reconciliation_intent",
        "workspace_reconciliation_receipt",
        "workspace_reconciliation_queued",
    ]
    with pytest.raises(ValueError, match="completion or readmission"):
        await service.admit(env.root.name, original_request, body)


async def test_hidden_staged_blob_is_not_accepted_from_unchanged_working_tree(
    reconciliation_service, tmp_path, monkeypatch
):
    env, original_request, original, catalog = await existing_effect(reconciliation_service, tmp_path, monkeypatch)
    foreign = tmp_path / "foreign-blob.txt"
    foreign.write_text("Unreviewed staged contents\n")
    object_id = git(env.root, "hash-object", "-w", str(foreign))
    git(env.root, "update-index", "--cacheinfo", "100644," + object_id + ",frontend/app.js")
    with pytest.raises(ValueError, match="staged blob"):
        await ObservedSourceReconciliation(env.service).candidate(env.root.name, original_request, str(catalog))
    assert env.store.run.status == "failed"


async def test_wrong_old_catalog_path_cannot_authorize_other_configuration(
    reconciliation_service, tmp_path, monkeypatch
):
    env, original_request, original, catalog = await existing_effect(reconciliation_service, tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="differs beyond"):
        await ObservedSourceReconciliation(env.service).candidate(
            env.root.name,
            original_request,
            str(tmp_path / "wrong/resources/native/codex-0.163.0/source-only-model.json"),
        )


async def test_any_next_attempt_dispatch_prohibits_deadline_renewal(reconciliation_service, tmp_path, monkeypatch):
    env, original_request, original, catalog = await existing_effect(reconciliation_service, tmp_path, monkeypatch)
    next_key = uuid5(NAMESPACE_URL, f"forge:{env.root.name}:implement:2").hex
    await env.journal._append(env.root.name, "implement", next_key, "intent", {"attempt": 2})
    with pytest.raises(ValueError, match="no next dispatch"):
        await ObservedSourceReconciliation(env.service).candidate(env.root.name, original_request, str(catalog))


@pytest.mark.parametrize("change", ["revocation", "source"])
async def test_credential_wait_cannot_stale_admit_current_authority_or_source(
    reconciliation_service, tmp_path, monkeypatch, change
):
    env, original_request, original, catalog = await existing_effect(reconciliation_service, tmp_path, monkeypatch)
    service = ObservedSourceReconciliation(env.service)
    review = await service.candidate(env.root.name, original_request, str(catalog))
    count = len(env.journal.records.rows)

    async def changed(_value):
        if change == "revocation":
            env.identity.enabled = False
        else:
            (env.root / "frontend/app.js").write_text("export const changed = 2;\n")

    env.store.admit_credentials = changed
    with pytest.raises((AccessDenied, ValueError)):
        await service.admit(env.root.name, original_request, request(review, catalog))
    assert len(env.journal.records.rows) == count
    assert env.store.run.status == "failed"


async def test_mapped_caller_cannot_convert_unknown_scm_outcome_to_native_consent(
    reconciliation_service, tmp_path, monkeypatch
):
    env, original_request, original, catalog = await existing_effect(reconciliation_service, tmp_path, monkeypatch)
    native = CURRENT_PRINCIPAL.get()
    token = CURRENT_PRINCIPAL.set(
        Principal(native.id, native.issuer, native.subject, authentication_method="scm_mapping")
    )
    try:
        with pytest.raises(AccessDenied, match="native factory administrator"):
            await ObservedSourceReconciliation(env.service).candidate(env.root.name, original_request, str(catalog))
    finally:
        CURRENT_PRINCIPAL.reset(token)


async def test_parent_replacement_after_manifest_refuses_before_outside_content_read(
    reconciliation_service, tmp_path, monkeypatch
):
    from firefly_forge.workspace_tools import WorkspaceTools

    env, original_request, original, catalog = await existing_effect(reconciliation_service, tmp_path, monkeypatch)
    outside = tmp_path / "outside-source"
    outside.mkdir()
    (outside / "app.js").write_text("Outside sentinel must not be read\n")
    old_parent = WorkspaceTools._parent_fd
    changed = []

    def replaced(self, parts, *, create):
        if parts[0] == "frontend" and not changed:
            (env.root / "frontend").rename(tmp_path / "original-frontend")
            (env.root / "frontend").symlink_to(outside, target_is_directory=True)
            changed.append(True)
        return old_parent(self, parts, create=create)

    monkeypatch.setattr(WorkspaceTools, "_parent_fd", replaced)
    with pytest.raises(ValueError, match="symlink or invalid directory"):
        await ObservedSourceReconciliation(env.service).candidate(env.root.name, original_request, str(catalog))
    assert changed == [True]


async def test_kernel_private_case_alias_refuses_before_staged_blob_read(reconciliation_service, tmp_path, monkeypatch):
    from firefly_forge import workspace_tools

    env, original_request, original, catalog = await existing_effect(reconciliation_service, tmp_path, monkeypatch)
    path = workspace_tools._descriptor_path
    refused = []

    def actual_spelling(fd):
        observed = path(fd)
        if stat.S_ISREG(os.fstat(fd).st_mode) and observed.name == "app.js":
            refused.append(True)
            return env.root / ".env.EXAMPLE"
        return observed

    monkeypatch.setattr(workspace_tools, "_descriptor_path", actual_spelling)
    with pytest.raises(ValueError, match="Credential files"):
        await ObservedSourceReconciliation(env.service).candidate(env.root.name, original_request, str(catalog))
    assert refused == [True]


async def test_queue_failure_rolls_back_new_consent_intent_receipt_and_run_together(
    reconciliation_service, tmp_path, monkeypatch
):
    env, original_request, original, catalog = await existing_effect(reconciliation_service, tmp_path, monkeypatch)
    service = ObservedSourceReconciliation(env.service)
    review = await service.candidate(env.root.name, original_request, str(catalog))
    before_entries = copy.deepcopy(env.journal.records.rows)
    before_run = copy.deepcopy(env.store.run.data)
    index = (env.root / ".git/index").read_bytes()
    depth = 0

    @asynccontextmanager
    async def atomic_unit(**kwargs):
        nonlocal depth
        outer = depth == 0
        saved_rows = copy.deepcopy(env.journal.records.rows) if outer else None
        saved_run = copy.deepcopy(env.store.run.data) if outer else None
        saved_status = env.store.run.status
        depth += 1
        try:
            yield None
        except BaseException:
            if outer:
                env.journal.records.rows[:] = saved_rows
                env.store.run.data = saved_run
                env.store.run.status = saved_status
            raise
        finally:
            depth -= 1

    env.store._unit = atomic_unit
    queued = env.journal.queue_environment_reconciled

    async def lost_queue(*args, **kwargs):
        await queued(*args, **kwargs)
        raise OSError("Controlled queue transaction failure")

    monkeypatch.setattr(env.journal, "queue_environment_reconciled", lost_queue)
    with pytest.raises(OSError, match="queue transaction failure"):
        await service.admit(env.root.name, original_request, request(review, catalog))
    assert [row.data for row in env.journal.records.rows] == [row.data for row in before_entries]
    assert env.store.run.data == before_run and env.store.run.status == "failed"
    assert (env.root / ".git/index").read_bytes() == index


def test_readmission_requires_exact_acknowledgements_and_finite_integer_deadline():
    body = request(
        {"original_intent_digest": "a" * 64, "digest": "b" * 64, "admission_contract_digest": "c" * 64},
        Path("/old/resources/native/codex-0.163.0/source-only-model.json"),
    )
    valid = body.model_dump()
    for name in [key for key in valid if key.startswith("acknowledge_")]:
        with pytest.raises(ValueError):
            ObservedSourceReadmissionInput.model_validate({**valid, name: 1})
    for value in (0, 1801, True, "900"):
        with pytest.raises(ValueError):
            ObservedSourceReadmissionInput.model_validate({**valid, "valid_for_seconds": value})
    with pytest.raises(ValueError):
        ObservedSourceReadmissionInput.model_validate({**valid, "version": True})
    with pytest.raises(ValueError):
        ObservedSourceReadmissionInput.model_validate({**valid, "extra": "unreviewed"})


@pytest.mark.parametrize("change", ["config", "hook", "mode", "extra_info"])
async def test_observed_exclude_does_not_mask_other_git_metadata_drift(
    reconciliation_service, tmp_path, monkeypatch, change
):
    env, original_request, original, catalog = await existing_effect(reconciliation_service, tmp_path, monkeypatch)
    git_root = env.root / ".git"
    if change == "config":
        with (git_root / "config").open("ab") as stream:
            stream.write(b"\n# Unreviewed configuration bytes\n")
    elif change == "hook":
        (git_root / "hooks/pre-commit").write_text("#!/bin/sh\nexit 0\n")
    elif change == "mode":
        path = git_root / "info/exclude"
        path.chmod(stat.S_IMODE(path.stat().st_mode) ^ 0o040)
    else:
        (git_root / "info/unreviewed.txt").write_text("Unreviewed metadata\n")
    with pytest.raises(ValueError, match="metadata changed beyond"):
        await ObservedSourceReconciliation(env.service).candidate(env.root.name, original_request, str(catalog))
    assert env.store.run.status == "failed"
