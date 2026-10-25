# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Owned native SQL execution windows; local Git and model-free verification only."""

import copy
import subprocess
import sys
from uuid import UUID, uuid4

import pytest
from test_project_revision_reference_native import setup

from firefly_forge.campaign_execution_budget import CampaignBudgetRenewalV1, CampaignExecutionPolicyInputV1
from firefly_forge.engine import Engine
from firefly_forge.identity_persistence import ForgeIdentityUserRepository
from firefly_forge.journal import Journal
from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied
from firefly_forge.project_campaigns import CampaignLaunch, CampaignRetry, ProjectCampaigns
from firefly_forge.project_models import ProjectDefinition
from firefly_forge.projects import Projects
from firefly_forge.store import current_unit_of_work


async def native_campaign(store, tmp_path, *, components=3, window=1, parallel=1):
    source = tmp_path / "source"
    source.mkdir()
    for index in range(components):
        path = source / "services" / str(index)
        path.mkdir(parents=True)
        (path / "README.md").write_text(f"Fictional service {index}\n")
    for argv in (
        ["git", "init", "--initial-branch=main"],
        ["git", "add", "."],
        ["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-m", "Seed source"],
    ):
        subprocess.run(argv, cwd=source, capture_output=True, check=True, timeout=10)
    repo, project, token = await setup(store, source, components=components)
    try:
        definition = copy.deepcopy(project["definition"])
        for component in definition["components"]:
            component["verification"] = [{"argv": [sys.executable, "-c", "assert 7 * 8 == 56"]}]
        project = await Projects(store).configure(ProjectDefinition.model_validate(definition), project["id"])
        journal = Journal(store)
        engine = Engine(store, tmp_path / "workspaces", journal=journal)
        manager = ProjectCampaigns(store, engine, journal)
        configured = await manager.execution_policies.configure(
            project["id"],
            CampaignExecutionPolicyInputV1(
                request_id=uuid4().hex,
                expected_previous_digest=None,
                policy={"dispatch_window": window, "max_parallel_runs": parallel},
            ),
        )
        return repo, project, manager, configured, token
    except BaseException:
        CURRENT_PRINCIPAL.reset(token)
        raise


async def test_native_campaign_exhaustion_renewal_preserves_prior_real_checks_and_spent_reservations(store, tmp_path):
    _, project, manager, policy, token = await native_campaign(store, tmp_path)
    try:
        campaign = await manager.launch(
            project["id"], CampaignLaunch(request_id=uuid4().hex, brief="Verify all owned services", operation="test")
        )
        children = campaign["children"]
        assert [child["status"] for child in children].count("queued") == 1
        first = next(child for child in children if child["status"] == "queued")
        await manager.engine.execute_run(first["run_id"])
        first_run = await store.get_run(first["run_id"])
        assert first_run["status"] == "succeeded"
        assert first_run["final_verification"][0]["argv"] == [sys.executable, "-c", "assert 7 * 8 == 56"]
        assert first_run["final_verification"][0]["exit_code"] == 0
        assert len(first_run["source_tree"]) in {40, 64}
        held = await manager.tick(project["id"], campaign["id"])
        assert held["status"] == "paused"
        assert held["execution_budget_state"]["consumed_dispatches"] == 1
        assert held["execution_budget_state"]["remaining_dispatches"] == 0
        assert sum(child["status"] == "campaign_blocked" for child in held["children"]) == 2
        original_ids = [child["run_id"] for child in held["children"]]
        body = CampaignBudgetRenewalV1(
            request_id=uuid4().hex,
            expected_journal_head=held["journal"]["head"],
            expected_policy_digest=policy["digest"],
            additional_dispatches=1,
            max_parallel_runs=1,
        )
        renewed = await manager.renew_budget(project["id"], campaign["id"], body)
        replay = await manager.renew_budget(project["id"], campaign["id"], body)
        assert replay["journal"]["head"] == renewed["journal"]["head"]
        assert renewed["execution_budget_state"]["granted_dispatches"] == 2
        assert renewed["execution_budget_state"]["consumed_dispatches"] == 2
        assert [child["run_id"] for child in renewed["children"]] == original_ids
        second = next(child for child in renewed["children"] if child["status"] == "queued")
        await manager.engine.execute_run(second["run_id"])
        second_run = await store.get_run(second["run_id"])
        assert second_run["status"] == "succeeded" and second_run["final_verification"][0]["exit_code"] == 0
        assert await store.get_run(first["run_id"]) == first_run
        paused = await manager.tick(project["id"], campaign["id"])
        assert paused["status"] == "paused" and paused["execution_budget_state"]["consumed_dispatches"] == 2
        assert len(await store.runs()) == 3
    finally:
        CURRENT_PRINCIPAL.reset(token)


async def test_native_more_than_fifty_components_keep_finite_dispatch_and_parallel_reservations(store, tmp_path):
    _, project, manager, _, token = await native_campaign(store, tmp_path, components=51, window=60, parallel=2)
    try:
        selected = [component["id"] for component in project["definition"]["components"]]
        campaign = await manager.launch(
            project["id"],
            CampaignLaunch(
                request_id=uuid4().hex, brief="Admit owned services", operation="test", component_ids=selected
            ),
        )
        assert len(campaign["children"]) == len(await store.runs()) == 51
        assert sum(child["status"] == "queued" for child in campaign["children"]) == 2
        assert campaign["execution_budget_state"] == {
            "version": 1,
            "granted_dispatches": 60,
            "consumed_dispatches": 2,
            "remaining_dispatches": 58,
            "max_parallel_runs": 2,
        }
        definition = copy.deepcopy(project["definition"])
        definition["description"] = "A later project revision"
        await Projects(store).configure(ProjectDefinition.model_validate(definition), project["id"])
        assert (await manager.execution_policies.view(project["id"]))["policy"]["dispatch_window"] == 60
    finally:
        CURRENT_PRINCIPAL.reset(token)


async def test_native_revoked_actor_cannot_renew_or_change_existing_campaign_custody(store, tmp_path):
    _, project, manager, policy, token = await native_campaign(store, tmp_path)
    try:
        campaign = await manager.launch(
            project["id"], CampaignLaunch(request_id=uuid4().hex, brief="Verify owned services", operation="test")
        )
        body = CampaignBudgetRenewalV1(
            request_id=uuid4().hex,
            expected_journal_head=campaign["journal"]["head"],
            expected_policy_digest=policy["digest"],
            additional_dispatches=1,
            max_parallel_runs=1,
        )
        actor = CURRENT_PRINCIPAL.get()
        assert actor is not None
        async with store._unit():
            identities = ForgeIdentityUserRepository()
            user = await identities.find_by_id(actor.id)
            user.enabled = False
            await identities.save(user)
        with pytest.raises(AccessDenied):
            await manager.renew_budget(project["id"], campaign["id"], body)
        async with store._unit():
            row = await manager.records.find_by_id(UUID(campaign["id"]))
            entries = await manager.events.events(row.id)
            assert [event.data for event in entries] == campaign["journal"]["entries"]
            assert row.data["execution_budget"] == campaign["execution_budget"]
    finally:
        CURRENT_PRINCIPAL.reset(token)


async def test_native_policy_change_during_preflight_rolls_back_every_campaign_child(store, tmp_path):
    _, project, manager, policy, token = await native_campaign(store, tmp_path)
    try:

        async def changing_policy(repository):
            await manager.execution_policies.configure(
                project["id"],
                CampaignExecutionPolicyInputV1(
                    request_id=uuid4().hex,
                    expected_previous_digest=policy["digest"],
                    policy={"dispatch_window": 2, "max_parallel_runs": 1},
                ),
            )
            return "1" * 40

        manager.source_resolver = changing_policy
        with pytest.raises(ValueError, match="execution policy changed"):
            await manager.launch(
                project["id"], CampaignLaunch(request_id=uuid4().hex, brief="Verify owned services", operation="test")
            )
        assert await store.runs() == [] and await manager.campaigns(project["id"]) == []
    finally:
        CURRENT_PRINCIPAL.reset(token)


async def test_native_explicit_retries_spend_the_same_campaign_window_beyond_the_old_three_retry_ceiling(
    store, tmp_path
):
    _, project, manager, _, token = await native_campaign(store, tmp_path, components=1, window=6)
    try:
        campaign = await manager.launch(
            project["id"], CampaignLaunch(request_id=uuid4().hex, brief="Verify owned service", operation="test")
        )
        first = campaign["children"][0]["run_id"]
        for attempt in range(1, 7):
            current = next(child for child in campaign["children"] if child["current"])
            assert current["attempt"] == attempt and current["status"] == "queued"
            await store.finish(current["run_id"], "failed", error="Controlled known terminal failure without effects")
            campaign = await manager.retry(
                project["id"],
                campaign["id"],
                CampaignRetry(request_id=uuid4().hex, component_ids=["service-0"]),
            )
        current = next(child for child in campaign["children"] if child["current"])
        assert current["attempt"] == 7 and current["status"] == "campaign_blocked"
        assert campaign["status"] == "paused"
        assert campaign["execution_budget_state"]["granted_dispatches"] == 6
        assert campaign["execution_budget_state"]["consumed_dispatches"] == 6
        assert len(await store.runs()) == 7
        assert (await store.get_run(first))["status"] == "failed"
        assert len({child["run_id"] for child in campaign["children"]}) == 7
    finally:
        CURRENT_PRINCIPAL.reset(token)


async def test_native_identical_launches_prepare_once_outside_writer_and_keep_one_child(store, tmp_path, monkeypatch):
    import asyncio

    _, project, manager, _, token = await native_campaign(store, tmp_path, components=1)
    tasks = []
    entered, release = asyncio.Event(), asyncio.Event()
    try:
        prepared = []
        original = store.prepare_run

        async def held_preparation(*args, **kwargs):
            assert current_unit_of_work() is None
            result = await original(*args, **kwargs)
            assert current_unit_of_work() is None
            prepared.append(result)
            entered.set()
            await release.wait()
            return result

        monkeypatch.setattr(store, "prepare_run", held_preparation)
        body = CampaignLaunch(request_id=uuid4().hex, brief="One owned admission", operation="test")
        first = asyncio.create_task(manager.launch(project["id"], body))
        tasks.append(first)
        await asyncio.wait_for(entered.wait(), 10)
        second = asyncio.create_task(manager.launch(project["id"], body))
        tasks.append(second)
        await asyncio.sleep(0)
        release.set()
        results = await asyncio.gather(first, second)
        assert results[0]["id"] == results[1]["id"]
        assert len(prepared) == 1
        assert len(await store.runs()) == 1
        assert results[0]["execution_budget_state"]["consumed_dispatches"] == 1
        assert results[0]["journal"]["head"] == results[1]["journal"]["head"]
    finally:
        release.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        CURRENT_PRINCIPAL.reset(token)
