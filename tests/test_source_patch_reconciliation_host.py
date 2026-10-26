# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Real reconciliation plus real Store/OAuth admission; only SQL result transport is controlled."""

# ruff: noqa: F811

import asyncio
import copy
from contextlib import asynccontextmanager
from types import MethodType, SimpleNamespace

import pytest
from cryptography.fernet import Fernet
from test_source_edit_continuations_host import controlled_metadata, source_environment  # noqa: F401
from test_workspace_dependency_reconciliation import legacy_workspace  # noqa: F401
from test_workspace_reconciliation_journal import environment_journal  # noqa: F401
from test_workspace_reconciliation_service import reconciliation_service  # noqa: F401

from firefly_forge.connection_oauth import ConnectionOAuth
from firefly_forge.oauth_credential_fingerprints import OAuthCredentialMatcher
from firefly_forge.store import Store
from firefly_forge.verification_models import verification_source


@pytest.mark.parametrize("secret_present", [False, True])
async def test_typed_patch_is_admitted_before_any_environment_effect(
    reconciliation_service, tmp_path, monkeypatch, secret_present
):
    env = await source_environment(reconciliation_service, tmp_path, monkeypatch)
    marker = "protected-current-credential" if secret_present else "public-tail"
    (env.root / "frontend/app.js").write_text("export const value = '" + "public" * 190000 + marker + "';\n")
    entries = [copy.deepcopy(row.data) for row in env.journal.records.rows]
    entries[0]["payload"]["source_digest"] = verification_source(env.root)
    env.journal.records.rows.clear()
    for entry in entries:
        await env.journal._append(
            env.root.name, entry["task_id"], entry["idempotency_key"], entry["kind"], entry["payload"]
        )
    candidate = await env.service.source_candidate(env.root.name)
    from test_source_edit_continuations_host import source_request

    before = {name: (env.root / name).read_bytes() for name in (".git/index", ".git/info/exclude")}

    async def execute(_statement):
        return SimpleNamespace(mappings=lambda: [])

    @asynccontextmanager
    async def unit(**_kwargs):
        yield SimpleNamespace(execute=execute)

    oauth = object.__new__(ConnectionOAuth)
    oauth.store = env.store
    oauth.matcher = OAuthCredentialMatcher(Fernet.generate_key().decode(), "forge_fixture")
    oauth._secrets = {"FORGE_CURRENT_SECRET": "protected-current-credential"}
    env.store.credential_registry = lambda: {}
    env.store._unit = unit
    env.store.vault_credential_admission = oauth.admit_credentials
    env.store.vault_source_patch_admission = oauth.admit_source_patch
    env.store.admit_credentials = MethodType(Store.admit_credentials, env.store)
    if secret_present:
        with pytest.raises(ValueError, match="configured credentials"):
            await env.service.activate_source_edit(env.root.name, source_request(candidate))
        assert not any(row.data["kind"] == "workspace_reconciliation_intent" for row in env.journal.records.rows)
        assert {name: (env.root / name).read_bytes() for name in before} == before
    else:
        receipt = await env.service.activate_source_edit(env.root.name, source_request(candidate))
        assert receipt["completed_cut"]["source_digest"] == candidate["cut"]["source_digest"]
        assert env.store.run.status == "queued"
        assert len([r for r in env.journal.records.rows if r.data["kind"] == "workspace_reconciliation_intent"]) == 1


async def test_typed_preflight_cancellation_keeps_original_wal_and_git_effect_absent(
    reconciliation_service, tmp_path, monkeypatch
):
    from test_source_edit_continuations_host import source_request

    from firefly_forge.source_patch_admission import SourcePatch

    env = await source_environment(reconciliation_service, tmp_path, monkeypatch)
    candidate = await env.service.source_candidate(env.root.name)
    before_wal = [copy.deepcopy(row.data) for row in env.journal.records.rows]
    before_run = copy.deepcopy(env.store.run.data)
    before = {name: (env.root / name).read_bytes() for name in (".git/index", ".git/info/exclude")}
    admissions = []

    async def interrupted(patch):
        assert type(patch) is SourcePatch
        admissions.append(patch)
        raise asyncio.CancelledError()

    monkeypatch.setattr(env.store, "vault_source_patch_admission", interrupted, raising=False)
    with pytest.raises(asyncio.CancelledError):
        await env.service.activate_source_edit(env.root.name, source_request(candidate))
    assert len(admissions) == 1
    assert [row.data for row in env.journal.records.rows] == before_wal
    assert env.store.run.data == before_run
    assert {name: (env.root / name).read_bytes() for name in before} == before
