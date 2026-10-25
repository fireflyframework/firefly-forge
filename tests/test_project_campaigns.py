# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
import uuid

from firefly_forge.engine import Engine
from firefly_forge.journal import Journal
from firefly_forge.models import RepositoryInput
from firefly_forge.project_models import ProjectDefinition
from firefly_forge.projects import Projects


async def test_campaign_launch_freezes_all_children_and_idempotent_replay_admits_once(store, tmp_path):
    from firefly_forge.project_campaigns import CampaignLaunch, ProjectCampaigns

    repo = await store.add_repository(RepositoryInput(name="Inventory", url=str(tmp_path)))
    project = await Projects(store).configure(
        ProjectDefinition.model_validate(
            {
                "name": "Inventory",
                "description": "API and workspace",
                "topology": "monorepo",
                "owners": ["Team"],
                "components": [
                    {
                        "id": "api",
                        "name": "API",
                        "repository_id": repo["id"],
                        "path": "api",
                        "responsibility": "Inventory rules",
                        "verification": [{"argv": ["python", "-m", "pytest"]}],
                    },
                    {
                        "id": "web",
                        "name": "Workspace",
                        "repository_id": repo["id"],
                        "path": "web",
                        "kind": "web",
                        "backend": "none",
                        "frontend": "react",
                        "depends_on": ["api"],
                        "responsibility": "Inventory journey",
                        "verification": [{"argv": ["npm", "test"]}],
                    },
                ],
            }
        )
    )

    async def source_base(repository):
        return "1" * 40

    engine = Engine(store, tmp_path / "workspaces")
    campaigns = ProjectCampaigns(store, engine, Journal(store), source_resolver=source_base)
    request = CampaignLaunch(request_id=uuid.uuid4().hex, brief="Deliver inventory behavior", agent="codex")
    launched = await campaigns.launch(project["id"], request)
    replay = await campaigns.launch(project["id"], request)
    assert replay["id"] == launched["id"]
    assert len(await store.runs()) == 2
    children = {item["component_id"]: item for item in launched["children"]}
    assert children["api"]["status"] == "queued"
    assert children["web"]["status"] == "campaign_blocked"
    first = await store.get_run(children["api"]["run_id"])
    second = await store.get_run(children["web"]["run_id"])
    assert first["config_revision"] == second["config_revision"]
    assert second["campaign_context"]["source_base"] == "1" * 40
    assert second["component_snapshot"]["project_revision"] == 1
    assert launched["journal"]["verified"] is True


async def test_campaign_snapshot_drift_rolls_back_every_child_and_header(store, tmp_path, monkeypatch):
    import pytest

    from firefly_forge.configuration import ProviderConfiguration
    from firefly_forge.project_campaigns import CampaignLaunch, ProjectCampaigns

    repo = await store.add_repository(RepositoryInput(name="Inventory", url=str(tmp_path)))
    project = await Projects(store).configure(
        ProjectDefinition.model_validate(
            {
                "name": "Inventory",
                "description": "API",
                "topology": "monorepo",
                "owners": ["Team"],
                "components": [
                    {"id": "api", "name": "API", "repository_id": repo["id"], "responsibility": "Validate stock"}
                ],
            }
        )
    )
    original = store.insert_prepared_run_locked

    async def changing_settings(request, *, fence=None):
        run = await original(request, fence=fence)
        store.configuration.update("codex", ProviderConfiguration(values={"model": "changed-during-admission"}))
        return run

    monkeypatch.setattr(store, "insert_prepared_run_locked", changing_settings)

    async def source_base(repository):
        return "1" * 40

    manager = ProjectCampaigns(
        store, Engine(store, tmp_path / "workspaces"), Journal(store), source_resolver=source_base
    )
    with pytest.raises(ValueError, match="settings changed"):
        await manager.launch(project["id"], CampaignLaunch(request_id=uuid.uuid4().hex, brief="Deliver"))
    assert await store.runs() == []
    assert await manager.campaigns(project["id"]) == []


async def test_campaign_only_releases_dependency_after_real_checks_and_copies_commit_receipts(store, tmp_path):
    import sys

    from firefly_forge.process import checked
    from firefly_forge.project_campaigns import CampaignLaunch, ProjectCampaigns

    source = tmp_path / "source"
    source.mkdir()
    (source / "api").mkdir()
    (source / "web").mkdir()
    (source / "api" / "README.md").write_text("Inventory service\n")
    (source / "web" / "README.md").write_text("Inventory workspace\n")
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
            "Create source fixture",
        ],
        source,
    )
    repo = await store.add_repository(RepositoryInput(name="Inventory", url=str(source)))
    project = await Projects(store).configure(
        ProjectDefinition.model_validate(
            {
                "name": "Inventory",
                "description": "API and web",
                "topology": "monorepo",
                "owners": ["Team"],
                "components": [
                    {
                        "id": "api",
                        "name": "API",
                        "repository_id": repo["id"],
                        "path": "api",
                        "responsibility": "Stock rules",
                        "verification": [{"argv": [sys.executable, "-c", "assert 2 + 3 == 5"]}],
                    },
                    {
                        "id": "web",
                        "name": "Web",
                        "repository_id": repo["id"],
                        "path": "web",
                        "kind": "web",
                        "backend": "none",
                        "frontend": "react",
                        "responsibility": "Stock journey",
                        "depends_on": ["api"],
                        "verification": [{"argv": [sys.executable, "-c", "assert 'stock'.upper() == 'STOCK'"]}],
                    },
                ],
            }
        )
    )
    journal = Journal(store)
    engine = Engine(store, tmp_path / "workspaces", journal=journal)

    async def source_base(repository):
        return await checked(["git", "rev-parse", "HEAD"], source)

    manager = ProjectCampaigns(store, engine, journal, source_resolver=source_base)
    campaign = await manager.launch(
        project["id"], CampaignLaunch(request_id=uuid.uuid4().hex, brief="Verify stock contracts", operation="test")
    )
    children = {item["component_id"]: item for item in campaign["children"]}
    await manager.tick(project["id"], campaign["id"])
    assert (await store.get_run(children["web"]["run_id"]))["status"] == "campaign_blocked"
    frozen_base = children["api"]["source_base"]
    (source / "api" / "README.md").write_text("Changed after launch\n")
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
            "Advance fixture after admission",
        ],
        source,
    )
    assert await checked(["git", "rev-parse", "HEAD"], source) != frozen_base
    await engine.execute_run(children["api"]["run_id"])
    assert (await store.get_run(children["api"]["run_id"]))["status"] == "succeeded"
    assert (await store.get_run(children["api"]["run_id"]))["base_revision"] == frozen_base
    await manager.tick(project["id"], campaign["id"])
    downstream = await store.get_run(children["web"]["run_id"])
    assert downstream["status"] == "queued"
    upstream = await store.get_run(children["api"]["run_id"])
    assert (
        downstream["campaign_context"]["upstream_receipts"][0]["checkpoint_revision"] == upstream["checkpoint_revision"]
    )
    assert downstream["campaign_context"]["upstream_receipts"][0]["source_tree"] == upstream["source_tree"]
    await engine.execute_run(downstream["id"])
    complete = await manager.tick(project["id"], campaign["id"])
    assert complete["status"] == "succeeded"
    assert complete["journal"]["verified"]


async def one_component(store, tmp_path):
    from firefly_forge.project_campaigns import CampaignLaunch, ProjectCampaigns

    repo = await store.add_repository(RepositoryInput(name="Inventory", url=str(tmp_path)))
    project = await Projects(store).configure(
        ProjectDefinition.model_validate(
            {
                "name": "Inventory",
                "description": "API",
                "topology": "monorepo",
                "owners": ["Team"],
                "components": [
                    {
                        "id": "api",
                        "name": "API",
                        "repository_id": repo["id"],
                        "responsibility": "Stock rules",
                        "verification": [{"argv": ["python", "-m", "pytest"]}],
                    }
                ],
            }
        )
    )

    async def source_base(repository):
        return "1" * 40

    journal = Journal(store)
    engine = Engine(store, tmp_path / "workspaces", journal=journal)
    manager = ProjectCampaigns(store, engine, journal, source_resolver=source_base)
    request = CampaignLaunch(request_id=uuid.uuid4().hex, brief="Deliver stock")
    return project, manager, journal, await manager.launch(project["id"], request)


async def test_campaign_retry_is_idempotent_and_preserves_original_authorized_snapshots(store, tmp_path):
    from firefly_forge.configuration import ProviderConfiguration
    from firefly_forge.project_campaigns import CampaignRetry

    project, manager, journal, campaign = await one_component(store, tmp_path)
    original = await store.get_run(campaign["children"][0]["run_id"])
    await store.finish(original["id"], "failed", error="Known verification failure")
    store.configuration.update("codex", ProviderConfiguration(values={"model": "new-after-launch"}))
    changed = {**project["definition"], "name": "Changed after launch"}
    await Projects(store).configure(ProjectDefinition.model_validate(changed), project["id"])
    request = CampaignRetry(request_id=uuid.uuid4().hex, component_ids=["api"])
    import asyncio

    retried, replay = await asyncio.gather(
        manager.retry(project["id"], campaign["id"], request), manager.retry(project["id"], campaign["id"], request)
    )
    assert len(retried["children"]) == len(replay["children"]) == 2
    assert len(await store.runs()) == 2
    current = next(item for item in retried["children"] if item["current"])
    fresh = await store.get_run(current["run_id"])
    for field in (
        "config_revision",
        "component_snapshot",
        "policy_snapshot",
        "role_snapshot",
        "product_definition",
        "branding_snapshot",
        "design_snapshot",
    ):
        assert fresh[field] == original[field]
    assert fresh["campaign_context"]["source_base"] == "1" * 40
    assert fresh["component_snapshot"]["project_revision"] == 1
    assert retried["journal"]["verified"]


async def test_campaign_does_not_retry_a_cancelled_child_with_an_unreceipted_effect(store, tmp_path):
    import pytest

    from firefly_forge.project_campaigns import CampaignAction, CampaignRetry

    project, manager, journal, campaign = await one_component(store, tmp_path)
    run_id = campaign["children"][0]["run_id"]
    await journal.ensure_plan(run_id)
    await journal.intent(run_id, "prepare", payload={"source_base": "1" * 40})
    cancelled = await manager.cancel(project["id"], campaign["id"], CampaignAction(request_id=uuid.uuid4().hex))
    assert cancelled["status"] == "needs_reconciliation"
    with pytest.raises(ValueError, match="reconciliation"):
        await manager.retry(
            project["id"], campaign["id"], CampaignRetry(request_id=uuid.uuid4().hex, component_ids=["api"])
        )
    assert len(await store.runs()) == 1


async def test_campaign_resume_keeps_child_identity_and_refuses_unanswered_worker_questions(store, tmp_path):
    import pytest

    from firefly_forge.project_campaigns import CampaignAction

    project, manager, journal, campaign = await one_component(store, tmp_path)
    run_id = campaign["children"][0]["run_id"]
    await store.update(run_id, status="paused", worker_request={"status": "pending"})
    with pytest.raises(ValueError, match="worker question"):
        await manager.resume(project["id"], campaign["id"], CampaignAction(request_id=uuid.uuid4().hex))
    await store.update(run_id, worker_request={"status": "answered"})
    action = CampaignAction(request_id=uuid.uuid4().hex)
    resumed = await manager.resume(project["id"], campaign["id"], action)
    replay = await manager.resume(project["id"], campaign["id"], action)
    assert resumed["children"][0]["run_id"] == replay["children"][0]["run_id"] == run_id
    assert (await store.get_run(run_id))["status"] == "queued"
    assert len(await store.runs()) == 1


async def test_concurrent_launches_admit_one_campaign_and_action_keys_cannot_change_content(store, tmp_path):
    import asyncio

    import pytest

    from firefly_forge.project_campaigns import CampaignLaunch

    project, manager, journal, original = await one_component(store, tmp_path)
    request = CampaignLaunch(request_id=uuid.uuid4().hex, brief="One concurrent delivery")
    first, second = await asyncio.gather(manager.launch(project["id"], request), manager.launch(project["id"], request))
    assert first["id"] == second["id"]
    assert len(await store.runs()) == 2
    with pytest.raises(ValueError, match="another launch"):
        await manager.launch(project["id"], request.model_copy(update={"brief": "Different content"}))


async def test_campaign_refuses_a_transient_changed_credential_even_after_settings_restore(
    store, tmp_path, monkeypatch
):
    import pytest

    from firefly_forge.configuration import ProviderConfiguration
    from firefly_forge.project_campaigns import CampaignLaunch, ProjectCampaigns

    repo = await store.add_repository(RepositoryInput(name="Inventory", url=str(tmp_path)))
    project = await Projects(store).configure(
        ProjectDefinition.model_validate(
            {
                "name": "Inventory",
                "description": "API",
                "topology": "monorepo",
                "owners": ["Team"],
                "components": [
                    {"id": "api", "name": "API", "repository_id": repo["id"], "responsibility": "Stock rules"}
                ],
            }
        )
    )
    store.configuration.update("codex", ProviderConfiguration(values={"model": "initial-model"}))
    original = store.prepare_run

    async def temporarily_changed(request, **kwargs):
        store.configuration.update("codex", ProviderConfiguration(values={"model": "temporary-model"}))
        run = await original(request, **kwargs)
        store.configuration.update("codex", ProviderConfiguration(values={"model": "initial-model"}))
        return run

    monkeypatch.setattr(store, "prepare_run", temporarily_changed)

    async def source_base(repository):
        return "1" * 40

    manager = ProjectCampaigns(
        store, Engine(store, tmp_path / "workspaces"), Journal(store), source_resolver=source_base
    )
    with pytest.raises(ValueError, match="snapshot"):
        await manager.launch(project["id"], CampaignLaunch(request_id=uuid.uuid4().hex, brief="Deliver stock"))
    assert await store.runs() == []


async def dependent_campaign(store, tmp_path, **changes):
    from firefly_forge.project_campaigns import CampaignLaunch, ProjectCampaigns

    repo = await store.add_repository(RepositoryInput(name="Inventory", url=str(tmp_path)))
    project = await Projects(store).configure(
        ProjectDefinition.model_validate(
            {
                "name": "Inventory",
                "description": "API and web",
                "topology": "monorepo",
                "owners": ["Team"],
                "components": [
                    {
                        "id": "api",
                        "name": "API",
                        "repository_id": repo["id"],
                        "path": "api",
                        "responsibility": "Stock rules",
                        "verification": [{"argv": ["python", "-m", "pytest"]}],
                    },
                    {
                        "id": "web",
                        "name": "Web",
                        "repository_id": repo["id"],
                        "path": "web",
                        "kind": "web",
                        "backend": "none",
                        "frontend": "react",
                        "depends_on": ["api"],
                        "responsibility": "Inventory journey",
                        "verification": [{"argv": ["npm", "test"]}],
                    },
                ],
            }
        )
    )

    async def source_base(repository):
        return "1" * 40

    journal = Journal(store)
    manager = ProjectCampaigns(
        store, Engine(store, tmp_path / "workspaces", journal=journal), journal, source_resolver=source_base
    )
    campaign = await manager.launch(
        project["id"], CampaignLaunch(request_id=uuid.uuid4().hex, brief="Deliver inventory", **changes)
    )
    return project, manager, journal, campaign


async def recorded_success(store, journal, run_id, checks=None, review=None):
    checks = (
        [{"argv": ["python", "-m", "pytest"], "exit_code": 0, "output": "Actual fixture receipt"}]
        if checks is None
        else checks
    )
    await journal.ensure_plan(run_id)
    for task in ["prepare", "implement", "verify", "checkpoint"]:
        intent = await journal.intent(run_id, task, payload={"fixture": "controlled journal evidence"})
        evidence = {"source_tree": "2" * 40, "checkpoint_revision": "3" * 40}
        if task == "verify":
            evidence["checks"] = checks
        await journal.receipt(run_id, intent["idempotency_key"], evidence)
    await store.update(
        run_id,
        source_tree="2" * 40,
        checkpoint_revision="3" * 40,
        final_verification=checks,
        review=review or {"status": "not_requested", "source_tree": "2" * 40},
    )
    await store.finish(run_id, "succeeded")


async def test_campaign_does_not_treat_success_status_or_empty_checks_as_verification(store, tmp_path):
    project, manager, journal, campaign = await dependent_campaign(store, tmp_path)
    children = {item["component_id"]: item for item in campaign["children"]}
    await recorded_success(store, journal, children["api"]["run_id"], checks=[])
    await manager.tick(project["id"], campaign["id"])
    downstream = await store.get_run(children["web"]["run_id"])
    assert downstream["status"] == "campaign_blocked"
    assert "verification" in downstream["campaign_context"]["blocked_reason"]


async def test_campaign_required_review_needs_matching_durable_review_receipt(store, tmp_path):
    project, manager, journal, campaign = await dependent_campaign(
        store, tmp_path, assurance={"review_agent": "claude"}
    )
    children = {item["component_id"]: item for item in campaign["children"]}
    upstream = children["api"]["run_id"]
    verdict = {"status": "approved", "source_tree": "2" * 40}
    await recorded_success(store, journal, upstream, review=verdict)
    await manager.tick(project["id"], campaign["id"])
    assert (await store.get_run(children["web"]["run_id"]))["status"] == "campaign_blocked"
    intent = await journal.intent(upstream, "review", payload={"source_tree": "2" * 40})
    await journal.receipt(upstream, intent["idempotency_key"], verdict)
    await manager.tick(project["id"], campaign["id"])
    assert (await store.get_run(children["web"]["run_id"]))["status"] == "queued"


async def test_published_dependency_requires_actual_publication_receipt_and_is_not_merge_or_deploy(store, tmp_path):
    project, manager, journal, campaign = await dependent_campaign(store, tmp_path, dependency_gate="published")
    children = {item["component_id"]: item for item in campaign["children"]}
    upstream = children["api"]["run_id"]
    await recorded_success(store, journal, upstream)
    await store.update(upstream, publication_status="published", pr_url="https://github.com/example/stock/pull/1")
    await manager.tick(project["id"], campaign["id"])
    assert (await store.get_run(children["web"]["run_id"]))["status"] == "campaign_blocked"
    intent = await journal.intent(upstream, "publish", payload={"source_tree": "2" * 40})
    await journal.receipt(
        upstream,
        intent["idempotency_key"],
        {"source_tree": "2" * 40, "pr_url": "https://github.com/example/stock/pull/1"},
    )
    result = await manager.tick(project["id"], campaign["id"])
    assert next(item for item in result["children"] if item["component_id"] == "web")["status"] == "queued"
    downstream = await store.get_run(children["web"]["run_id"])
    receipt = downstream["campaign_context"]["upstream_receipts"][0]
    assert receipt["pr_url"] == "https://github.com/example/stock/pull/1"
    assert "merged" not in receipt and "deployed" not in receipt


async def test_campaign_restart_retains_blocked_children_and_concurrent_release_is_unique(store, tmp_path):
    import asyncio

    from firefly_forge.project_campaigns import ProjectCampaigns

    project, manager, journal, campaign = await dependent_campaign(store, tmp_path)
    children = {item["component_id"]: item for item in campaign["children"]}
    assert await store.recover() == 0
    assert (await store.get_run(children["web"]["run_id"]))["status"] == "campaign_blocked"
    await recorded_success(store, journal, children["api"]["run_id"])
    restarted = ProjectCampaigns(store, manager.engine, journal)
    one, two = await asyncio.gather(
        restarted.tick(project["id"], campaign["id"]), manager.tick(project["id"], campaign["id"])
    )
    entries = two["journal"]["entries"]
    assert (
        len(
            [
                entry
                for entry in entries
                if entry["kind"] == "released" and entry["payload"]["run_id"] == children["web"]["run_id"]
            ]
        )
        == 1
    )
    assert len(await store.runs()) == 2
    assert one["journal"]["verified"] and two["journal"]["verified"]


def test_campaign_native_api_authentication_receipts_and_real_verification(tmp_path, postgres_schema):
    import subprocess
    import sys
    import time

    from starlette.testclient import TestClient

    from firefly_forge.api import create_application

    source = tmp_path / "source"
    source.mkdir()
    (source / "README.md").write_text("Native campaign fixture\n")
    for argv in (
        ["git", "init", "--initial-branch=main"],
        ["git", "add", "."],
        [
            "git",
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-m",
            "Create source fixture",
        ],
    ):
        subprocess.run(argv, cwd=source, check=True, capture_output=True)
    token = "test-native-campaign-token-32-characters"
    headers = {"Authorization": "Bearer " + token}
    with TestClient(create_application(tmp_path / "state", token), client=("127.0.0.1", 50000)) as client:
        assert client.get("/api/v1/projects/" + "a" * 32 + "/campaigns").status_code == 401
        repo = client.post("/api/v1/repositories", headers=headers, json={"name": "Fixture", "url": str(source)}).json()
        project = client.post(
            "/api/v1/projects",
            headers=headers,
            json={
                "name": "Inventory",
                "description": "Source-only service",
                "topology": "monorepo",
                "owners": ["Team"],
                "components": [
                    {
                        "id": "api",
                        "name": "API",
                        "repository_id": repo["id"],
                        "responsibility": "Validate inventory",
                        "verification": [{"argv": [sys.executable, "-c", "assert 4 * 4 == 16"]}],
                    }
                ],
            },
        ).json()
        path = f"/api/v1/projects/{project['id']}/campaigns"
        request = {"request_id": uuid.uuid4().hex, "brief": "Verify existing source", "operation": "test"}
        response = client.post(path, headers=headers, json=request)
        assert response.status_code == 201, response.text
        campaign = response.json()
        replay = client.post(path, headers=headers, json=request).json()
        assert replay["id"] == campaign["id"]
        deadline = time.monotonic() + 15
        while campaign["status"] == "running" and time.monotonic() < deadline:
            time.sleep(0.05)
            campaign = client.get(path + "/" + campaign["id"], headers=headers).json()
        assert campaign["status"] == "succeeded", campaign
        assert campaign["journal"]["verified"]
        run = client.get("/api/v1/runs/" + campaign["children"][0]["run_id"], headers=headers).json()
        assert run["final_verification"][0]["exit_code"] == 0
        assert run["campaign_context"]["source_base"] == run["base_revision"]
        wrong = f"/api/v1/projects/{'f' * 32}/campaigns/{campaign['id']}"
        assert client.get(wrong, headers=headers).status_code == 404
        changed = client.post(path, headers=headers, json={**request, "brief": "Different request"})
        assert changed.status_code == 409
        assert client.get("/api/v1/runs", headers=headers).json()[0]["id"] == run["id"]


async def test_campaign_stops_all_release_when_successful_child_has_unreceipted_publication_intent(store, tmp_path):
    project, manager, journal, campaign = await dependent_campaign(store, tmp_path)
    children = {item["component_id"]: item for item in campaign["children"]}
    upstream = children["api"]["run_id"]
    await recorded_success(store, journal, upstream)
    await journal.intent(upstream, "publish", payload={"source_tree": "2" * 40})
    result = await manager.tick(project["id"], campaign["id"])
    assert result["status"] == "needs_reconciliation"
    assert (await store.get_run(children["web"]["run_id"]))["status"] == "campaign_blocked"


async def test_campaign_requested_publication_is_part_of_child_completion_even_with_verified_dependency_gate(
    store, tmp_path
):
    project, manager, journal, campaign = await dependent_campaign(store, tmp_path, publish=True)
    children = {item["component_id"]: item for item in campaign["children"]}
    await recorded_success(store, journal, children["api"]["run_id"])
    result = await manager.tick(project["id"], campaign["id"])
    assert next(item for item in result["children"] if item["component_id"] == "web")["status"] == "campaign_blocked"


async def test_interrupted_campaign_cancel_is_reconciled_from_stopped_receipts_without_repeating_cancel(
    store, tmp_path, monkeypatch
):
    import pytest

    from firefly_forge.project_campaigns import CampaignAction

    project, manager, journal, campaign = await one_component(store, tmp_path)
    actual_cancel = manager.engine.cancel

    async def interrupted_cancel(run_id):
        raise RuntimeError("Simulate coordinator stopping before child cancellation")

    monkeypatch.setattr(manager.engine, "cancel", interrupted_cancel)
    with pytest.raises(ValueError, match="cancellation outcome"):
        await manager.cancel(project["id"], campaign["id"], CampaignAction(request_id=uuid.uuid4().hex))
    blocked = await manager.campaign(project["id"], campaign["id"])
    assert blocked["pending_action"]["action"] == "cancel"
    with pytest.raises(ValueError, match="stopped child"):
        await manager.resume(project["id"], campaign["id"], CampaignAction(request_id=uuid.uuid4().hex))
    await actual_cancel(blocked["children"][0]["run_id"])
    resumed = await manager.resume(project["id"], campaign["id"], CampaignAction(request_id=uuid.uuid4().hex))
    assert resumed["pending_action"] is None
    assert resumed["children"][0]["status"] == "cancelled"
    assert any(entry["kind"] == "action_reconciliation" for entry in resumed["journal"]["entries"])
    assert resumed["journal"]["verified"]
    assert len(await store.runs()) == 1


async def test_campaign_child_admissions_and_events_are_immutable_native_history(store, tmp_path):
    import pytest
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    project, manager, journal, campaign = await one_component(store, tmp_path)
    for table in ("forge_campaign_children", "forge_campaign_events"):
        with pytest.raises(DBAPIError, match="immutable"):
            async with store._unit() as session:
                await session.execute(text(f"UPDATE {table} SET data='{{}}'::jsonb"))
    assert (await manager.campaign(project["id"], campaign["id"]))["journal"]["verified"]
