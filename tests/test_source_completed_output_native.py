# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Real native SQL/broker custody; provider output is a controlled archived public stream."""

import copy
import json
from datetime import datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import text
from test_source_completed_output_service_host import request
from test_source_edit_native import started
from test_source_human_index_semantics_host import refresh_stat_cache
from test_source_human_readmission_native import answered
from test_source_human_readmission_native import request as human_request
from worker_reply_fixtures import authenticated_operator

from firefly_forge import source_result_readmission as module
from firefly_forge.identity_persistence import ForgeIdentityUserRepository
from firefly_forge.project_access import AccessDenied
from firefly_forge.source_edit_continuations import source_edit_receipt
from firefly_forge.source_human_readmission import SourceHumanReadmissions


def source_cli_transport(monkeypatch):
    import test_source_edit_native

    register = test_source_edit_native.register_fixture_profile
    from pathlib import Path

    program = Path(__file__).with_name("source_completed_output_cli_fixture.py").read_text()

    def fixture_profile(patch, executable):
        original = executable.read_text()
        prefix, _ = original.split("else:\n", 1)
        executable.write_text(prefix + "else:\n" + "\n".join(" " + line for line in program.splitlines()) + "\n")
        return register(patch, executable)

    monkeypatch.setattr(test_source_edit_native, "register_fixture_profile", fixture_profile)


async def completed(store, tmp_path, monkeypatch, *, notice=False):
    source_cli_transport(monkeypatch)
    env, parent, catalog = await answered(store, tmp_path, monkeypatch, observed=True, sanitized=True)
    refresh_stat_cache(env.workspace)
    human = SourceHumanReadmissions(env.service)
    review = await human.candidate(env.run_id, parent["request"]["request_id"], str(catalog))
    parent = await human.admit(env.run_id, parent["request"]["request_id"], human_request(review, catalog))
    key = await started(env)
    # A controlled persisted dispatch is not an invocation of a native provider.
    async with store._unit():
        row = await env.journal._run(env.run_id)
        cut = env.service.recoveries._cut(row.data)
        dispatch = await env.journal._append(
            env.run_id,
            "implement",
            key + ":provider",
            "provider_dispatch_intent",
            {"version": 1, "task_key": key, "attempt": 3},
        )
        row.data = {**row.data, "provider_dispatch": {"task_key": key, "cut": cut, "actor": parent["actor"]}}
        await store.run_records.save(row)
    summary = (
        '{"summary":"Retained source reader context was unavailable",'
        '"feedback":{"disposition":"blocked","request":null}}'
    )
    payloads = [
        {"type": "thread.started", "thread_id": "controlled-public-turn"},
        {"type": "turn.started"},
        {"type": "item.completed", "item": {"id": "final", "type": "agent_message", "text": summary}},
        {"type": "turn.completed", "usage": {"input_tokens": 100, "output_tokens": 20}},
    ]
    if notice:
        payloads[1:1] = [
            {
                "type": "item.completed",
                "item": {
                    "id": "item_0",
                    "type": "error",
                    "message": (
                        "Under-development features enabled: skip_host_skill_discovery. "
                        "Under-development features are incomplete and may behave unpredictably. "
                        "To suppress this warning, set `suppress_unstable_features_warning = true` "
                        "in /public/fixture/codex-home/config.toml."
                    ),
                },
            },
        ]
    boundary = datetime.fromisoformat(dispatch["created_at"])
    assert boundary + timedelta(microseconds=len(payloads)) < datetime.now(boundary.tzinfo)
    with monkeypatch.context() as clock:
        for ordinal, payload in enumerate(payloads, 1):
            timestamp = (boundary + timedelta(microseconds=ordinal)).isoformat()
            clock.setattr("firefly_forge.store.now", lambda timestamp=timestamp: timestamp)
            await store.event(env.run_id, "stdout", json.dumps(payload, separators=(",", ":")))
    await store.update(
        env.run_id, implementation_rounds=3, summary=summary, usage={"input_tokens": 100, "output_tokens": 20}
    )
    await store.finish(env.run_id, "failed", error="Controlled downstream feedback validation failure")
    refresh_stat_cache(env.workspace)
    return env, parent, catalog


async def snapshot(env):
    # Diagnostic SQL projection preserves the refusal of a disabled invoking identity.
    async with env.store._unit() as session:
        await session.execute(text("SET TRANSACTION READ ONLY"))
        row = await env.store.run_records.find_by_id(UUID(env.run_id))
        task = await env.journal._task(env.run_id, "implement")
        entries = await env.service.recoveries._entries(env.run_id)
        return copy.deepcopy(row.data), env.service.recoveries._task_view(task), entries


@pytest.mark.parametrize("notice", [False, True])
async def test_native_known_output_admits_only_attempt_four_and_real_source_broker_then_final_seven(
    store, tmp_path, monkeypatch, notice
):
    async with authenticated_operator(store):
        env, parent, catalog = await completed(store, tmp_path, monkeypatch, notice=notice)
        service = module.SourceCompletedOutputReadmissions(env.service)
        before, task, wal = await snapshot(env)
        index = (env.workspace / ".git/index").read_bytes()
        review = await service.candidate(env.run_id, parent["request"]["request_id"], str(catalog))
        body = request(review, str(catalog))
        append = env.journal._append

        async def prior_intent(*args, **kwargs):
            if args[3] == "intent" and args[2] != task["idempotency_key"]:
                entries = await env.service.recoveries._entries(env.run_id)
                assert entries[-1]["kind"] == "workspace_reconciliation_intent"
                assert entries[-2]["kind"] == "source_output_readmission"
                current = await env.journal._task(env.run_id, "implement")
                assert current.attempt == 3 and current.status == "running"
            return await append(*args, **kwargs)

        monkeypatch.setattr(env.journal, "_append", prior_intent)
        receipt = await service.admit(env.run_id, parent["request"]["request_id"], body)
        monkeypatch.setattr(env.journal, "_append", append)
        assert receipt == await service.get(env.run_id, parent["request"]["request_id"], body.request_id)
        assert receipt == await service.admit(env.run_id, parent["request"]["request_id"], body)
        after, queued, entries = await snapshot(env)
        assert entries[: len(wal)] == wal and queued["attempt"] == 4 and queued["status"] == "queued"
        assert (env.workspace / ".git/index").read_bytes() == index
        for key in (
            "commands",
            "sandbox_snapshot",
            "repair_budget",
            "implementation_rounds",
            "worker_response",
            "provider_dispatch",
        ):
            assert before[key] == after[key]
        assert receipt["candidate"]["plan"] == review["plan"]
        assert receipt["candidate"]["source_edit"]["remaining_commands"] == 7
        assert len(review["observation"]["events"]) == (5 if notice else 4)
        assert len(review["observation"].get("diagnostics", [])) == int(notice)
        assert receipt["candidate"]["source_edit"]["command_limit"] == 35
        assert (
            await source_edit_receipt(env.service.recoveries, await store.get_run(env.run_id), qualify=True) == receipt
        )
        env.engine.configured_provider = True
        await env.engine.execute_run(env.run_id)
        final = await store.get_run(env.run_id)
        assert final["status"] == "succeeded", final.get("error")
        assert final["attempt"] == 4 and final["implementation_rounds"] == 4
        rows = await env.engine.tool_broker.invocations(env.run_id)
        assert len(rows) == 3 and all(row["status"] == "succeeded" for row in rows)
        assert [row["admission"]["tool"] for row in rows] == ["list_files", "read_file", "search_text"]
        assert len(env.checks) == 14 and len(await env.journal.sandbox_executions(env.run_id)) == 14


@pytest.mark.parametrize("outcome", ["revoked", "source_cas", "expired", "receipt_rollback", "output_drift"])
async def test_native_known_output_refusal_rolls_back_every_new_record_and_queue(store, tmp_path, monkeypatch, outcome):
    async with authenticated_operator(store) as actor:
        env, parent, catalog = await completed(store, tmp_path, monkeypatch)
        service = module.SourceCompletedOutputReadmissions(env.service)
        review = await service.candidate(env.run_id, parent["request"]["request_id"], str(catalog))
        body = request(review, str(catalog))
        before = await snapshot(env)
        if outcome in {"revoked", "source_cas", "output_drift"}:
            admission = store.vault_source_patch_admission

            async def changed(patch):
                if admission is not None:
                    await admission(patch)
                if outcome == "revoked":
                    async with store._unit():
                        user = await ForgeIdentityUserRepository().find_by_id(actor.id)
                        user.enabled = False
                        await ForgeIdentityUserRepository().save(user)
                elif outcome == "source_cas":
                    (env.workspace / "partial.py").write_text("UNREVIEWED=1\n")
                else:
                    await store.update(env.run_id, summary="Different retained native output")

            monkeypatch.setattr(store, "vault_source_patch_admission", changed)
        else:
            append = env.journal._append

            async def changed_append(*args, **kwargs):
                value = await append(*args, **kwargs)
                if args[3] == "workspace_reconciliation_receipt":
                    if outcome == "receipt_rollback":
                        raise OSError("Controlled receipt rollback")
                    monkeypatch.setattr(module, "clock_ms", lambda: value["payload"]["expires_at_ms"])
                return value

            monkeypatch.setattr(env.journal, "_append", changed_append)
        with pytest.raises((ValueError, AccessDenied, OSError)):
            await service.admit(env.run_id, parent["request"]["request_id"], body)
        after = await snapshot(env)
        expected = copy.deepcopy(before)
        if outcome == "output_drift":
            expected[0]["summary"] = "Different retained native output"
            expected[0]["updated_at"] = after[0]["updated_at"]
        assert after == expected
        assert after[1]["attempt"] == 3 and after[1]["status"] == "running"
        assert len(env.checks) == 7


def test_native_api_completed_output_requires_exact_consent_and_returns_durable_receipt(
    tmp_path, postgres_schema, monkeypatch
):
    from starlette.testclient import TestClient

    from firefly_forge.api import create_application
    from firefly_forge.engine import Engine
    from firefly_forge.store import Store

    token = "public-completed-output-api-fixture"
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
                    env, parent, catalog = await completed(seed_store, tmp_path / "fixture", monkeypatch)
                    engine.workspaces = env.engine.workspaces
                    values = env.run_id, parent["request"]["request_id"], str(catalog)
            finally:
                await seed_store.close()
            await api_store.acquire_ownership()
            return values

        (tmp_path / "fixture").mkdir()
        run_id, parent, path = http.portal.call(seed)
        endpoint = f"/api/v1/runs/{run_id}/source-edit-continuations/{parent}"
        candidate = http.get(endpoint + "/completed-output-candidate", params={"previous_catalog_path": path})
        assert candidate.status_code == 200, candidate.text
        body = request(candidate.json(), path).model_dump()
        assert (
            http.post(endpoint + "/completed-output-readmissions", json=body | {"command_limit": 99}).status_code == 422
        )
        assert (
            http.post(
                endpoint + "/completed-output-readmissions", json=body | {"acknowledge_no_exit_attestation": False}
            ).status_code
            == 422
        )
        response = http.post(endpoint + "/completed-output-readmissions", json=body)
        assert response.status_code == 201, response.text
        assert http.get(endpoint + "/completed-output-readmissions/" + body["request_id"]).json() == response.json()
        assert http.post(endpoint + "/completed-output-readmissions", json=body).json() == response.json()
        assert http.get(f"/api/v1/runs/{run_id}").json()["status"] == "queued"
