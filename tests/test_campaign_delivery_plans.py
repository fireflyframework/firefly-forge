# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Post-admission reporting criteria never change execution or claim prior enforcement."""

from uuid import uuid4

import pytest
from test_campaign_delivery_progress import fixture
from test_campaign_reports import seeded, service

from firefly_forge.campaign_report_history import CampaignReportHistory
from firefly_forge.campaign_report_history_persistence import ForgeCampaignReportArchiveRepository
from firefly_forge.identity_persistence import ForgeIdentityUserEntity
from firefly_forge.project_access import CURRENT_PRINCIPAL


async def prepared(store, tmp_path):
    from types import SimpleNamespace

    from firefly_forge.campaign_delivery_plan_persistence import ForgeCampaignDeliveryPlanRepository
    from firefly_forge.campaign_delivery_plans import CampaignDeliveryPlanInput, CampaignDeliveryPlans
    from firefly_forge.campaign_report_history import CampaignReportArchiveInput

    project, manager, journal, campaign, token = await seeded(store, tmp_path)
    reports = service(store, manager, journal)
    history = CampaignReportHistory(store, reports, ForgeCampaignReportArchiveRepository())
    report = await reports.report(project["id"], campaign["id"])
    archive = await history.archive(
        project["id"],
        campaign["id"],
        CampaignReportArchiveInput(
            request_id=uuid4().hex,
            expected_report_digest=report["digest"],
        ),
    )
    request = fixture()[0]["request"]
    request.update(
        request_id=uuid4().hex,
        expected_project_digest=report["campaign"]["project_digest"],
        expected_campaign_request_digest=report["campaign"]["request_digest"],
    )
    return SimpleNamespace(
        project=project,
        manager=manager,
        journal=journal,
        campaign=campaign,
        token=token,
        reports=reports,
        history=history,
        report=report,
        archive=archive,
        plans=CampaignDeliveryPlans(store, reports, history, ForgeCampaignDeliveryPlanRepository()),
        body=CampaignDeliveryPlanInput.model_validate(request),
    )


def next_input(body, previous):
    from firefly_forge.campaign_delivery_plans import CampaignDeliveryPlanInput

    return CampaignDeliveryPlanInput.model_validate(
        {**body.model_dump(), "request_id": uuid4().hex, "expected_previous_digest": previous}
    )


async def test_explicit_plan_and_progress_bind_immutable_report_without_execution(store, tmp_path):
    project, manager, journal, campaign, token = await seeded(store, tmp_path)
    try:
        reports = service(store, manager, journal)
        from firefly_forge.campaign_report_history import CampaignReportArchiveInput

        history = CampaignReportHistory(store, reports, ForgeCampaignReportArchiveRepository())
        report = await reports.report(project["id"], campaign["id"])
        archive = await history.archive(
            project["id"],
            campaign["id"],
            CampaignReportArchiveInput(
                request_id=uuid4().hex,
                expected_report_digest=report["digest"],
            ),
        )
        from firefly_forge.campaign_delivery_plan_persistence import ForgeCampaignDeliveryPlanRepository
        from firefly_forge.campaign_delivery_plans import CampaignDeliveryPlanInput, CampaignDeliveryPlans

        plans = CampaignDeliveryPlans(store, reports, history, ForgeCampaignDeliveryPlanRepository())
        body = CampaignDeliveryPlanInput(
            request_id=uuid4().hex,
            expected_previous_digest=None,
            expected_campaign_request_digest=report["campaign"]["request_digest"],
            expected_project_digest=report["campaign"]["project_digest"],
            title="Inventory evidence",
            waves=[{"id": "first", "name": "First delivery", "component_ids": ["api"]}],
            milestones=[
                {
                    "id": "ready",
                    "name": "API verified",
                    "wave_id": "first",
                    "conditions": [
                        {"kind": "component_verified", "component_id": "api"},
                    ],
                }
            ],
        )
        before = await manager.campaign(project["id"], campaign["id"])
        saved = await plans.save(project["id"], campaign["id"], body)
        assert saved == await plans.get(project["id"], campaign["id"], body.request_id)
        assert saved == await plans.save(project["id"], campaign["id"], body)
        progress = await plans.progress(project["id"], campaign["id"], body.request_id, archive["request_id"])
        assert progress["criteria_binding"] == "reporting_only_post_admission"
        assert progress["enforced_at_admission"] is False
        assert progress["state"] == "in_progress"
        assert progress["report_digest"] == report["digest"]
        assert progress == await plans.progress(project["id"], campaign["id"], body.request_id, archive["request_id"])
        assert (await plans.list(project["id"], campaign["id"]))["items"][0]["receipt_digest"] == saved["digest"]
        assert await manager.campaign(project["id"], campaign["id"]) == before
        assert await reports.report(project["id"], campaign["id"]) == report
    finally:
        CURRENT_PRINCIPAL.reset(token)


async def test_plan_revision_cas_keeps_old_receipt_exact_after_new_revision(store, tmp_path):
    c = await prepared(store, tmp_path)
    try:
        first = await c.plans.save(c.project["id"], c.campaign["id"], c.body)
        second = await c.plans.save(c.project["id"], c.campaign["id"], next_input(c.body, first["digest"]))
        assert second["revision"] == 2 and second["previous_digest"] == first["digest"]
        assert await c.plans.save(c.project["id"], c.campaign["id"], c.body) == first
        with pytest.raises(ValueError):
            await c.plans.save(c.project["id"], c.campaign["id"], next_input(c.body, None))
        assert len((await c.plans.list(c.project["id"], c.campaign["id"]))["items"]) == 2
    finally:
        CURRENT_PRINCIPAL.reset(c.token)


@pytest.mark.parametrize("same_id", [True, False])
async def test_concurrent_admission_is_idempotent_or_has_explicit_revision_conflict(store, tmp_path, same_id):
    import asyncio

    c = await prepared(store, tmp_path)
    try:
        gate = asyncio.Event()
        arrived = 0

        async def credentials(value):
            nonlocal arrived
            if "receipt" in value and "summary" in value:
                arrived += 1
                if arrived == 2:
                    gate.set()
                await asyncio.wait_for(gate.wait(), 2)

        store.vault_credential_admission = credentials
        other = c.body if same_id else next_input(c.body, None)
        result = await asyncio.gather(
            *(c.plans.save(c.project["id"], c.campaign["id"], body) for body in (c.body, other)), return_exceptions=True
        )
        if same_id:
            assert result[0] == result[1] and isinstance(result[0], dict)
        else:
            assert sum(isinstance(item, ValueError) for item in result) == 1
            assert "advanced" in str(next(item for item in result if isinstance(item, ValueError)))
        assert len((await c.plans.list(c.project["id"], c.campaign["id"]))["items"]) == 1
    finally:
        CURRENT_PRINCIPAL.reset(c.token)


@pytest.mark.parametrize("operation", ["UPDATE", "DELETE"])
async def test_native_plan_revision_guard_refuses_mutation(store, tmp_path, operation):
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    c = await prepared(store, tmp_path)
    try:
        saved = await c.plans.save(c.project["id"], c.campaign["id"], c.body)
        sql = (
            "UPDATE forge_campaign_delivery_plan_revisions SET data=data"
            if operation == "UPDATE"
            else ("DELETE FROM forge_campaign_delivery_plan_revisions")
        )
        with pytest.raises(DBAPIError):
            async with store._unit() as session:
                await session.execute(text(sql))
        assert await c.plans.get(c.project["id"], c.campaign["id"], c.body.request_id) == saved
    finally:
        CURRENT_PRINCIPAL.reset(c.token)


@pytest.mark.parametrize(
    "change", ["project_digest", "launch_digest", "foreign_component", "implement_test", "review_absent"]
)
async def test_plan_binds_frozen_scope_and_available_native_task_phases(store, tmp_path, change):
    from firefly_forge.campaign_delivery_plans import CampaignDeliveryPlanInput

    c = await prepared(store, tmp_path)
    try:
        body = c.body.model_dump()
        if change == "project_digest":
            body["expected_project_digest"] = "0" * 64
        elif change == "launch_digest":
            body["expected_campaign_request_digest"] = "0" * 64
        elif change == "foreign_component":
            body["waves"][0]["component_ids"] = ["foreign"]
            body["milestones"][0]["conditions"][0]["component_id"] = "foreign"
        else:
            body["milestones"][0]["conditions"][0]["task_id"] = "review" if change == "review_absent" else "implement"
            if change == "implement_test":
                # The actual immutable operation is test in a separately admitted campaign.
                from firefly_forge.project_campaigns import CampaignLaunch

                c.campaign = await c.manager.launch(
                    c.project["id"],
                    CampaignLaunch(request_id=uuid4().hex, brief="Owned test-only campaign", operation="test"),
                )
                report = await c.reports.report(c.project["id"], c.campaign["id"])
                body["expected_campaign_request_digest"] = report["campaign"]["request_digest"]
        with pytest.raises(ValueError):
            await c.plans.save(c.project["id"], c.campaign["id"], CampaignDeliveryPlanInput.model_validate(body))
        assert (await c.plans.list(c.project["id"], c.campaign["id"]))["items"] == []
    finally:
        CURRENT_PRINCIPAL.reset(c.token)


async def test_progress_presizes_combined_bodies_before_either_exact_read(store, tmp_path, monkeypatch):
    import firefly_forge.campaign_delivery_plans as module

    c = await prepared(store, tmp_path)
    try:
        await c.plans.save(c.project["id"], c.campaign["id"], c.body)
        calls = []

        async def forbidden(*args):
            calls.append(args)
            pytest.fail("Body read must follow combined JSON byte preflight")

        monkeypatch.setattr(c.plans, "get", forbidden)
        monkeypatch.setattr(c.history, "get", forbidden)
        monkeypatch.setattr(module, "MAX_PROGRESS_BYTES", 1)
        with pytest.raises(ValueError, match="aggregate byte bound"):
            await c.plans.progress(c.project["id"], c.campaign["id"], c.body.request_id, c.archive["request_id"])
        assert calls == []
    finally:
        CURRENT_PRINCIPAL.reset(c.token)


@pytest.mark.parametrize("read", ["get", "list", "progress"])
async def test_current_credentials_fence_all_retained_reporting_outputs(store, tmp_path, monkeypatch, read):
    c = await prepared(store, tmp_path)
    try:
        saved = await c.plans.save(c.project["id"], c.campaign["id"], c.body)
        target = saved["digest"]
        if read == "progress":
            target = (
                await c.plans.progress(c.project["id"], c.campaign["id"], c.body.request_id, c.archive["request_id"])
            )["digest"]
        monkeypatch.setenv("OWNED_DELIVERY_API_TOKEN", target)
        with pytest.raises(ValueError) as error:
            if read == "get":
                await c.plans.get(c.project["id"], c.campaign["id"], c.body.request_id)
            elif read == "list":
                await c.plans.list(c.project["id"], c.campaign["id"])
            else:
                await c.plans.progress(c.project["id"], c.campaign["id"], c.body.request_id, c.archive["request_id"])
        assert target not in str(error.value)
    finally:
        CURRENT_PRINCIPAL.reset(c.token)


@pytest.mark.parametrize("existing", [False, True])
async def test_post_return_requires_execute_even_after_native_insert(store, tmp_path, existing):
    from firefly_forge.identity import Principal
    from firefly_forge.project_access import AccessDenied, ProjectAccess

    c = await prepared(store, tmp_path)
    try:
        actor = Principal(uuid4(), "https://identity.invalid", "reporting-developer")
        access = ProjectAccess(store)
        await access.set_membership(c.project["id"], actor, "developer")
        acting = CURRENT_PRINCIPAL.set(actor)
        try:
            if existing:
                await c.plans.save(c.project["id"], c.campaign["id"], c.body)

            async def demote(value):
                if "receipt" in value and "summary" not in value:
                    await access.set_membership(c.project["id"], actor, "viewer")

            store.vault_credential_admission = demote
            with pytest.raises(AccessDenied):
                await c.plans.save(c.project["id"], c.campaign["id"], c.body)
            store.vault_credential_admission = None
            assert (await c.plans.get(c.project["id"], c.campaign["id"], c.body.request_id))[
                "request"
            ] == c.body.model_dump()
        finally:
            CURRENT_PRINCIPAL.reset(acting)
    finally:
        CURRENT_PRINCIPAL.reset(c.token)


async def test_native_revision_quota_and_schema25_upgrade_preserve_archives(store, tmp_path, monkeypatch):
    from sqlalchemy import text

    import firefly_forge.campaign_delivery_plans as module
    from firefly_forge.migrations import SCHEMA_REVISION, migrate

    c = await prepared(store, tmp_path)
    try:
        assert SCHEMA_REVISION == 31
        async with store._unit() as session:
            assert await session.scalar(text("SELECT count(*) FROM forge_event_content")) == 0
            await session.execute(text("DROP TABLE forge_campaign_delivery_plan_revisions"))
            await session.execute(text("DELETE FROM forge_schema_migrations WHERE revision>25"))
            await session.execute(
                text("INSERT INTO forge_schema_migrations(revision) VALUES (25) ON CONFLICT DO NOTHING")
            )
            await migrate(session, store.schema)
            assert await session.scalar(text("SELECT max(revision) FROM forge_schema_migrations")) == SCHEMA_REVISION
            assert await session.scalar(text("SELECT count(*) FROM forge_event_content")) == 0
        assert await c.history.get(c.project["id"], c.campaign["id"], c.archive["request_id"]) == c.archive
        saved = await c.plans.save(c.project["id"], c.campaign["id"], c.body)
        monkeypatch.setattr(module, "MAX_REVISIONS", 1)
        with pytest.raises(ValueError):
            await c.plans.save(c.project["id"], c.campaign["id"], next_input(c.body, saved["digest"]))
        assert len((await c.plans.list(c.project["id"], c.campaign["id"]))["items"]) == 1
    finally:
        CURRENT_PRINCIPAL.reset(c.token)


async def test_product_links_use_frozen_admission_even_after_current_definition_changes(store, tmp_path):
    from firefly_forge.campaign_delivery_plan_models import CampaignDeliveryPlanInput
    from firefly_forge.collaboration import Collaboration, ProductDefinition
    from firefly_forge.project_campaigns import CampaignLaunch

    c = await prepared(store, tmp_path)
    try:
        repo = c.project["definition"]["components"][0]["repository_id"]
        collaboration = Collaboration(store)
        original = ProductDefinition(
            customer="Stock team",
            problem="Count fictional stock",
            journey=["Count stock"],
            user_stories=["As an operator I count fictional stock"],
            acceptance=["Count is correct"],
            owners=["Team"],
            features=[
                {
                    "id": "stock",
                    "name": "Stock",
                    "journey_refs": ["Count stock"],
                    "acceptance_refs": ["acceptance-1"],
                    "component_refs": ["api"],
                    "behaviors": [
                        {
                            "id": "count",
                            "given": "Fictional stock exists",
                            "when": "Operator counts",
                            "then": "Count is correct",
                            "failure_paths": ["Missing stock is reported"],
                        }
                    ],
                }
            ],
        )
        first = await collaboration.save_product(repo, original)
        campaign = await c.manager.launch(
            c.project["id"], CampaignLaunch(request_id=uuid4().hex, brief="Owned product-linked campaign")
        )
        report = await c.reports.report(c.project["id"], campaign["id"])
        # Editing the current product cannot change the already frozen campaign references.
        await collaboration.save_product(repo, original.model_copy(update={"journey": ["New journey"], "features": []}))
        body = c.body.model_dump()
        body.update(request_id=uuid4().hex, expected_campaign_request_digest=report["campaign"]["request_digest"])
        body["milestones"][0].update(
            feature_ids=["stock"], journey_refs=["Count stock"], acceptance_refs=["acceptance-1"]
        )
        saved = await c.plans.save(c.project["id"], campaign["id"], CampaignDeliveryPlanInput.model_validate(body))
        assert saved["source"]["product_definitions"] == [
            {"repository_id": repo, "revision": first["revision"], "digest": first["digest"]}
        ]
        body["request_id"] = uuid4().hex
        body["expected_previous_digest"] = saved["digest"]
        body["milestones"][0]["journey_refs"] = ["New journey"]
        with pytest.raises(ValueError):
            await c.plans.save(c.project["id"], campaign["id"], CampaignDeliveryPlanInput.model_validate(body))
    finally:
        CURRENT_PRINCIPAL.reset(c.token)


async def test_current_retry_task_receipt_and_historical_archive_are_separate_evidence_cuts(store, tmp_path):
    from firefly_forge.campaign_report_history import CampaignReportArchiveInput
    from firefly_forge.project_campaigns import CampaignRetry

    c = await prepared(store, tmp_path)
    try:
        saved = await c.plans.save(c.project["id"], c.campaign["id"], c.body)
        first_run = c.campaign["children"][0]["run_id"]
        await store.finish(first_run, "failed", error="Owned fixture failure")
        retried = await c.manager.retry(
            c.project["id"], c.campaign["id"], CampaignRetry(request_id=uuid4().hex, component_ids=["api"])
        )
        current_children = [item for item in retried["children"] if item["component_id"] == "api" and item["current"]]
        assert len(current_children) == 1 and current_children[0]["attempt"] == 2
        assert any(
            item["run_id"] == first_run and item["attempt"] == 1 and not item["current"] for item in retried["children"]
        )
        current = current_children[0]["run_id"]
        assert current != first_run
        await c.journal.ensure_plan(current)
        intent = await c.journal.intent(current, "prepare")
        await c.journal.receipt(current, intent["idempotency_key"], {"branch": "owned-retry-fixture"})
        await store.update(current, branch="owned-retry-fixture")
        report = await c.reports.report(c.project["id"], c.campaign["id"])
        archive = await c.history.archive(
            c.project["id"],
            c.campaign["id"],
            CampaignReportArchiveInput(request_id=uuid4().hex, expected_report_digest=report["digest"]),
        )
        progress = await c.plans.progress(c.project["id"], c.campaign["id"], saved["request_id"], archive["request_id"])
        condition = progress["milestones"][0]["conditions"][0]
        assert condition["state"] == "satisfied"
        assert condition["run_id"] == current
        assert condition["component_attempt"] == 2 and condition["task_attempt"] == 1
        old = await c.plans.progress(c.project["id"], c.campaign["id"], saved["request_id"], c.archive["request_id"])
        assert old["milestones"][0]["conditions"][0]["state"] == "not_started"
        assert old["milestones"][0]["conditions"][0]["run_id"] == first_run
    finally:
        CURRENT_PRINCIPAL.reset(c.token)


async def test_actual_git_and_python_receipts_satisfy_only_the_explicit_saved_progress_cut(store, tmp_path):
    import sys

    from firefly_forge.campaign_delivery_plan_models import CampaignDeliveryPlanInput
    from firefly_forge.campaign_delivery_plan_persistence import ForgeCampaignDeliveryPlanRepository
    from firefly_forge.campaign_delivery_plans import CampaignDeliveryPlans
    from firefly_forge.campaign_report_history import CampaignReportArchiveInput
    from firefly_forge.engine import Engine
    from firefly_forge.identity import Principal
    from firefly_forge.journal import Journal
    from firefly_forge.models import RepositoryInput
    from firefly_forge.process import checked
    from firefly_forge.project_campaigns import CampaignLaunch, ProjectCampaigns
    from firefly_forge.project_models import ProjectDefinition
    from firefly_forge.projects import Projects

    source = tmp_path / "source"
    source.mkdir()
    (source / "stock.py").write_text("stock = 5\n")
    for argv in (
        ["git", "init", "--initial-branch=main"],
        ["git", "add", "."],
        ["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-m", "Own source"],
    ):
        await checked(argv, source)
    repo = await store.add_repository(RepositoryInput(name="Owned stock", url=str(source)))
    project = await Projects(store).configure(
        ProjectDefinition.model_validate(
            {
                "name": "Stock",
                "description": "Owned local verification",
                "topology": "monorepo",
                "owners": ["Team"],
                "components": [
                    {
                        "id": "api",
                        "name": "API",
                        "repository_id": repo["id"],
                        "responsibility": "Stock",
                        "verification": [
                            {"argv": [sys.executable, "-c", "from stock import stock; assert stock == 5"]}
                        ],
                    }
                ],
            }
        )
    )
    journal = Journal(store)
    engine = Engine(store, tmp_path / "workspaces", journal=journal)

    async def resolve(_repository):
        return await checked(["git", "rev-parse", "HEAD"], source)

    manager = ProjectCampaigns(store, engine, journal, source_resolver=resolve)
    actor = Principal(uuid4(), "local", "owned-plan", authentication_method="local_bootstrap")
    async with store._unit() as session:
        session.add(ForgeIdentityUserEntity(id=actor.id, issuer=actor.issuer, subject=actor.subject, enabled=True))
    token = CURRENT_PRINCIPAL.set(actor)
    try:
        campaign = await manager.launch(
            project["id"], CampaignLaunch(request_id=uuid4().hex, brief="Verify owned stock", operation="test")
        )
        await engine.execute_run(campaign["children"][0]["run_id"])
        reports = service(store, manager, journal)
        report = await reports.report(project["id"], campaign["id"])
        assert report["state"] == "verified"
        history = CampaignReportHistory(store, reports, ForgeCampaignReportArchiveRepository())
        archive = await history.archive(
            project["id"],
            campaign["id"],
            CampaignReportArchiveInput(request_id=uuid4().hex, expected_report_digest=report["digest"]),
        )
        body = fixture()[0]["request"]
        body.update(
            request_id=uuid4().hex,
            expected_project_digest=report["campaign"]["project_digest"],
            expected_campaign_request_digest=report["campaign"]["request_digest"],
        )
        body["milestones"][0]["conditions"].append(
            {"kind": "component_verified", "component_id": "api", "task_id": None}
        )
        plans = CampaignDeliveryPlans(store, reports, history, ForgeCampaignDeliveryPlanRepository())
        receipt = await plans.save(project["id"], campaign["id"], CampaignDeliveryPlanInput.model_validate(body))
        progress = await plans.progress(project["id"], campaign["id"], receipt["request_id"], archive["request_id"])
        assert progress["state"] == progress["waves"][0]["state"] == "satisfied"
        condition = progress["milestones"][0]["conditions"][0]
        assert condition["component_id"] == "api" and condition["task_id"] == "prepare"
        component = next(item for item in report["components"] if item["component_id"] == condition["component_id"])
        task = next(item for item in component["tasks"] if item["id"] == condition["task_id"])
        assert condition["receipt"] == task["receipt"]
        assert progress["enforced_at_admission"] is False
        assert await reports.report(project["id"], campaign["id"]) == report
    finally:
        CURRENT_PRINCIPAL.reset(token)
