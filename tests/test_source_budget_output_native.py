# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Real SQL, owned admission and Engine/MCP custody with a controlled CLI; no paid model."""

import copy
import hashlib
import json
import os
from dataclasses import replace
from pathlib import Path

import pytest
from sqlalchemy import text
from test_source_budget_native import answered_thirteen, retained_tool_rows, review_allowance
from test_source_budget_output_host import proposal, request
from test_source_completed_output_native import snapshot
from worker_reply_fixtures import authenticated_operator

from firefly_forge import source_budget_allowances as module
from firefly_forge.source_edit_qualification import profile_contract_digest


def controlled_output_cli(monkeypatch):
    import test_source_budget_native
    import test_source_completed_output_native
    import test_source_edit_native

    from firefly_forge import source_edit_qualification

    register = test_source_edit_native.register_fixture_profile
    program = Path(__file__).with_name("source_budget_output_cli_fixture.py").read_text()

    def budget_cli(patch):
        def registered(inner, executable):
            prefix, _ = executable.read_text().split("else:\n", 1)
            executable.write_text(prefix + "else:\n" + "\n".join(" " + line for line in program.splitlines()) + "\n")
            profile = register(inner, executable)
            limits = (("read_file_lifetime_limit", 20), ("activation_ceiling", 6))
            profile = replace(profile, source_budget_limits=limits, proof=replace(profile.proof, budget_limits=limits))
            profile = replace(profile, proof=replace(profile.proof, contract_digest=profile_contract_digest(profile)))
            inner.setattr(source_edit_qualification, "_profiles", lambda: (profile,))
            return profile

        patch.setattr(test_source_edit_native, "register_fixture_profile", registered)
        patch.setattr(test_source_completed_output_native, "source_cli_transport", lambda inner: None)

    def witness(kind, values):
        path = Path(os.environ["NATIVE_SQL_FIXTURE"]) / "logs/source-budget-output-witnesses.jsonl"
        assert path.parent.is_dir() and not path.is_symlink()
        value = {
            "version": 1,
            "case": os.environ["PYTEST_CURRENT_TEST"].removesuffix(" (call)"),
            "source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "cli_sha256": hashlib.sha256(program.encode()).hexdigest(),
            "kind": kind,
            "observed": values,
        }
        with path.open("a") as stream:
            stream.write(json.dumps(value, sort_keys=True) + "\n")

    monkeypatch.setattr(test_source_budget_native, "budget_cli", budget_cli)
    monkeypatch.setattr(test_source_budget_native, "measured_witness", witness)
    return witness


async def failed_fifth(store, tmp_path, monkeypatch):
    witness = controlled_output_cli(monkeypatch)
    env, parent, catalog, original = await answered_thirteen(store, tmp_path, monkeypatch)
    service, review, body = await review_allowance(env, parent, catalog)
    parent = await service.admit(env.run_id, parent["request"]["request_id"], body)
    env.engine.configured_provider = True
    await env.engine.execute_run(env.run_id)
    current = await store.get_run(env.run_id)
    history = await env.engine.tool_broker.invocations(env.run_id)
    assert current["status"] == "failed" and current["implementation_rounds"] == 5
    assert current["provider_recovery"]["status"] == "consumed" and current["provider_recovery"]["attempt"] == 4
    assert "typed feedback contract" in current["error"]
    assert json.loads(current["summary"])["feedback"]["request"] is None
    assert history[:13] == original and len(history) == 24
    assert sum(row["admission"]["tool"] == "read_file" for row in history) == 19
    assert len(env.checks) == 7 and (env.workspace / "docs/CONTINUATION_STATUS.md").is_file()
    module.write_policy(
        store.configuration,
        module.SourceBudgetPolicy(
            enabled=True, max_read_file_lifetime_limit=20, max_activation_ceiling=6, max_additional_activations=2
        ),
    )
    return env, parent, catalog, history, witness


async def test_native_known_output_failed5_explicit6_keeps24_receipts_then_real_mcp_final7(
    store, tmp_path, monkeypatch
):
    async with authenticated_operator(store):
        env, parent, catalog, history, witness = await failed_fifth(store, tmp_path, monkeypatch)
        service = module.SourceBudgetReadmissions(env.service)
        review = await service.candidate(env.run_id, parent["request"]["request_id"], str(catalog), proposal())
        before, _, prefix = await snapshot(env)
        index = (env.workspace / ".git/index").read_bytes()
        assert review["basis"] == "known_completed_output"
        assert review["observation"]["implementation_success"] is False
        assert review["observation"]["process_exit_attested"] is False
        assert "docs/CONTINUATION_STATUS.md" in review["source_review"]["untracked"]
        append = env.journal._append
        pids = []

        async def witnessed(*args, **kwargs):
            if args[3] == "workspace_reconciliation_intent":
                async with store._unit() as session:
                    pid = await session.scalar(text("SELECT pg_backend_pid()"))
                    assert pid == store._ownership_pid
                    pids.append(pid)
            return await append(*args, **kwargs)

        monkeypatch.setattr(env.journal, "_append", witnessed)
        body = request(review, str(catalog))
        receipt = await service.admit(env.run_id, parent["request"]["request_id"], body)
        assert len(pids) == 1 and receipt == await service.get(
            env.run_id, parent["request"]["request_id"], body.request_id
        )
        assert receipt == await service.admit(env.run_id, parent["request"]["request_id"], body)
        queued, task, entries = await snapshot(env)
        assert entries[: len(prefix)] == prefix and task["attempt"] == 6 and task["status"] == "queued"
        assert queued["provider_recovery"] == before["provider_recovery"]
        assert queued["implementation_rounds"] == 5 and queued["summary"] == before["summary"]
        assert (env.workspace / ".git/index").read_bytes() == index
        assert not any(
            e["kind"] in {"resume", "provider_dispatch_intent", "worker_feedback", "worker_response"}
            for e in entries[len(prefix) :]
        )
        await env.engine.execute_run(env.run_id)
        final = await store.get_run(env.run_id)
        rows = await env.engine.tool_broker.invocations(env.run_id)
        assert final["status"] == "succeeded", final.get("error")
        assert final["implementation_rounds"] == 6 and final["attempt"] == 6
        assert rows[:24] == history and len(rows) == 30
        assert sum(row["admission"]["tool"] == "read_file" for row in rows) == 20
        assert len(env.checks) == 14 and len(await env.journal.sandbox_executions(env.run_id)) == 14
        assert final["provider_recovery"] == before["provider_recovery"] and final["commands"] == before["commands"]
        assert final["sandbox_snapshot"]["configuration"]["command_limit"] == 35
        witness(
            "known_completed_output",
            {
                "failed_attempt": 5,
                "new_activation": 6,
                "prior24_sha256": module.digest(history),
                "retained24_sha256": module.digest(rows[:24]),
                "read_used_before": 19,
                "read_used_after": 20,
                "shared_used_before": 24,
                "shared_used_after": 30,
                "native_final_checks_added": len(env.checks) - 7,
                "original_command_limit": 35,
                "no_environment_effect": True,
                "same_owned_pid": True,
                "implementation_success_claim_in_observation": False,
                "process_exit_attested": False,
            },
        )


@pytest.mark.parametrize("outcome", ["unknown", "source_cas", "revoked", "receipt_rollback"])
async def test_native_known_output_custody_failure_never_replays_or_partially_queues(
    store, tmp_path, monkeypatch, outcome
):
    from firefly_forge.identity_persistence import ForgeIdentityUserRepository
    from firefly_forge.journal import Journal
    from firefly_forge.project_access import AccessDenied
    from firefly_forge.store import Store, identifier

    async with authenticated_operator(store) as principal:
        env, parent, catalog, history, _ = await failed_fifth(store, tmp_path, monkeypatch)
        service = module.SourceBudgetReadmissions(env.service)
        review = await service.candidate(env.run_id, parent["request"]["request_id"], str(catalog), proposal())
        body = request(review, str(catalog))
        if outcome == "unknown":
            async with store._unit():
                await env.journal._append(
                    env.run_id,
                    "implement",
                    body.request_id,
                    "workspace_reconciliation_intent",
                    {
                        "actor": parent["actor"],
                        "candidate": {
                            "source_budget_readmission": {
                                "parent_marker": {"request_id": parent["request"]["request_id"]}
                            }
                        },
                    },
                )
        before = await snapshot(env)
        if outcome in {"source_cas", "revoked"}:
            original = Store.admit_source_patch

            async def changed(current, patch):
                await original(current, patch)
                if outcome == "source_cas":
                    (env.workspace / "docs/CONTINUATION_STATUS.md").write_text("Changed after review.\n")
                else:
                    async with store._unit():
                        row = await ForgeIdentityUserRepository().find_by_id(principal.id)
                        row.enabled = False
                        await ForgeIdentityUserRepository().save(row)

            monkeypatch.setattr(Store, "admit_source_patch", changed)
        elif outcome == "receipt_rollback":
            original = env.journal._append

            async def fail(*args, **kwargs):
                if args[3] == "workspace_reconciliation_receipt":
                    raise RuntimeError("Controlled receipt persistence failure")
                return await original(*args, **kwargs)

            monkeypatch.setattr(env.journal, "_append", fail)
        with pytest.raises((ValueError, AccessDenied, RuntimeError)):
            await service.admit(env.run_id, parent["request"]["request_id"], body)
        # Suspended callers cannot read through the service; an independent read-only native unit observes rollback.
        async with store._unit() as session:
            await session.execute(text("SET TRANSACTION READ ONLY"))
            run_key = identifier(env.run_id)
            row = await store.run_records.find_by_id(run_key)
            assert row is not None
            tasks = [task for task in await env.journal.tasks.find_for_run(run_key) if task.task_id == "implement"]
            assert len(tasks) == 1
            current = tasks[0]
            task = {
                **copy.deepcopy(current.data),
                "id": current.task_id,
                "status": current.status,
                "attempt": current.attempt,
            }
            records = await env.journal.records.find_for_run(run_key)
            entries = [{**copy.deepcopy(record.data), "digest": record.digest} for record in records]
            assert (
                len(entries) <= 10000 and len(json.dumps(entries)) <= 16 * 1024 * 1024 and Journal.verify_chain(entries)
            )
            if outcome == "revoked":
                identity = await ForgeIdentityUserRepository().find_by_id(principal.id)
                assert identity is not None and identity.enabled is False
            assert row.data == before[0] and task == before[1] and entries == before[2]
        assert await retained_tool_rows(env) == history
