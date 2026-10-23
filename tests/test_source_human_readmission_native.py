# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Controlled SQL/DI human readmission; no account, paid model or existing database access."""

from uuid import uuid4

import pytest
from sqlalchemy import text
from test_source_edit_native import activation, prepared, started
from worker_reply_fixtures import authenticated_operator

from firefly_forge import source_edit_model_catalog, source_human_readmission, workspace_reconciliations
from firefly_forge.agent_roles import WorkerFeedback, WorkerResponse
from firefly_forge.identity_persistence import ForgeIdentityUserRepository
from firefly_forge.project_access import AccessDenied
from firefly_forge.source_edit_continuations import SourceEditInvocation, source_edit_receipt
from firefly_forge.source_edit_models import SourceReadRequest
from firefly_forge.source_human_readmission import SourceHumanReadmissionInput, SourceHumanReadmissions


async def answered(store, tmp_path, monkeypatch, *, observed=False, sanitized=False):
    env = await prepared(store, tmp_path, monkeypatch)
    await store.update(env.run_id, repair_budget=0)
    catalog = tmp_path / "old-package/resources/native/codex-0.163.0/source-only-model.json"
    catalog.parent.mkdir(parents=True)
    catalog.write_bytes(source_edit_model_catalog.CATALOG_PATH.read_bytes())
    monkeypatch.setattr(source_edit_model_catalog, "CATALOG_PATH", catalog)
    initial = activation(await env.service.source_candidate(env.run_id))
    if observed:
        from firefly_forge.source_edit_observed_reconciliation import (
            ObservedSourceReadmissionInput,
            ObservedSourceReconciliation,
        )

        apply = workspace_reconciliations.apply_dependency_plan

        def complete_then_lost(*args):
            apply(*args)
            raise OSError("Controlled lost original environment response")

        monkeypatch.setattr(workspace_reconciliations, "apply_dependency_plan", complete_then_lost)
        with pytest.raises(OSError):
            await env.service.activate_source_edit(env.run_id, initial)
        observer = ObservedSourceReconciliation(env.service)
        if sanitized:
            from test_source_index_sanitation_native import _cache, _request

            _cache(env)
            review = await observer.index_sanitation_candidate(env.run_id, initial.request_id, str(catalog))
            await observer.sanitize_index(env.run_id, initial.request_id, _request(review, catalog))
        review = await observer.candidate(env.run_id, initial.request_id, str(catalog))
        body = ObservedSourceReadmissionInput(
            version=1,
            request_id=uuid4().hex,
            original_intent_digest=review["original_intent_digest"],
            observed_candidate_digest=review["digest"],
            admission_contract_digest=review["admission_contract_digest"],
            previous_catalog_path=str(catalog),
            valid_for_seconds=900,
            acknowledge_observed_effect_without_replay=True,
            acknowledge_same_task_and_original_budgets=True,
            acknowledge_new_finite_activation_deadline=True,
            acknowledge_original_scm_outcome_remains_unknown=True,
        )
        parent = await observer.admit(env.run_id, initial.request_id, body)
    else:
        parent = await env.service.activate_source_edit(env.run_id, initial)
    await started(env)
    await store.update(env.run_id, implementation_rounds=2)
    await env.journal.worker_feedback(
        env.run_id,
        WorkerFeedback(
            id="controlled-retained-diagnostics",
            role_id="implementer",
            task_id="implement",
            attempt=2,
            disposition="clarification",
            summary="Retained diagnostics required",
            request={
                "id": "retained-diagnostic-question",
                "kind": "other",
                "question": "What did the existing check report?",
                "context": "Use retained evidence; do not execute the check again",
            },
        ),
    )
    await env.journal.worker_response(
        env.run_id,
        "retained-diagnostic-question",
        WorkerResponse(
            id="controlled-diagnostic-answer",
            actor="Authenticated native operator",
            answer="The retained check reports a missing normalized_name column.",
        ),
    )
    return env, parent, catalog


def request(review, catalog):
    return SourceHumanReadmissionInput(
        version=1,
        request_id=uuid4().hex,
        candidate_digest=review["digest"],
        admission_contract_digest=review["admission_contract_digest"],
        previous_catalog_path=str(catalog),
        valid_for_seconds=900,
        acknowledge_same_answer_and_next_attempt=True,
        acknowledge_original_budgets=True,
        acknowledge_no_environment_replay=True,
        acknowledge_new_finite_activation=True,
    )


async def custody(env):
    async with env.store._unit() as session:
        await session.execute(text("SET TRANSACTION READ ONLY"))
        return await env.store.get_run(env.run_id), await env.service.recoveries._entries(env.run_id)


async def actual_source_readers(env, key):
    import time

    import httpx

    from firefly_forge.coding_check_transport import CodingCheckServer

    runtime = SourceEditInvocation(env.service.recoveries, env.run_id, key)
    before = await env.engine.tool_broker.invocations(env.run_id)
    listed = await runtime.run(SourceReadRequest(tool="list_files", request_id=uuid4().hex))
    assert "partial.py" in listed["files"]
    read = await runtime.run(SourceReadRequest(tool="read_file", path="partial.py", request_id=uuid4().hex))
    assert read["content"]
    found = await runtime.run(
        SourceReadRequest(tool="search_text", path="partial.py", text="VALUE", max_results=1, request_id=uuid4().hex)
    )
    assert found["matches"]
    after = await env.engine.tool_broker.invocations(env.run_id)
    assert len(after) == len(before) + 3 and all(row["status"] == "succeeded" for row in after)
    assert [row["admission"]["tool"] for row in after[len(before) :]] == ["list_files", "read_file", "search_text"]
    server = CodingCheckServer(None, source_callback=runtime.run, expires_at_ms=int(time.time() * 1000) + 30000)
    app = server.application(19435)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://127.0.0.1:19435") as client,
    ):
        denied = await client.post(
            "/mcp",
            headers={
                "authorization": "Bearer " + server.token,
                "accept": "application/json, text/event-stream",
                "mcp-protocol-version": "2025-11-25",
            },
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "run_configured_check",
                    "arguments": {"command_index": 0, "request_id": uuid4().hex},
                },
            },
        )
        assert denied.status_code == 400
    assert await env.engine.tool_broker.invocations(env.run_id) == after
    assert len(env.checks) == 7 and len(await env.journal.sandbox_executions(env.run_id)) == 7


async def test_native_fresh_human_admission_commits_new_receipt_and_only_the_original_final_seven(
    store, tmp_path, monkeypatch
):
    async with authenticated_operator(store):
        from test_source_completed_output_native import source_cli_transport
        from test_source_human_index_semantics_host import refresh_stat_cache

        source_cli_transport(monkeypatch)
        env, parent, catalog = await answered(store, tmp_path, monkeypatch, observed=True, sanitized=True)
        service = SourceHumanReadmissions(env.service)
        old, wal = await custody(env)
        refresh_stat_cache(env.workspace)
        body = request(await service.candidate(env.run_id, parent["request"]["request_id"], str(catalog)), catalog)
        index = (env.workspace / ".git/index").read_bytes()

        def no_effect(*args, **kwargs):
            raise AssertionError("Human admission must never repeat an environment effect")

        monkeypatch.setattr(workspace_reconciliations, "apply_dependency_plan", no_effect)
        resume = env.journal.resume

        async def observed(*args, **kwargs):
            async with store._unit():
                entries = await env.service.recoveries._entries(env.run_id)
                task = await env.journal._task(env.run_id, "implement")
                assert entries[-1]["kind"] == "workspace_reconciliation_intent"
                assert (
                    entries[-1]["idempotency_key"] == body.request_id and task.status == "paused" and task.attempt == 2
                )
            return await resume(*args, **kwargs)

        monkeypatch.setattr(env.journal, "resume", observed)
        receipt = await service.admit(env.run_id, parent["request"]["request_id"], body)
        assert receipt == await service.get(env.run_id, parent["request"]["request_id"], body.request_id)
        assert receipt == await service.admit(env.run_id, parent["request"]["request_id"], body)
        assert (
            await source_edit_receipt(env.service.recoveries, await store.get_run(env.run_id), qualify=True) == receipt
        )
        monkeypatch.setattr(env.journal, "resume", resume)
        await actual_source_readers(env, await started(env))
        await store.update(env.run_id, implementation_rounds=3)
        await env.journal.worker_feedback(
            env.run_id,
            WorkerFeedback(
                id="second-controlled-feedback",
                role_id="implementer",
                task_id="implement",
                attempt=3,
                disposition="clarification",
                summary="Another bounded clarification",
                request={
                    "id": "second-controlled-question",
                    "kind": "other",
                    "question": "Which retained answer applies?",
                    "context": "No additional commands",
                },
            ),
        )
        await env.journal.worker_response(
            env.run_id,
            "second-controlled-question",
            WorkerResponse(
                id="second-controlled-answer",
                actor="Native operator",
                answer="Keep the original bounded product scope.",
            ),
        )
        refresh_stat_cache(env.workspace)
        index = (env.workspace / ".git/index").read_bytes()
        second = request(await service.candidate(env.run_id, body.request_id, str(catalog)), catalog)
        receipt = await service.admit(env.run_id, body.request_id, second)
        assert receipt["candidate"]["human_readmission"]["index_semantics"]
        assert receipt == await service.get(env.run_id, body.request_id, second.request_id)
        queued, newwal = await custody(env)
        assert newwal[: len(wal)] == wal and (env.workspace / ".git/index").read_bytes() == index
        assert queued["status"] == "queued" and receipt["candidate"]["previous_task"]["attempt"] == 4
        assert receipt["candidate"]["source_edit"]["command_limit"] == 35
        assert receipt["candidate"]["source_edit"]["remaining_commands"] == 7
        for key in ("commands", "sandbox_snapshot", "repair_budget", "execution_actor"):
            assert queued[key] == old[key]
        assert await source_edit_receipt(env.service.recoveries, queued, qualify=True) == receipt
        # The Engine must claim queued attempt four and open its exclusive source channel.
        assert queued["status"] == "queued"
        env.engine.configured_provider = True
        await env.engine.execute_run(env.run_id)
        final = await store.get_run(env.run_id)
        assert final["status"] == "succeeded", final.get("error")
        assert final["attempt"] == 4 and final["implementation_rounds"] == 4
        invocations = await env.engine.tool_broker.invocations(env.run_id)
        assert len(invocations) == 6 and all(row["status"] == "succeeded" for row in invocations)
        assert [row["admission"]["tool"] for row in invocations] == ["list_files", "read_file", "search_text"] * 2
        assert len(env.checks) == 14 and len(await env.journal.sandbox_executions(env.run_id)) == 14
        assert final["sandbox_snapshot"]["configuration"]["command_limit"] == 35


@pytest.mark.parametrize("fault", ["different_blob", "private_extra", "await_refresh"])
async def test_native_semantic_index_proof_keeps_content_private_and_current_raw_index_fenced(
    store, tmp_path, monkeypatch, fault
):
    from test_source_human_index_semantics_host import refresh_stat_cache
    from test_workspace_dependency_reconciliation import git

    async with authenticated_operator(store):
        env, parent, catalog = await answered(store, tmp_path, monkeypatch, observed=True, sanitized=True)
        refresh_stat_cache(env.workspace)
        service = SourceHumanReadmissions(env.service)
        before, wal = await custody(env)
        if fault == "different_blob":
            foreign = tmp_path / "unreviewed-public-blob"
            foreign.write_text("UNREVIEWED = True\n")
            blob = git(env.workspace, "hash-object", "-w", str(foreign))
            target = git(env.workspace, "ls-files").splitlines()[0]
            git(env.workspace, "update-index", "--cacheinfo", "100644," + blob + "," + target)
        elif fault == "private_extra":
            (env.workspace / ".env").write_text("PUBLIC_FIXTURE=private-name\n")
            git(env.workspace, "add", "--force", "--", ".env")
        if fault == "await_refresh":
            body = request(await service.candidate(env.run_id, parent["request"]["request_id"], str(catalog)), catalog)
            admission = store.vault_source_patch_admission

            async def changed(patch):
                if admission is not None:
                    await admission(patch)
                refresh_stat_cache(env.workspace)

            monkeypatch.setattr(store, "vault_source_patch_admission", changed)
            with pytest.raises(ValueError, match="changed during credential admission"):
                await service.admit(env.run_id, parent["request"]["request_id"], body)
        else:
            index = (env.workspace / ".git/index").read_bytes()
            with pytest.raises(ValueError):
                await service.candidate(env.run_id, parent["request"]["request_id"], str(catalog))
            assert (env.workspace / ".git/index").read_bytes() == index
        assert await custody(env) == (before, wal)
        async with store._unit():
            task = await env.journal._task(env.run_id, "implement")
            assert task.status == "paused" and task.attempt == 2


@pytest.mark.parametrize("outcome", ["revoked", "source_cas", "expired", "receipt_rollback"])
async def test_native_human_admission_failure_never_commits_a_queue_or_replaces_original_receipt(
    store, tmp_path, monkeypatch, outcome
):
    async with authenticated_operator(store) as actor:
        env, parent, catalog = await answered(store, tmp_path, monkeypatch)
        service = SourceHumanReadmissions(env.service)
        body = request(await service.candidate(env.run_id, parent["request"]["request_id"], str(catalog)), catalog)
        before, wal = await custody(env)
        index = (env.workspace / ".git/index").read_bytes()
        if outcome in {"revoked", "source_cas"}:
            admit = store.vault_source_patch_admission

            async def changed(patch):
                if admit is not None:
                    await admit(patch)
                if outcome == "revoked":
                    async with store._unit():
                        user = await ForgeIdentityUserRepository().find_by_id(actor.id)
                        user.enabled = False
                        await ForgeIdentityUserRepository().save(user)
                else:
                    (env.workspace / "partial.py").write_text("UNREVIEWED=1\n")

            monkeypatch.setattr(store, "vault_source_patch_admission", changed)
        elif outcome == "expired":
            resume = env.journal.resume

            async def expired(*args, **kwargs):
                clock = source_human_readmission.clock_ms()
                monkeypatch.setattr(source_human_readmission, "clock_ms", lambda: clock + 901000)
                return await resume(*args, **kwargs)

            monkeypatch.setattr(env.journal, "resume", expired)
        else:
            append = env.journal._append

            async def rolled_back(*args, **kwargs):
                result = await append(*args, **kwargs)
                if args[3] == "workspace_reconciliation_receipt":
                    raise OSError("Controlled human receipt rollback")
                return result

            monkeypatch.setattr(env.journal, "_append", rolled_back)
        with pytest.raises((ValueError, AccessDenied, OSError)):
            await service.admit(env.run_id, parent["request"]["request_id"], body)
        after, entries = await custody(env)
        assert after == before and entries == wal and (env.workspace / ".git/index").read_bytes() == index
        async with store._unit():
            task = await env.journal._task(env.run_id, "implement")
            assert task.status == "paused" and task.attempt == 2
            if outcome == "revoked":
                assert not (await ForgeIdentityUserRepository().find_by_id(actor.id)).enabled
        assert len(env.calls) == 1 and len(env.checks) == 7


async def test_native_plain_human_resume_and_forged_intent_identity_cannot_reuse_source_grant(
    store, tmp_path, monkeypatch
):
    async with authenticated_operator(store):
        env, parent, catalog = await answered(store, tmp_path, monkeypatch)
        before, wal = await custody(env)
        with pytest.raises(ValueError, match="fresh native source-only human"):
            await env.journal.resume(env.run_id)
        with pytest.raises(ValueError, match="fresh native source-only human"):
            await env.journal.resume(env.run_id, source_human_readmission=uuid4().hex)
        assert await custody(env) == (before, wal)


def test_native_api_human_readmission_is_typed_authenticated_and_queues_one_exact_attempt(
    tmp_path, postgres_schema, monkeypatch
):
    from starlette.testclient import TestClient

    from firefly_forge.api import create_application
    from firefly_forge.engine import Engine
    from firefly_forge.store import Store

    token = "public-human-readmission-api-fixture"
    with TestClient(
        create_application(tmp_path / "api", token, role="api"),
        base_url="http://testserver",
        headers={"Authorization": "Bearer " + token, "Origin": "http://testserver"},
        client=("127.0.0.1", 52550),
    ) as http:
        assert http.post("/api/v1/auth/login", json={"token": token}).status_code == 200
        api_store = http.app.state.store
        engine = http.app.state.runtime.context.get_bean(Engine)

        async def seed():
            seed_store = Store(tmp_path / "seed/factory.db")
            await seed_store.initialize()
            seed_store.configuration = api_store.configuration
            try:
                async with authenticated_operator(seed_store):
                    env, parent, catalog = await answered(seed_store, tmp_path / "public-fixture", monkeypatch)
                    engine.workspaces = env.engine.workspaces
                    values = env.run_id, parent["request"]["request_id"], str(catalog)
            finally:
                await seed_store.close()
            await api_store.acquire_ownership()
            return values

        (tmp_path / "public-fixture").mkdir()
        run_id, parent, path = http.portal.call(seed)
        endpoint = f"/api/v1/runs/{run_id}/source-edit-continuations/{parent}"
        candidate = http.get(endpoint + "/human-readmission-candidate", params={"previous_catalog_path": path})
        assert candidate.status_code == 200, candidate.text
        body = request(candidate.json(), path).model_dump()
        assert http.post(endpoint + "/human-readmissions", json=body | {"command_limit": 99}).status_code == 422
        receipt = http.post(endpoint + "/human-readmissions", json=body)
        assert receipt.status_code == 201, receipt.text
        assert http.get(endpoint + "/human-readmissions/" + body["request_id"]).json() == receipt.json()
        assert http.post(endpoint + "/human-readmissions", json=body).json() == receipt.json()
        assert http.get(f"/api/v1/runs/{run_id}").json()["status"] == "queued"
