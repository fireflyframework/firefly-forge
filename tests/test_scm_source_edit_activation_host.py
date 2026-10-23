# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""A separate SCM trigger preserves the sealed native operator and original task."""

# ruff: noqa: F811, F401
import copy
from uuid import uuid4

import pytest
from test_source_edit_continuations_host import controlled_metadata, source_environment, source_request
from test_workspace_dependency_reconciliation import legacy_workspace
from test_workspace_reconciliation_journal import environment_journal
from test_workspace_reconciliation_service import reconciliation_service

from firefly_forge.identity import Principal
from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied


async def prepare(env, tmp_path, monkeypatch):
    env = await source_environment(env, tmp_path, monkeypatch)
    candidate = await env.service.source_candidate(env.root.name)
    from firefly_forge.scm_source_edit_models import SealedSourceEditAdmission

    native = CURRENT_PRINCIPAL.get()
    caller = Principal(
        uuid4(),
        "https://public-fixture.invalid",
        "mapped-user",
        authentication_method="scm_mapping",
        scm_delegation={"transport": "github_rest_poll_source_edit_v1"},
    )
    state = {"valid": True, "checks": 0}

    async def verify():
        assert CURRENT_PRINCIPAL.get() == caller
        state["checks"] += 1
        if not state["valid"]:
            raise AccessDenied("Source-only grant revoked")

    evidence = dict(
        version=1,
        transport="github_rest_poll_source_edit_v1",
        receipt_id="a" * 32,
        grant_digest="b" * 64,
        binding_digest="c" * 64,
    )
    admission = SealedSourceEditAdmission(
        run_id=env.root.name,
        native_actor=copy.deepcopy(candidate["actor"]),
        caller=caller,
        request=source_request(candidate),
        evidence=evidence,
        verify=verify,
    )
    return env, candidate, native, caller, state, admission


async def test_source_trigger_retains_native_seal_and_scm_provenance_without_principal_impersonation(
    reconciliation_service, tmp_path, monkeypatch
):
    env, candidate, native, caller, state, admission = await prepare(reconciliation_service, tmp_path, monkeypatch)
    before = copy.deepcopy(env.store.run.data)
    token = CURRENT_PRINCIPAL.set(caller)
    try:
        result = await env.service.activate_source_edit_from_scm(admission)
        assert CURRENT_PRINCIPAL.get() == caller
        assert result["actor"] == candidate["actor"]
        assert result["scm_source_activation"] == admission.evidence
        assert result["candidate"] == candidate
    finally:
        CURRENT_PRINCIPAL.reset(token)
    assert state["checks"] >= 4
    assert env.task.attempt == 2
    for name in ("attempt", "implementation_rounds", "sandbox_snapshot", "commands", "execution_actor"):
        assert env.store.run.data[name] == before[name]
    assert [e.data["kind"] for e in env.journal.records.rows][-3:] == [
        "workspace_reconciliation_intent",
        "workspace_reconciliation_receipt",
        "workspace_reconciliation_queued",
    ]


async def test_source_trigger_rechecks_grant_after_admission_wait_without_intent(
    reconciliation_service, tmp_path, monkeypatch
):
    env, candidate, native, caller, state, admission = await prepare(reconciliation_service, tmp_path, monkeypatch)

    async def revoke(value):
        state["valid"] = False

    env.store.admit_credentials = revoke
    before = (env.root / ".git/index").read_bytes()
    token = CURRENT_PRINCIPAL.set(caller)
    try:
        with pytest.raises(AccessDenied, match="revoked"):
            await env.service.activate_source_edit_from_scm(admission)
    finally:
        CURRENT_PRINCIPAL.reset(token)
    assert (env.root / ".git/index").read_bytes() == before
    assert not any(e.kind == "workspace_reconciliation_intent" for e in env.journal.records.rows)


async def test_ordinary_source_api_still_refuses_scm_even_with_same_native_user_id(
    reconciliation_service, tmp_path, monkeypatch
):
    env = await source_environment(reconciliation_service, tmp_path, monkeypatch)
    body = source_request(await env.service.source_candidate(env.root.name))
    native = CURRENT_PRINCIPAL.get()
    caller = Principal(native.id, native.issuer, native.subject, authentication_method="scm_mapping")
    token = CURRENT_PRINCIPAL.set(caller)
    try:
        with pytest.raises(AccessDenied, match="administrator"):
            await env.service.activate_source_edit(env.root.name, body)
    finally:
        CURRENT_PRINCIPAL.reset(token)


async def test_source_trigger_rechecks_consent_after_candidate_wait_before_filesystem_effect(
    reconciliation_service, tmp_path, monkeypatch
):
    env, candidate, native, caller, state, admission = await prepare(reconciliation_service, tmp_path, monkeypatch)
    original = env.service._candidate

    async def revoke_after_candidate(*args, **kwargs):
        value = await original(*args, **kwargs)
        if kwargs.get("reserved") is not None:
            state["valid"] = False
        return value

    monkeypatch.setattr(env.service, "_candidate", revoke_after_candidate)
    before = (env.root / ".git/index").read_bytes()
    token = CURRENT_PRINCIPAL.set(caller)
    try:
        with pytest.raises(AccessDenied, match="revoked"):
            await env.service.activate_source_edit_from_scm(admission)
    finally:
        CURRENT_PRINCIPAL.reset(token)
    assert (env.root / ".git/index").read_bytes() == before
    assert env.store.run.status == "failed"
    assert not any(e.kind == "workspace_reconciliation_receipt" for e in env.journal.records.rows)


@pytest.mark.parametrize("prior_kind", ["native", "different_scm", "same_scm"])
async def test_prior_activation_requires_exact_scm_trigger_provenance(
    reconciliation_service, tmp_path, monkeypatch, prior_kind
):
    from dataclasses import replace

    env, candidate, native, caller, state, admission = await prepare(reconciliation_service, tmp_path, monkeypatch)
    if prior_kind == "native":
        original = await env.service.activate_source_edit(env.root.name, admission.request)
    else:
        token = CURRENT_PRINCIPAL.set(caller)
        try:
            original = await env.service.activate_source_edit_from_scm(admission)
        finally:
            CURRENT_PRINCIPAL.reset(token)
    rows = copy.deepcopy([row.data for row in env.journal.records.rows])
    requested = (
        replace(admission, evidence={**admission.evidence, "receipt_id": "d" * 32})
        if prior_kind == "different_scm"
        else admission
    )
    token = CURRENT_PRINCIPAL.set(caller)
    try:
        if prior_kind == "same_scm":
            assert await env.service.activate_source_edit_from_scm(requested) == original
        else:
            with pytest.raises(AccessDenied, match="provenance|trigger|SCM"):
                await env.service.activate_source_edit_from_scm(requested)
    finally:
        CURRENT_PRINCIPAL.reset(token)
    assert [row.data for row in env.journal.records.rows] == rows
    assert await env.service.activate_source_edit(env.root.name, admission.request) == original
