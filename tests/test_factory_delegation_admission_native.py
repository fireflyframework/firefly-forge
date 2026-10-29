# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Owned PostgreSQL admits one immutable native child per parent nonce."""

import asyncio

import pytest

from firefly_forge.delegation_contract import team_capture
from firefly_forge.delegation_models import DelegationStartInput, FactoryTeamV1, FamilyBudget
from firefly_forge.factory_delegations import FactoryDelegations
from firefly_forge.journal import Journal
from firefly_forge.models import RepositoryInput, RunInput
from firefly_forge.process import checked
from firefly_forge.project_models import ProjectDefinition
from firefly_forge.projects import Projects
from firefly_forge.sandbox_models import SandboxInput


async def seed(store, tmp_path, *, team=None):
    await store.acquire_ownership()
    source = tmp_path / "source"
    source.mkdir()
    (source / "app.py").write_text("value = 1\n")
    await checked(["git", "init", "--initial-branch=main"], source)
    await checked(["git", "add", "."], source)
    await checked(
        [
            "git",
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-m",
            "Fictional source",
        ],
        source,
    )
    repository = await store.add_repository(RepositoryInput(name="Fictional team", url=str(source)))
    selected = {}
    if team is not None:
        project = await Projects(store).configure(
            ProjectDefinition.model_validate(
                {
                    "name": "Fictional team",
                    "description": "Explicit approved family policy",
                    "topology": "monorepo",
                    "owners": ["Fixture"],
                    "components": [
                        {
                            "id": "api",
                            "name": "API",
                            "repository_id": repository["id"],
                            "responsibility": "Inspect approved source",
                        }
                    ],
                    "factory_team": team.model_dump(mode="json"),
                }
            )
        )
        selected = {"project_id": project["id"], "component_id": "api"}
    run = await store.create_run(
        RunInput(
            repository_id=repository["id"],
            agent="agentic",
            brief="Review fictional source",
            sandbox=SandboxInput(command_limit=2),
            **selected,
        )
    )
    await store.update(run["id"], factory_team=team_capture(run), status="developing", attempt=1)
    journal = Journal(store)
    intent = await journal.intent(run["id"], "implement")

    async def readiness(run):
        return {
            "status": "ready",
            "configured_framework_tools": True,
            "required_checkpoint": True,
            "shared_budget": True,
        }

    manager = FactoryDelegations(store, journal, sources=tmp_path / "origins", readiness=readiness)
    return manager.bind(run["id"], intent["idempotency_key"], source), manager, source


async def test_concurrent_exact_parent_nonce_creates_one_native_child_and_family_event(store, tmp_path):
    session, manager, source = await seed(store, tmp_path)
    (source / "untracked.txt").write_text("pending parent source\n")
    request = DelegationStartInput(request_id="a" * 32, task_id="inspect", brief="Inspect source")
    results = await asyncio.gather(*(session.start(request) for _ in range(3)))
    assert len({item["delegation_id"] for item in results}) == 1
    assert len(await store.runs()) == 2
    child = await store.get_run(results[0]["delegation_id"])
    assert child["factory_context"]["source"]["files"]["untracked.txt"]
    assert child["publish"] is False and child["status"] == "queued"
    assert child["model_selection"] == (await store.get_run(session.run_id))["model_selection"]
    assert Journal.verify_chain(await manager.journal.entries(session.run_id))
    state = await manager.ledger.state(session.run_id)
    assert state["members"] == sorted([session.run_id, child["id"]])
    assert state["charged"] == {"requests": 0, "tool_calls": 0, "commands": 0, "broker_calls": 0}


async def test_nonce_body_drift_and_native_parent_attempt_changes_refuse_without_second_child(store, tmp_path):
    session, _, _ = await seed(store, tmp_path)
    request = DelegationStartInput(request_id="a" * 32, task_id="inspect", brief="Inspect source")
    await session.start(request)
    with pytest.raises(ValueError):
        await session.start(request.model_copy(update={"brief": "Another task"}))
    await store.update(session.run_id, attempt=2)
    with pytest.raises(ValueError):
        await session.start(request.model_copy(update={"request_id": "b" * 32}))
    assert len(await store.runs()) == 2


async def test_child_admission_rolls_back_both_native_child_and_event_on_write_fault(store, tmp_path, monkeypatch):
    session, manager, _ = await seed(store, tmp_path)
    original = manager.ledger.append_locked

    async def fail_after_admit(*args, **kwargs):
        await original(*args, **kwargs)
        raise RuntimeError("controlled post-admission fault")

    monkeypatch.setattr(manager.ledger, "append_locked", fail_after_admit)
    with pytest.raises(RuntimeError, match="controlled"):
        await session.start(DelegationStartInput(request_id="a" * 32, task_id="inspect", brief="Inspect source"))
    assert len(await store.runs()) == 1
    assert not [item for item in await manager.journal.entries(session.run_id) if item["kind"] == "factory_team_event"]


async def test_missing_selected_journey_is_reported_before_origin_or_native_admission(store, tmp_path):
    session, manager, _ = await seed(store, tmp_path)

    async def unavailable(run):
        return {"status": "unavailable", "holds": ["caller_function_tools_missing"]}

    manager.readiness = unavailable
    with pytest.raises(ValueError, match="caller_function_tools_missing"):
        await session.start(DelegationStartInput(request_id="a" * 32, task_id="inspect", brief="Inspect source"))
    assert len(await store.runs()) == 1
    assert list(manager.sources.iterdir()) == []


async def test_native_parallel_command_reservations_and_dispatch_cas_share_one_original_allowance(store, tmp_path):
    session, manager, _ = await seed(store, tmp_path)
    claims = await asyncio.gather(
        *(manager.ledger.reserve(session.run_id, session.run_id, f"command:{i}", "commands") for i in range(3)),
        return_exceptions=True,
    )
    assert sum(isinstance(item, ValueError) for item in claims) == 1
    assert (await manager.ledger.state(session.run_id))["remaining"]["commands"] == 0
    first = next(item for item in claims if not isinstance(item, BaseException))
    dispatches = await asyncio.gather(
        *(manager.ledger.observe(session.run_id, first["key"], "budget_dispatched") for _ in range(2)),
        return_exceptions=True,
    )
    assert sum(isinstance(item, ValueError) for item in dispatches) == 1
    await manager.ledger.observe(session.run_id, first["key"], "budget_unknown")
    with pytest.raises(ValueError):
        await manager.ledger.observe(session.run_id, first["key"], "budget_dispatched")
    state = await manager.ledger.state(session.run_id)
    assert state["charged"]["commands"] == 2 and state["attempted"]["commands"] == 1
    assert state["unknown"]["commands"] == 1
    assert Journal.verify_chain(await manager.journal.entries(session.run_id))


@pytest.mark.parametrize("limit", [None, 2**40])
async def test_native_project_approved_nullable_and_large_budgets_survive_fresh_ledger_without_child_multiplication(
    store, tmp_path, limit
):
    team = FactoryTeamV1(
        budget=FamilyBudget(requests=limit, tool_calls=limit, commands=2, broker_calls=None), broker_per_tool_limit=2
    )
    session, manager, _ = await seed(store, tmp_path, team=team)
    child = await session.start(DelegationStartInput(request_id="a" * 32, task_id="inspect", brief="Inspect source"))
    members = [session.run_id, child["delegation_id"]]
    await asyncio.gather(
        *(manager.ledger.reserve(session.run_id, members[i % 2], f"model:{i}", "requests") for i in range(4))
    )
    from firefly_forge.delegation_ledger import FamilyLedger

    state = await FamilyLedger(store, Journal(store)).state(session.run_id)
    assert state["approved"]["requests"] == limit and state["charged"]["requests"] == 4
    assert state["remaining"]["requests"] == (None if limit is None else limit - 4)
    assert state["approved"]["broker_calls"] is None
    for i, member in enumerate(members):
        await manager.ledger.reserve(session.run_id, member, f"broker:{i}", "broker_calls", tool="external_mcp")
    with pytest.raises(ValueError):
        await manager.ledger.reserve(session.run_id, members[1], "broker:overflow", "broker_calls", tool="external_mcp")
    state = await manager.ledger.state(session.run_id)
    assert state["charged"]["broker_calls"] == 2 and state["per_tool_charged"] == {"external_mcp": 2}
