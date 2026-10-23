# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Native SQL/DI/index-only admission; coding transports are controlled public fixtures."""
# ruff: noqa: F401, F811

import asyncio
import copy
import os
import re
from concurrent.futures import ThreadPoolExecutor
from uuid import UUID, uuid4

import asyncpg
import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from test_connection_oauth_ledger import service as oauth_service
from test_source_edit_native import activation, prepared
from test_workspace_dependency_reconciliation import git
from worker_reply_fixtures import authenticated_operator

from firefly_forge import source_edit_model_catalog, source_index_sanitation, workspace_reconciliations
from firefly_forge.identity import Principal
from firefly_forge.identity_persistence import ForgeIdentityUserRepository
from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied
from firefly_forge.scm_source_edit_models import SealedSourceEditAdmission
from firefly_forge.source_edit_observed_reconciliation import ObservedSourceReconciliation
from firefly_forge.source_index_sanitation import SANITATION_INTENT, SANITATION_RECEIPT, IndexSanitationInput

CACHE = "frontend/.cache/vite/vitest/" + "a" * 40 + "/results.json"


async def _entries(env):
    async with env.store._unit():
        return await env.service.recoveries._entries(env.run_id)


async def _custody_read_only(env):
    # Diagnostic SELECTs retain the revoked principal and cannot alter application authority.
    async with env.store._unit() as session:
        await session.execute(text("SET TRANSACTION READ ONLY"))
        run = await env.store.get_run(env.run_id)
        entries = await env.service.recoveries._entries(env.run_id)
        return run, entries


async def _lost_completed_effect(store, tmp_path, monkeypatch):
    env = await prepared(store, tmp_path, monkeypatch)
    catalog = tmp_path / "old-package/resources/native/codex-0.163.0/source-only-model.json"
    catalog.parent.mkdir(parents=True)
    catalog.write_bytes(source_edit_model_catalog.CATALOG_PATH.read_bytes())
    monkeypatch.setattr(source_edit_model_catalog, "CATALOG_PATH", catalog)
    candidate = await env.service.source_candidate(env.run_id)
    request = activation(candidate)
    caller = Principal(
        uuid4(),
        "public-fixture",
        "mapped-source-user",
        authentication_method="scm_mapping",
    )

    async def verified():
        assert CURRENT_PRINCIPAL.get() == caller

    admission = SealedSourceEditAdmission(
        run_id=env.run_id,
        native_actor=candidate["actor"],
        caller=caller,
        request=request,
        evidence={
            "version": 1,
            "transport": "github_rest_poll_source_edit_v1",
            "receipt_id": "a" * 32,
        },
        verify=verified,
    )
    apply = workspace_reconciliations.apply_dependency_plan

    def completed_then_lost(workspace, plan):
        apply(workspace, plan)
        raise OSError("Controlled lost completion after exact effect")

    monkeypatch.setattr(workspace_reconciliations, "apply_dependency_plan", completed_then_lost)
    token = CURRENT_PRINCIPAL.set(caller)
    try:
        with pytest.raises(OSError, match="Controlled lost completion"):
            await env.service.activate_source_edit_from_scm(admission)
    finally:
        CURRENT_PRINCIPAL.reset(token)

    def never_replay(*args):
        raise AssertionError("Observed readmission must not repeat the environment effect")

    monkeypatch.setattr(workspace_reconciliations, "apply_dependency_plan", never_replay)
    entries = await _entries(env)
    original = next(
        e
        for e in entries
        if e["kind"] == "workspace_reconciliation_intent" and e["idempotency_key"] == request.request_id
    )
    return env, request.request_id, original, catalog


def _request(review, catalog):
    return IndexSanitationInput(
        version=1,
        request_id=uuid4().hex,
        original_intent_digest=review["original_intent_digest"],
        candidate_digest=review["digest"],
        admission_contract_digest=review["admission_contract_digest"],
        previous_catalog_path=str(catalog),
        valid_for_seconds=900,
        acknowledge_index_only_generated_artifacts=True,
        acknowledge_preserve_files_source_and_budgets=True,
        acknowledge_original_scm_outcome_unknown=True,
        acknowledge_no_queue_or_provider_dispatch=True,
    )


def _cache(env):
    cache = env.workspace / CACHE
    cache.parent.mkdir(parents=True)
    cache.write_text('{"public":"bounded-generated-result"}\n')
    git(env.workspace, "add", "--", CACHE)
    return cache


async def _committed_index_intents(schema, run_id, request_id):
    url = make_url(os.environ["FORGE_TEST_DATABASE_URL"])
    assert url.get_backend_name() == "postgresql" and (url.database or "").endswith("_test"), (
        "FORGE_TEST_DATABASE_URL must name a dedicated PostgreSQL database ending in _test"
    )
    assert re.fullmatch(r"[a-z][a-z0-9_]{0,62}", schema)
    connection = await asyncpg.connect(
        host=url.host, port=url.port, user=url.username, password=url.password, database=url.database, timeout=3
    )
    try:
        return await connection.fetchval(
            f'SELECT count(*) FROM "{schema}".forge_journal WHERE run_id=$1 AND idempotency_key=$2 AND kind=$3',
            UUID(run_id),
            request_id,
            SANITATION_INTENT,
        )
    finally:
        await connection.close()


@pytest.mark.parametrize(
    "outcome", ["completed", "lost_effect", "revoked", "source_cas", "receipt_rollback", "expired"]
)
async def test_native_index_sanitation_retains_custody_and_never_replays(store, tmp_path, monkeypatch, outcome):
    from firefly_forge import source_edit_continuations

    async with authenticated_operator(store) as actor:
        env, old_request, original, catalog = await _lost_completed_effect(store, tmp_path, monkeypatch)
        cache = _cache(env)
        physical = cache.read_bytes(), cache.stat()
        vault = oauth_service(store)
        assert store.vault_source_patch_admission == vault.admit_source_patch
        service = ObservedSourceReconciliation(env.service)
        with pytest.raises(ValueError, match="foreign or private"):
            await service.candidate(env.run_id, old_request, str(catalog))
        review = await service.index_sanitation_candidate(env.run_id, old_request, str(catalog))
        body = _request(review, catalog)
        before_run, before_entries = await _custody_read_only(env)
        before_index = (env.workspace / ".git/index").read_bytes()
        clock = [source_edit_continuations.clock_ms()]
        monkeypatch.setattr(source_edit_continuations, "clock_ms", lambda: clock[0])
        admitted = store.vault_source_patch_admission
        admission_calls = []

        async def controlled_admission(patch):
            await admitted(patch)
            admission_calls.append(patch.text)
            if outcome == "revoked" and len(admission_calls) == 2:
                async with store._unit():
                    user = await ForgeIdentityUserRepository().find_by_id(actor.id)
                    user.enabled = False
                    await ForgeIdentityUserRepository().save(user)
            if outcome == "source_cas" and len(admission_calls) == 1:
                (env.workspace / "partial.py").write_text("VALUE = 999\n")

        monkeypatch.setattr(store, "vault_source_patch_admission", controlled_admission)
        apply = source_index_sanitation.apply_sanitation
        effects = []

        def controlled_effect(workspace, plan):
            with ThreadPoolExecutor(max_workers=1) as pool:
                committed = pool.submit(
                    asyncio.run, _committed_index_intents(store.schema, env.run_id, body.request_id)
                ).result(timeout=5)
            assert committed == 1
            effects.append(plan)
            result = apply(workspace, plan)
            if outcome == "lost_effect":
                raise OSError("Controlled lost index-only completion")
            if outcome == "expired":
                clock[0] += 901000
            return result

        monkeypatch.setattr(source_index_sanitation, "apply_sanitation", controlled_effect)
        if outcome == "receipt_rollback":
            append = env.journal._append

            async def rolled_back(*args, **kwargs):
                result = await append(*args, **kwargs)
                if args[3] == SANITATION_RECEIPT:
                    raise OSError("Controlled native receipt transaction rollback")
                return result

            monkeypatch.setattr(env.journal, "_append", rolled_back)
        if outcome == "completed":
            receipt = await service.sanitize_index(env.run_id, old_request, body)
            assert receipt["no_queue_or_provider_dispatch"] is True
            assert await service.index_sanitation_get(env.run_id, old_request, body.request_id) == receipt
            assert await service.sanitize_index(env.run_id, old_request, body) == receipt
            assert len(effects) == 1 and len(admission_calls) == 2 and admission_calls[0] == admission_calls[1]
            assert (await service.candidate(env.run_id, old_request, str(catalog)))[
                "original_intent_digest"
            ] == original["digest"]
        else:
            with pytest.raises((ValueError, AccessDenied, OSError)):
                await service.sanitize_index(env.run_id, old_request, body)
            if outcome != "source_cas":
                if outcome == "revoked":
                    with pytest.raises(AccessDenied):
                        await service.index_sanitation_get(env.run_id, old_request, body.request_id)
                else:
                    held = await service.index_sanitation_get(env.run_id, old_request, body.request_id)
                    assert held["status"] == "needs_reconciliation"
                    with pytest.raises(ValueError, match="[Uu]nknown"):
                        await service.sanitize_index(env.run_id, old_request, body)
                    with pytest.raises(ValueError, match="[Uu]nknown"):
                        await service.candidate(env.run_id, old_request, str(catalog))
                assert len(effects) == 1
        after_run, after_entries = await _custody_read_only(env)
        assert after_run == before_run and after_entries[: len(before_entries)] == before_entries
        retained = after_entries[len(before_entries) :]
        expected = [] if outcome == "source_cas" else [SANITATION_INTENT]
        if outcome == "completed":
            expected.append(SANITATION_RECEIPT)
        assert [e["kind"] for e in retained] == expected
        assert cache.read_bytes() == physical[0] and cache.stat() == physical[1]
        if outcome == "source_cas":
            assert effects == [] and (env.workspace / ".git/index").read_bytes() == before_index
            assert git(env.workspace, "ls-files", "--", CACHE) == CACHE
        else:
            assert (env.workspace / ".git/index").read_bytes() != before_index
            assert git(env.workspace, "ls-files", "--", CACHE) == ""
        if outcome == "revoked":
            async with store._unit() as session:
                await session.execute(text("SET TRANSACTION READ ONLY"))
                assert not (await ForgeIdentityUserRepository().find_by_id(actor.id)).enabled
        else:
            assert (await env.service.get(env.run_id, old_request))["status"] == "needs_reconciliation"
        assert len(env.calls) == 1 and len(env.checks) == 7


def test_registered_native_api_sanitizes_only_reviewed_index_and_never_queues(tmp_path, postgres_schema, monkeypatch):
    from starlette.testclient import TestClient

    from firefly_forge.api import create_application
    from firefly_forge.engine import Engine
    from firefly_forge.store import Store

    token = "public-index-sanitation-api-fixture"
    with TestClient(
        create_application(tmp_path / "api", token, role="api"),
        base_url="http://testserver",
        headers={"Authorization": "Bearer " + token, "Origin": "http://testserver"},
        client=("127.0.0.1", 52550),
    ) as http:
        assert http.post("/api/v1/auth/login", json={"token": token}).status_code == 200
        api_store = http.app.state.store
        engine = http.app.state.runtime.context.get_bean(Engine)
        assert api_store.vault_source_patch_admission is not None

        async def seed():
            fixture_store = Store(tmp_path / "seed-native/factory.db")
            await fixture_store.initialize()
            fixture_store.configuration = api_store.configuration
            try:
                async with authenticated_operator(fixture_store):
                    (tmp_path / "seed").mkdir()
                    env, request_id, original, catalog = await _lost_completed_effect(
                        fixture_store, tmp_path / "seed", monkeypatch
                    )
                    _cache(env)
                    engine.workspaces = env.engine.workspaces
                    saved = env.run_id, request_id, str(catalog), copy.deepcopy(original)
            finally:
                await fixture_store.close()
            await api_store.acquire_ownership()
            return saved

        run_id, old_request, catalog, original = http.portal.call(seed)
        endpoint = f"/api/v1/runs/{run_id}/source-edit-continuations/{old_request}"
        assert (
            http.get(endpoint + "/observed-effect-candidate", params={"previous_catalog_path": catalog}).status_code
            == 409
        )
        response = http.get(endpoint + "/index-sanitation-candidate", params={"previous_catalog_path": catalog})
        assert response.status_code == 200, response.text
        body = _request(response.json(), catalog).model_dump()
        for invalid in (body | {"valid_for_seconds": True}, body | {"acknowledge_no_queue_or_provider_dispatch": 1}):
            assert http.post(endpoint + "/index-sanitations", json=invalid).status_code == 422
        admitted = http.post(endpoint + "/index-sanitations", json=body)
        assert admitted.status_code == 201, admitted.text
        receipt = admitted.json()
        assert receipt["no_queue_or_provider_dispatch"] is True
        assert http.get(endpoint + "/index-sanitations/" + body["request_id"]).json() == receipt
        assert http.post(endpoint + "/index-sanitations", json=body).json() == receipt
        assert (
            http.get(endpoint + "/observed-effect-candidate", params={"previous_catalog_path": catalog}).status_code
            == 200
        )
        run = http.get(f"/api/v1/runs/{run_id}").json()
        assert run["status"] == "failed" and run.get("source_edit_continuation") is None
        unknown = http.get(f"/api/v1/runs/{run_id}/source-edit-continuations/{old_request}").json()
        assert unknown["status"] == "needs_reconciliation" and unknown["intent_digest"] == original["digest"]
