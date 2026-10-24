# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Real PostgreSQL/owner/Git custody; native provider frames are controlled fixture input."""

import copy
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from test_git_delivery_native import source_project
from test_git_failed_unit_adoption import request_from
from worker_reply_fixtures import authenticated_operator

from firefly_forge.engine import Engine
from firefly_forge.execution_requests import ExecutionRequests
from firefly_forge.git_deliveries import GitDeliveries
from firefly_forge.git_delivery_controllers import GitDeliveryController
from firefly_forge.git_delivery_models import GitDeliveryPolicyInputV1
from firefly_forge.git_delivery_policy import GitDeliveryPolicies
from firefly_forge.git_delivery_requests import GitDeliveryRequests
from firefly_forge.journal import Journal
from firefly_forge.models import RunInput
from firefly_forge.providers import AgentResult
from firefly_forge.runtime_roles import RuntimeRoles
from firefly_forge.scm import checked
from firefly_forge.store import identifier


async def failed_native(store, root):
    repository, project = await source_project(store, root)
    run = await store.create_run(
        RunInput(
            repository_id=repository["id"],
            project_id=project["id"],
            component_id="core",
            brief="Repair one public migration, retaining unfinished requirements.",
            skills=[],
        )
    )
    journal = Journal(store)

    class Worker:
        async def execute(self, workspace, *_args):
            (workspace / "product.py").write_text("value = 2\n")
            (workspace / "migration.py").write_text("def apply(): return 2\n")
            async with store._unit():
                task = await journal._task(run["id"], "implement")
                key = task.data["idempotency_key"]
                await journal._append(
                    run["id"],
                    "implement",
                    key + ":provider",
                    "provider_dispatch_intent",
                    {"version": 1, "task_key": key, "attempt": task.attempt},
                )
                row = await journal._run(run["id"])
                row.data = {**row.data, "provider_dispatch": {"task_key": key}}
                await store.run_records.save(row)
            feedback = {
                "id": "partial-native-unit",
                "role_id": "implementer",
                "task_id": "implement",
                "attempt": 1,
                "disposition": "failed",
                "summary": "The migration is repaired; remaining requirements and QA are incomplete.",
                "evidence_paths": [],
                "request": None,
            }
            summary = json.dumps({"summary": "Partial migration retained", "feedback": feedback}, separators=(",", ":"))
            usage = {"input_tokens": 10, "output_tokens": 5}
            for frame in [
                {"type": "turn.started"},
                {"type": "item.completed", "item": {"id": "final", "type": "agent_message", "text": summary}},
                {"type": "turn.completed", "usage": usage},
            ]:
                await store.event(run["id"], "stdout", json.dumps(frame, separators=(",", ":")))
            return AgentResult(summary, "controlled native-frame fixture", usage)

    engine = Engine(store, root / "workspaces", lambda _: Worker(), journal=journal)
    engine.enforce_ownership = True
    delivery = GitDeliveries(store, engine, journal)
    engine.git_deliveries = delivery
    await engine.execute_run(run["id"])
    current = await store.get_run(run["id"])
    assert current["status"] == "failed" and current["worker_feedback"]["disposition"] == "failed"
    assert not current.get("git_delivery_policy")
    await GitDeliveryPolicies(store).configure(
        project["id"],
        GitDeliveryPolicyInputV1(
            version=1,
            request_id=uuid4().hex,
            expected_previous_digest=None,
            policy={"enabled": True, "remote": "local"},
        ),
    )
    requests = ExecutionRequests(store, store.runtime_roles)
    tickets = GitDeliveryRequests(delivery, requests)
    controller = GitDeliveryController(delivery, tickets)
    return SimpleNamespace(
        run_id=run["id"],
        store=store,
        engine=engine,
        journal=journal,
        delivery=delivery,
        tickets=tickets,
        controller=controller,
        workspace=engine.workspaces / run["id"],
    )


async def witness(env):
    async with env.store._unit():
        row = await env.store.run_records.find_by_id(identifier(env.run_id))
        tasks = await env.journal.tasks.find_for_run(identifier(env.run_id))
        entries = await env.journal.records.find_for_run(identifier(env.run_id))
        return {
            "run": copy.deepcopy(row.data),
            "tasks": [(r.task_id, r.status, r.attempt, copy.deepcopy(r.data)) for r in tasks],
            "entries": [(r.kind, r.digest, copy.deepcopy(r.data)) for r in entries],
        }


@pytest.mark.parametrize("outcome", ["completed", "source_drift", "revoked", "rollback", "lost_commit_response"])
async def test_native_failed_unit_adoption_preserves_failed_run_and_effect_custody(
    store, tmp_path, monkeypatch, outcome
):
    async with authenticated_operator(store):
        runtime = RuntimeRoles(store, "combined")
        store.runtime_roles = runtime
        await runtime.start()
        await store.acquire_ownership()
        try:
            env = await failed_native(store, tmp_path)
            candidate = await env.controller.adoption_candidate(env.run_id)
            body = request_from(candidate).model_copy(update={"request_id": uuid4().hex})
            before = await witness(env)
            head = await checked(["git", "rev-parse", "HEAD"], env.workspace)
            await env.controller.request(env.run_id, body)
            if outcome == "source_drift":
                (env.workspace / "product.py").write_text("value = 99\n")
            elif outcome == "revoked":
                from firefly_forge.identity_persistence import ForgeIdentityUserRepository

                async with store._unit():
                    user = await ForgeIdentityUserRepository().find_by_id(identifier(candidate["actor"]["user_id"]))
                    user.enabled = False
                    await ForgeIdentityUserRepository().save(user)
            elif outcome == "rollback":
                append = env.delivery._append

                async def fail_after_append(run_id, kind, key, payload):
                    value = await append(run_id, kind, key, payload)
                    if kind == "git_failed_unit_adoption":
                        raise RuntimeError("Controlled rollback after native append")
                    return value

                monkeypatch.setattr(env.delivery, "_append", fail_after_append)
            elif outcome == "lost_commit_response":
                import firefly_forge.git_deliveries as module

                command = module.checked

                async def lost(argv, *args, **kwargs):
                    value = await command(argv, *args, **kwargs)
                    if "commit" in argv:
                        raise OSError("Controlled lost response after local commit")
                    return value

                monkeypatch.setattr(module, "checked", lost)
            await env.tickets.execute(body.request_id)
            after = await witness(env)
            current_head = await checked(["git", "rev-parse", "HEAD"], env.workspace)
            if outcome == "completed":
                receipt = await env.controller.ticket(env.run_id, body.request_id)
                assert (
                    receipt["status"] == "completed"
                    and receipt["result"]["verification_status"] == "partial_unverified"
                )
                assert (
                    current_head != head
                    and after["run"]["status"] == "failed"
                    and after["run"].get("git_delivery_policy") == before["run"].get("git_delivery_policy")
                )
                assert (
                    after["tasks"] == before["tasks"]
                    and after["entries"][: len(before["entries"])] == before["entries"]
                )
                assert (await env.controller.request(env.run_id, body))["status"] == "completed"
            elif outcome == "lost_commit_response":
                receipt = await env.controller.ticket(env.run_id, body.request_id)
                assert receipt["status"] == "needs_reconciliation" and current_head != head
                assert not any(kind == "git_delivery_receipt" for kind, _, _ in after["entries"])
                assert (await env.controller.request(env.run_id, body))["status"] == "needs_reconciliation"
            else:
                assert current_head == head and after == before
        finally:
            await runtime.stop()
            await store.close()
