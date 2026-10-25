# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Owned native PostgreSQL contracts; no coding model, service or SCM effects."""

import copy
import json
from uuid import uuid4

import pytest
from test_activity_archive import command_message
from test_project_revision_reference_host import record

from firefly_forge.activity_models import ActivityContentQuery
from firefly_forge.activity_service import ActivityArchive
from firefly_forge.identity import Principal
from firefly_forge.identity_persistence import ForgeIdentityUserEntity
from firefly_forge.journal import Journal
from firefly_forge.models import RepositoryInput, RunInput
from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied, ProjectAccess
from firefly_forge.project_models import ProjectDefinition
from firefly_forge.project_persistence import ForgeProjectRepository
from firefly_forge.projects import Projects
from firefly_forge.run_evidence import EvidenceQuery, RunEvidence
from firefly_forge.store import identifier, prepared_credential_rejection


async def setup(store, tmp_path, *, components=2):
    actor = Principal(uuid4(), "local", "project-reference-owner", authentication_method="local_bootstrap")
    async with store._unit() as session:
        session.add(ForgeIdentityUserEntity(id=actor.id, issuer=actor.issuer, subject=actor.subject, enabled=True))
    token = CURRENT_PRINCIPAL.set(actor)
    try:
        if components == 5000:
            from test_connection_oauth_ledger import configuration, service

            vault = service(store)
            credential = "fictional-native-project-" + uuid4().hex
            await vault.configure("project-scale-admission", configuration(client_secret=credential), actor)
            history = await vault.source_diff_history()
            assert history.packets and history.matcher is vault.matcher
            assert vault.matcher.contains(credential, list(history.packets))
        repo = await store.add_repository(RepositoryInput(name="Fictional services", url=str(tmp_path)))
        value = record(components)["definition"]
        for component in value["components"]:
            component["repository_id"] = repo["id"]
        value["product_repository_id"] = repo["id"]
        project = await Projects(store).configure(ProjectDefinition.model_validate(value))
        return repo, project, token
    except BaseException:
        CURRENT_PRINCIPAL.reset(token)
        raise


async def test_native_project_reference_freezes_roles_graph_evidence_and_current_reader(store, tmp_path, monkeypatch):
    repo, project, token = await setup(store, tmp_path, components=5000)
    assert len(json.dumps(project["definition"]).encode()) > 1048576
    try:
        from firefly_forge.engine import Engine
        from firefly_forge.project_campaigns import CampaignLaunch, ProjectCampaigns
        from firefly_forge.project_snapshot import PreparedProjectRevision

        capture = PreparedProjectRevision.capture.__func__
        captured = []

        def observed(cls, value):
            captured.append(value["id"])
            return capture(cls, value)

        async def controlled_source(repository):
            return "1" * 40

        manager = ProjectCampaigns(
            store, Engine(store, tmp_path / "workspaces"), Journal(store), source_resolver=controlled_source
        )
        with monkeypatch.context() as patch:
            patch.setattr(PreparedProjectRevision, "capture", classmethod(observed))
            campaign = await manager.launch(
                project["id"],
                CampaignLaunch(
                    request_id=uuid4().hex,
                    brief="Maintain owned services",
                    operation="maintain",
                    component_ids=["service-0", "service-1"],
                ),
            )
            assert captured == [project["id"], project["id"]]
        assert len(campaign["children"]) == 2
        runs = [await store.get_run(child["run_id"]) for child in campaign["children"]]
        run = next(item for item in runs if item["component_id"] == "service-0")
        assert all("project_definition" not in item["component_snapshot"] for item in runs)
        assert len(await store.runs()) == 2
        assert run["role_selection_source"] == "project"
        assert next(role for role in run["role_snapshot"]["roles"] if role["id"] == "docs")["enabled"] is False
        component = run["component_snapshot"]
        assert "project_definition" not in component
        assert component["project_revision_ref"]["kind"] == "project_revision_reference"
        journal = Journal(store)
        plan = await journal.ensure_plan(run["id"])
        assert len(json.dumps(plan).encode()) < 1048576
        assert plan["component_snapshot"]["project_revision_ref"]["kind"] == "project_revision_reference"
        before = await journal.entries(run["id"])
        revised = copy.deepcopy(project["definition"])
        revised["components"][1]["responsibility"] = "New current responsibility"
        await Projects(store).configure(ProjectDefinition.model_validate(revised), project["id"])
        assert await Projects(store).snapshot_definition(component) == project["definition"]
        await RunEvidence(store).query(run["id"], EvidenceQuery())
        await store.event(run["id"], "stdout", command_message("observed result " * 2000))
        event = next(e for e in (await store.get_run(run["id"]))["events"] if "activity_content" in e)
        await ActivityArchive(store).content(
            run["id"],
            event["id"],
            ActivityContentQuery(expected_descriptor=event["activity_content"]["descriptor_sha256"]),
        )
        viewer = Principal(uuid4(), "local", "current-project-reader")
        await ProjectAccess(store).set_membership(project["id"], viewer, "viewer")
        reader = CURRENT_PRINCIPAL.set(viewer)
        try:
            await RunEvidence(store).query(run["id"], EvidenceQuery())
        finally:
            CURRENT_PRINCIPAL.reset(reader)
        await ProjectAccess(store).set_membership(project["id"], viewer, None)
        reader = CURRENT_PRINCIPAL.set(viewer)
        try:
            with pytest.raises(AccessDenied):
                await RunEvidence(store).query(run["id"], EvidenceQuery())
        finally:
            CURRENT_PRINCIPAL.reset(reader)
        assert await journal.entries(run["id"]) == before
    finally:
        CURRENT_PRINCIPAL.reset(token)


async def test_native_changed_project_refuses_prepared_child_without_partial_run(store, tmp_path):
    repo, project, token = await setup(store, tmp_path)
    try:
        request = RunInput(
            repository_id=repo["id"], project_id=project["id"], component_id="service-0", brief="Maintain service"
        )
        prepared = await store.prepare_run(request)
        revised = copy.deepcopy(project["definition"])
        revised["components"][0]["responsibility"] = "Changed while preparing"
        await Projects(store).configure(ProjectDefinition.model_validate(revised), project["id"])
        with prepared_credential_rejection(store), pytest.raises(ValueError, match="component changed"):
            async with store._unit():
                fence = await store.fence_prepared_runs([prepared])
                await store.insert_prepared_run_locked(prepared, fence=fence)
        assert await store.runs() == []
        async with store._unit():
            records = ForgeProjectRepository()
            current = await records.find_by_id(identifier(project["id"]))
            assert current is not None
            current.data = {**current.data, "revision": 3}
            await records.save(current)
        with pytest.raises(KeyError, match="revision"):
            await store.prepare_run(request)
        assert await store.runs() == []
    finally:
        CURRENT_PRINCIPAL.reset(token)


@pytest.mark.parametrize("change", ["missing", "digest"])
async def test_native_invalid_reference_is_unavailable_without_history_mutation(store, tmp_path, change):
    repo, project, token = await setup(store, tmp_path)
    try:
        run = await store.create_run(
            RunInput(
                repository_id=repo["id"], project_id=project["id"], component_id="service-0", brief="Maintain service"
            )
        )
        journal = Journal(store)
        await journal.ensure_plan(run["id"])
        before_run = await store.get_run(run["id"])
        before_journal = await journal.entries(run["id"])
        await RunEvidence(store).query(run["id"], EvidenceQuery())
        component = copy.deepcopy(run["component_snapshot"])
        if change == "missing":
            component["project_revision"] = component["project_revision_ref"]["revision"] = 999
            failure = KeyError
        else:
            component["project_digest"] = component["project_revision_ref"]["digest"] = "0" * 64
            failure = ValueError
        with pytest.raises(failure):
            await Projects(store).snapshot_definition(
                component, project_id=project["id"], component_id="service-0", repository_id=repo["id"]
            )
        assert await Projects(store).revision(project["id"], 1) == project
        assert await store.get_run(run["id"]) == before_run
        assert await journal.entries(run["id"]) == before_journal
        await RunEvidence(store).query(run["id"], EvidenceQuery())
    finally:
        CURRENT_PRINCIPAL.reset(token)
