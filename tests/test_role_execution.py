# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
import json
import sys

import pytest
from worker_reply_fixtures import authenticated_operator

from firefly_forge.agent_roles import ProjectRoles, WorkerResponse
from firefly_forge.engine import Engine
from firefly_forge.journal import Journal
from firefly_forge.models import Command, RepositoryInput, RunInput
from firefly_forge.process import checked
from firefly_forge.providers import AgentResult


async def fixture_run(store, tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "product.py").write_text("stock = 3\n")
    await checked(["git", "init", "--initial-branch=main"], source)
    await checked(["git", "add", "."], source)
    await checked(
        ["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-m", "Fixture"],
        source,
    )
    repo = await store.add_repository(RepositoryInput(name="Stock", url=str(source)))
    run = await store.create_run(
        RunInput(
            repository_id=repo["id"],
            brief="Set the selected stock amount",
            frontend="none",
            skills=[],
            commands=[Command(argv=[sys.executable, "-c", "from product import stock; assert stock == 5"])],
            roles=ProjectRoles(enabled=["docs"]),
        )
    )
    return run


async def test_roles_are_frozen_in_the_run_plan_and_in_actual_worker_instructions(store, tmp_path):
    run = await fixture_run(store, tmp_path)
    journal = Journal(store)
    plan = await journal.ensure_plan(run["id"])
    assert plan["role_snapshot"]["digest"] == run["role_snapshot"]["digest"]
    assert plan["tasks"][1]["roles"]
    observed = []

    class Worker:
        async def execute(self, workspace, brief, *args):
            observed.append(brief)
            (workspace / "product.py").write_text("stock = 5\n")
            return AgentResult("Updated stock with tests", "fixture")

    await Engine(store, tmp_path / "workspaces", lambda _: Worker(), journal=journal).execute_run(run["id"])
    assert (await store.get_run(run["id"]))["status"] == "succeeded"
    assert "Frozen role and skill instructions" in observed[0] and "[docs; coding_provider]" in observed[0]


@pytest.mark.parametrize("repair_budget", [0, 2])
async def test_real_worker_blocker_pauses_and_requires_answer_before_explicit_resume(store, tmp_path, repair_budget):
    run = await fixture_run(store, tmp_path)
    await store.update(run["id"], repair_budget=repair_budget)
    journal = Journal(store)
    observed = []

    class Worker:
        async def execute(self, workspace, brief, *args):
            observed.append(brief)
            if len(observed) == 1:
                feedback = {
                    "id": "feedback-stock",
                    "role_id": "implementer",
                    "task_id": "implement",
                    "attempt": 1,
                    "disposition": "clarification",
                    "summary": "Stock policy needs an owner decision",
                    "evidence_paths": [],
                    "request": {
                        "id": "stock-amount",
                        "kind": "product_definition",
                        "question": "What stock amount should this fixture use?",
                        "context": "The amount was not specified",
                        "choices": ["5", "10"],
                    },
                }
                return AgentResult(json.dumps({"summary": "Owner decision required", "feedback": feedback}), "fixture")
            (workspace / "product.py").write_text("stock = 5\n")
            return AgentResult("Applied the selected stock amount", "fixture")

    engine = Engine(store, tmp_path / "workspaces", lambda _: Worker(), journal=journal)
    await engine.execute_run(run["id"])
    paused = await store.get_run(run["id"])
    assert paused["status"] == "paused" and paused["worker_request"]["status"] == "pending"
    with pytest.raises(ValueError, match="Answer"):
        await journal.resume(run["id"])
    async with authenticated_operator(store):
        await journal.worker_response(
            run["id"], "stock-amount", WorkerResponse(id="answer-stock", actor="operator", answer="Use 5")
        )
        assert (await store.get_run(run["id"]))["status"] == "paused" and len(observed) == 1
        await journal.resume(run["id"])
    journal = Journal(store)
    engine = Engine(store, tmp_path / "workspaces", lambda _: Worker(), journal=journal)
    assert await journal.human_clarification_continuation_allowance(run["id"]) == 1
    await engine.execute_run(run["id"])
    completed = await store.get_run(run["id"])
    assert completed["status"] == "succeeded" and "Use 5" in observed[1]
    assert completed["repair_budget"] == repair_budget and completed["implementation_rounds"] == 2
    assert journal.verify_chain(await journal.entries(run["id"])) is True


async def test_repository_role_defaults_persist_and_runs_freeze_their_selected_revision(store, tmp_path):
    repo = await store.add_repository(RepositoryInput(name="Stock", url=str(tmp_path)))
    await store.set_repository_roles(repo["id"], ProjectRoles(enabled=["docs"]))
    run = await store.create_run(RunInput(repository_id=repo["id"], brief="Maintain stock"))
    assert run["role_selection_source"] == "repository"
    assert next(role for role in run["role_snapshot"]["roles"] if role["id"] == "docs")["enabled"]
    await store.set_repository_roles(repo["id"], ProjectRoles(disabled=["docs"]))
    assert next(role for role in (await store.get_run(run["id"]))["role_snapshot"]["roles"] if role["id"] == "docs")[
        "enabled"
    ]
    assert (await store.repository_roles(repo["id"]))["roles"]["disabled"] == ["docs"]
    with pytest.raises(ValueError, match="mandatory"):
        await store.set_repository_roles(repo["id"], ProjectRoles(disabled=["security"]))


async def test_clarification_resume_rechecks_the_persisted_operator_before_queueing(store, tmp_path):
    from firefly_forge.agent_roles import WorkerFeedback
    from firefly_forge.identity_persistence import ForgeIdentityUserRepository
    from firefly_forge.project_access import AccessDenied

    run = await fixture_run(store, tmp_path)
    await store.update(run["id"], repair_budget=0)
    journal = Journal(store)
    await journal.intent(run["id"], "implement")
    await journal.worker_feedback(
        run["id"],
        WorkerFeedback.model_validate(
            {
                "id": "feedback-revoked",
                "role_id": "implementer",
                "task_id": "implement",
                "attempt": 1,
                "disposition": "clarification",
                "summary": "Owner decision required",
                "request": {
                    "id": "owner-revoked",
                    "kind": "product_definition",
                    "question": "Which owner?",
                    "context": "The owner is unspecified",
                },
            }
        ),
    )
    async with authenticated_operator(store) as actor:
        await journal.worker_response(
            run["id"],
            "owner-revoked",
            WorkerResponse(id="answer-revoked", actor="Fictional operator", answer="Support"),
        )
        async with store._unit():
            identities = ForgeIdentityUserRepository()
            row = await identities.find_by_id(actor.id)
            assert row is not None
            row.enabled = False
            await identities.save(row)
        with pytest.raises(AccessDenied, match="identity"):
            await journal.resume(run["id"])
    retained = await store.get_run(run["id"])
    assert retained["status"] == "paused" and retained["repair_budget"] == 0
    assert (await journal.plan(run["id"]))["tasks"][1]["attempt"] == 1
    assert await journal.human_clarification_continuation_allowance(run["id"]) == 0
