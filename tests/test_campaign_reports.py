# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Campaign reports are local evidence snapshots, never delivery estimates."""

import importlib.util
from uuid import uuid4

import pytest
from test_project_campaigns import one_component

from firefly_forge.identity import Principal
from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied, ProjectAccess


def service(store, manager, journal):
    if importlib.util.find_spec("firefly_forge.campaign_reports") is None:
        pytest.fail("A native campaign evidence-report service is required")
    from firefly_forge.campaign_reports import CampaignReports

    return CampaignReports(store, manager, journal)


async def seeded(store, tmp_path):
    project, manager, journal, campaign = await one_component(store, tmp_path)
    context = CURRENT_PRINCIPAL.set(
        Principal(uuid4(), "local", "report-owner", authentication_method="local_bootstrap")
    )
    return project, manager, journal, campaign, context


async def test_report_is_repeatable_local_evidence_without_claiming_milestones_or_scm(store, tmp_path):
    project, manager, journal, campaign, context = await seeded(store, tmp_path)
    try:
        reports = service(store, manager, journal)
        before = await manager.campaign(project["id"], campaign["id"])
        report = await reports.report(project["id"], campaign["id"])
        assert report == await reports.report(project["id"], campaign["id"])
        assert report["state"] == "in_progress"
        assert report["counts"] == {"in_progress": 1}
        assert report["milestones"] == report["waves"] == "not_configured"
        assert report["scm_freshness"] == "not_observed"
        assert report["campaign"]["id"] == campaign["id"]
        assert report["campaign"]["journal_head"] == campaign["journal"]["head"]
        assert report["components"][0]["run_id"] == campaign["children"][0]["run_id"]
        assert report["components"][0]["checks"] == []
        assert "percent" not in str(report)
        assert await manager.campaign(project["id"], campaign["id"]) == before
    finally:
        CURRENT_PRINCIPAL.reset(context)


async def test_success_status_without_matching_checks_and_checkpoint_is_unknown(store, tmp_path):
    project, manager, journal, campaign, context = await seeded(store, tmp_path)
    try:
        run_id = campaign["children"][0]["run_id"]
        await store.finish(run_id, "succeeded")
        report = await service(store, manager, journal).report(project["id"], campaign["id"])
        assert report["state"] == "unknown"
        assert report["counts"] == {"unknown": 1}
        assert report["components"][0]["state"] == "unknown"
        assert report["components"][0]["verification"] != "verified"
    finally:
        CURRENT_PRINCIPAL.reset(context)


async def test_unknown_effect_is_not_counted_as_verified_task(store, tmp_path):
    project, manager, journal, campaign, context = await seeded(store, tmp_path)
    try:
        run_id = campaign["children"][0]["run_id"]
        await journal.ensure_plan(run_id)
        await journal.intent(run_id, "verify")
        await store.update(run_id, status="needs_reconciliation")
        report = await service(store, manager, journal).report(project["id"], campaign["id"])
        item = report["components"][0]
        assert report["state"] == item["state"] == "unknown"
        task = next(x for x in item["tasks"] if x["id"] == "verify")
        assert task["state"] == "in_progress" and task["receipt"] is None
        assert item["journal"]["head"] == (await journal.entries(run_id))[-1]["digest"]
    finally:
        CURRENT_PRINCIPAL.reset(context)


async def test_mutable_done_task_without_immutable_receipt_refuses_partial_verified_totals(store, tmp_path):
    project, manager, journal, campaign, context = await seeded(store, tmp_path)
    try:
        run_id = campaign["children"][0]["run_id"]
        await journal.ensure_plan(run_id)
        async with store._unit():
            task = next(
                x for x in await journal.tasks.find_for_run(__import__("uuid").UUID(run_id)) if x.task_id == "verify"
            )
            task.status, task.data = "done", {"evidence": {"checks": []}}
            await journal.tasks.save(task)
        report = await service(store, manager, journal).report(project["id"], campaign["id"])
        assert report["state"] == "unknown"
        assert report["counts"] is None and report["components"] == []
        assert report["reason"] == "evidence_integrity"
    finally:
        CURRENT_PRINCIPAL.reset(context)


async def test_report_requires_current_project_and_repository_read_authority(store, tmp_path):
    project, manager, journal, campaign, context = await seeded(store, tmp_path)
    try:
        reports = service(store, manager, journal)
        viewer = Principal(uuid4(), "https://identity.invalid", "owned-viewer")
        await ProjectAccess(store).set_membership(project["id"], viewer, "viewer")
        token = CURRENT_PRINCIPAL.set(viewer)
        try:
            assert (await reports.report(project["id"], campaign["id"]))["counts"] == {"in_progress": 1}
            await ProjectAccess(store).set_membership(project["id"], viewer, None)
            with pytest.raises(AccessDenied):
                await reports.report(project["id"], campaign["id"])
        finally:
            CURRENT_PRINCIPAL.reset(token)
        with pytest.raises(KeyError):
            await reports.report(uuid4().hex, campaign["id"])
    finally:
        CURRENT_PRINCIPAL.reset(context)


async def test_report_digest_covers_the_complete_returned_snapshot(store, tmp_path):
    from firefly_forge.journal import digest

    project, manager, journal, campaign, context = await seeded(store, tmp_path)
    try:
        report = await service(store, manager, journal).report(project["id"], campaign["id"])
        assert report["digest"] == digest({key: value for key, value in report.items() if key != "digest"})
    finally:
        CURRENT_PRINCIPAL.reset(context)


async def test_snapshot_reads_do_not_mix_a_real_concurrent_generation_or_block_its_commit(store, tmp_path, monkeypatch):
    import asyncio
    import contextvars

    from sqlalchemy import text

    project, manager, journal, campaign, context = await seeded(store, tmp_path)
    try:
        reports = service(store, manager, journal)
        original = reports._run
        run_id = campaign["children"][0]["run_id"]
        await journal.ensure_plan(run_id)
        entered = False

        async def concurrent(run, gate, remaining):
            nonlocal entered
            if not entered:
                entered = True
                async with store._unit() as session:
                    assert await session.scalar(text("SHOW transaction_isolation")) == "repeatable read"
                    assert await session.scalar(text("SHOW transaction_read_only")) == "on"

                async def advance():
                    await journal.intent(run_id, "verify")
                    await store.update(run_id, status="needs_reconciliation")

                await asyncio.wait_for(asyncio.create_task(advance(), context=contextvars.Context()), 2)
            return await original(run, gate, remaining)

        monkeypatch.setattr(reports, "_run", concurrent)
        before = await reports.report(project["id"], campaign["id"])
        assert before["state"] == "in_progress"
        assert next(t for t in before["components"][0]["tasks"] if t["id"] == "verify")["state"] == "not_started"
        after = await reports.report(project["id"], campaign["id"])
        assert after["state"] == "unknown"
        assert next(t for t in after["components"][0]["tasks"] if t["id"] == "verify")["state"] == "in_progress"
        assert before["digest"] != after["digest"]
    finally:
        CURRENT_PRINCIPAL.reset(context)


async def test_credential_admission_occurs_outside_snapshot_and_rechecks_revoked_access(store, tmp_path):
    from pyfly.data.transaction import is_transaction_active

    project, manager, journal, campaign, context = await seeded(store, tmp_path)
    try:
        reports = service(store, manager, journal)
        viewer = Principal(uuid4(), "https://identity.invalid", "owned-revoked-reader")
        await ProjectAccess(store).set_membership(project["id"], viewer, "viewer")
        token = CURRENT_PRINCIPAL.set(viewer)
        try:

            async def revoke(value):
                assert not is_transaction_active(), "Credential admission must not hold the report snapshot open"
                await ProjectAccess(store).set_membership(project["id"], viewer, None)

            store.vault_credential_admission = revoke
            with pytest.raises(AccessDenied):
                await reports.report(project["id"], campaign["id"])
        finally:
            CURRENT_PRINCIPAL.reset(token)
    finally:
        CURRENT_PRINCIPAL.reset(context)


async def test_report_bounds_fail_without_partial_verified_totals(store, tmp_path, monkeypatch):
    import firefly_forge.campaign_reports as module

    project, manager, journal, campaign, context = await seeded(store, tmp_path)
    try:
        monkeypatch.setattr(module, "MAX_EVENTS", 0)
        report = await service(store, manager, journal).report(project["id"], campaign["id"])
        assert report["state"] == "unknown" and report["reason"] == "evidence_limit"
        assert report["counts"] is None and report["components"] == []
    finally:
        CURRENT_PRINCIPAL.reset(context)


async def test_actual_git_checkpoint_and_python_checks_have_exact_task_receipt_references(store, tmp_path):
    import sys

    from firefly_forge.engine import Engine
    from firefly_forge.journal import Journal, digest
    from firefly_forge.models import RepositoryInput
    from firefly_forge.process import checked
    from firefly_forge.project_campaigns import CampaignLaunch, ProjectCampaigns
    from firefly_forge.project_models import ProjectDefinition
    from firefly_forge.projects import Projects

    source = tmp_path / "original"
    source.mkdir()
    (source / "stock.py").write_text("stock = 5\n")
    await checked(["git", "init", "--initial-branch=main"], source)
    await checked(["git", "add", "."], source)
    await checked(
        ["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-m", "Own source"],
        source,
    )
    repo = await store.add_repository(RepositoryInput(name="Owned stock", url=str(source)))
    project = await Projects(store).configure(
        ProjectDefinition(
            name="Stock",
            description="Owned report fixture",
            topology="monorepo",
            owners=["Team"],
            components=[
                dict(
                    id="api",
                    name="API",
                    repository_id=repo["id"],
                    responsibility="Stock",
                    verification=[{"argv": [sys.executable, "-c", "from stock import stock; assert stock == 5"]}],
                )
            ],
        )
    )
    journal = Journal(store)
    engine = Engine(store, tmp_path / "workspaces", journal=journal)

    async def source_base(_repository):
        return await checked(["git", "rev-parse", "HEAD"], source)

    manager = ProjectCampaigns(store, engine, journal, source_resolver=source_base)
    token = CURRENT_PRINCIPAL.set(Principal(uuid4(), "local", "owned-report", authentication_method="local_bootstrap"))
    try:
        campaign = await manager.launch(
            project["id"], CampaignLaunch(request_id=uuid4().hex, brief="Verify stock", operation="test")
        )
        run_id = campaign["children"][0]["run_id"]
        await engine.execute_run(run_id)
        run = await store.get_run(run_id)
        assert run["status"] == "succeeded", run.get("error")
        reports = service(store, manager, journal)
        first = await reports.report(project["id"], campaign["id"])
        assert first["state"] == "verified"
        item = first["components"][0]
        assert item["branch"] == run["branch"] and item["source_tree"] == run["source_tree"]
        assert item["checkpoint_revision"] == run["checkpoint_revision"]
        entries = await journal.entries(run_id)
        by_sequence = {entry["sequence"]: entry for entry in entries}
        assert by_sequence[item["branch_receipt"]["sequence"]]["payload"]["branch"] == item["branch"]
        check = item["checks"][0]
        assert check["exit_code"] == 0
        assert check["command_digest"] == digest({"argv": run["final_verification"][0]["argv"]})
        assert by_sequence[check["receipt"]["sequence"]]["digest"] == check["receipt"]["digest"]
        assert "argv" not in str(first) and str(source) not in str(first)

        async def forbidden(*args, **kwargs):
            pytest.fail("Report GET must never run source resolution, workers or campaign coordination")

        engine.execute_run = forbidden
        manager.source_resolver = forbidden
        manager.tick = forbidden
        assert await reports.report(project["id"], campaign["id"]) == first
        assert (await checked(["git", "status", "--porcelain"], source)) == ""
    finally:
        CURRENT_PRINCIPAL.reset(token)


async def test_historical_repository_denial_precedes_any_credential_admission(store, tmp_path):
    from firefly_forge.models import RepositoryInput
    from firefly_forge.project_models import ProjectDefinition
    from firefly_forge.projects import Projects

    project, manager, journal, campaign, context = await seeded(store, tmp_path)
    try:
        old = project["definition"]["components"][0]["repository_id"]
        new = await store.add_repository(RepositoryInput(name="Changed project", url=str(tmp_path / "new")))
        definition = {
            **project["definition"],
            "product_repository_id": new["id"],
            "components": [{**project["definition"]["components"][0], "repository_id": new["id"]}],
        }
        await Projects(store).configure(ProjectDefinition.model_validate(definition), project["id"])
        await Projects(store).configure(
            ProjectDefinition(
                name="Foreign",
                description="Private original repo",
                topology="monorepo",
                owners=["Team"],
                components=[dict(id="api", name="API", repository_id=old, responsibility="Private")],
            )
        )
        viewer = Principal(uuid4(), "https://identity.invalid", "current-only-reader")
        await ProjectAccess(store).set_membership(project["id"], viewer, "viewer")
        called = []

        async def admit(value):
            called.append(value)

        store.vault_credential_admission = admit
        token = CURRENT_PRINCIPAL.set(viewer)
        try:
            with pytest.raises(AccessDenied):
                await service(store, manager, journal).report(project["id"], campaign["id"])
            assert called == []
        finally:
            CURRENT_PRINCIPAL.reset(token)
    finally:
        CURRENT_PRINCIPAL.reset(context)


@pytest.mark.parametrize("change", ["source_tree", "campaign_status"])
async def test_unreceipted_source_or_campaign_success_is_not_reported_as_known_evidence(store, tmp_path, change):
    project, manager, journal, campaign, context = await seeded(store, tmp_path)
    try:
        if change == "source_tree":
            await store.update(campaign["children"][0]["run_id"], source_tree="a" * 40)
        else:
            async with store._unit():
                row = await manager.records.find_by_id(__import__("uuid").UUID(campaign["id"]))
                row.status = "succeeded"
                await manager.records.save(row)
        report = await service(store, manager, journal).report(project["id"], campaign["id"])
        assert report["state"] == "unknown" and report["reason"] == "evidence_integrity"
        assert report["counts"] is None and report["components"] == []
    finally:
        CURRENT_PRINCIPAL.reset(context)


async def test_final_snapshot_digest_is_also_subject_to_current_credential_admission(store, tmp_path, monkeypatch):
    project, manager, journal, campaign, context = await seeded(store, tmp_path)
    try:
        reports = service(store, manager, journal)
        report = await reports.report(project["id"], campaign["id"])
        monkeypatch.setenv("OWNED_REPORT_API_TOKEN", report["digest"])
        with pytest.raises(ValueError) as error:
            await reports.report(project["id"], campaign["id"])
        assert report["digest"] not in str(error.value)
    finally:
        CURRENT_PRINCIPAL.reset(context)


async def test_snapshot_has_an_aggregate_json_evidence_budget_before_materializing_rows(store, tmp_path, monkeypatch):
    import firefly_forge.campaign_reports as module

    project, manager, journal, campaign, context = await seeded(store, tmp_path)
    try:
        assert hasattr(module, "MAX_EVIDENCE_BYTES"), "Report row limits also require a total evidence byte budget"
        monkeypatch.setattr(module, "MAX_EVIDENCE_BYTES", 1)
        report = await service(store, manager, journal).report(project["id"], campaign["id"])
        assert report["state"] == "unknown" and report["reason"] == "evidence_limit"
        assert report["counts"] is None and report["components"] == []
    finally:
        CURRENT_PRINCIPAL.reset(context)


async def test_explicit_retry_counts_only_current_attempt_and_retains_prior_admission_reference(store, tmp_path):
    from firefly_forge.project_campaigns import CampaignRetry

    project, manager, journal, campaign, context = await seeded(store, tmp_path)
    try:
        first = campaign["children"][0]["run_id"]
        await store.finish(first, "failed", error="Owned fixture verification failed")
        reports = service(store, manager, journal)
        assert (await reports.report(project["id"], campaign["id"]))["counts"] == {"blocked": 1}
        await manager.retry(project["id"], campaign["id"], CampaignRetry(request_id=uuid4().hex, component_ids=["api"]))
        report = await reports.report(project["id"], campaign["id"])
        assert report["counts"] == {"in_progress": 1}
        current = report["components"][0]
        assert current["attempt"] == 2 and current["run_id"] != first
        assert current["prior_attempts"] == [
            {"attempt": 1, "run_id": first, "admission_digest": campaign["children"][0]["admission_digest"]}
        ]
    finally:
        CURRENT_PRINCIPAL.reset(context)


async def test_boolean_exit_code_cannot_turn_synthetic_completion_receipts_into_verified_delivery(store, tmp_path):
    project, manager, journal, campaign, context = await seeded(store, tmp_path)
    try:
        run_id = campaign["children"][0]["run_id"]
        await journal.ensure_plan(run_id)
        checks = [{"argv": ["python", "owned-check"], "exit_code": False}]
        for task, evidence in [
            ("prepare", {"branch": "owned-fixture"}),
            ("verify", {"source_tree": "a" * 40, "checks": checks}),
            ("checkpoint", {"source_tree": "a" * 40, "checkpoint_revision": "b" * 40}),
        ]:
            intent = await journal.intent(run_id, task)
            await journal.receipt(run_id, intent["idempotency_key"], evidence)
        await store.update(
            run_id,
            branch="owned-fixture",
            final_verification=checks,
            source_tree="a" * 40,
            checkpoint_revision="b" * 40,
        )
        await store.finish(run_id, "succeeded")
        report = await service(store, manager, journal).report(project["id"], campaign["id"])
        assert report["state"] == "unknown" and report["counts"] is None
        assert report["reason"] == "evidence_integrity"
    finally:
        CURRENT_PRINCIPAL.reset(context)


async def synthetic_delivery(store, tmp_path):
    project, manager, journal, campaign, context = await seeded(store, tmp_path)
    run_id = campaign["children"][0]["run_id"]
    await journal.ensure_plan(run_id)
    checks = [{"argv": ["python", "owned-synthetic-check"], "exit_code": 0}]
    for task, evidence in [
        ("prepare", {"branch": "owned-fixture"}),
        ("implement", {"source_tree": "a" * 40}),
        ("verify", {"source_tree": "a" * 40, "checks": checks}),
        ("checkpoint", {"source_tree": "a" * 40, "checkpoint_revision": "b" * 40}),
    ]:
        intent = await journal.intent(run_id, task)
        await journal.receipt(run_id, intent["idempotency_key"], evidence)
    await store.update(
        run_id, branch="owned-fixture", final_verification=checks, source_tree="a" * 40, checkpoint_revision="b" * 40
    )
    await store.finish(run_id, "succeeded")
    return project, manager, journal, campaign, context


async def test_done_receipt_is_bound_to_its_exact_current_task_attempt(store, tmp_path):
    from uuid import UUID

    project, manager, journal, campaign, context = await synthetic_delivery(store, tmp_path)
    try:
        reports = service(store, manager, journal)
        assert (await reports.report(project["id"], campaign["id"]))["state"] == "verified"
        async with store._unit():
            task = next(
                row
                for row in await journal.tasks.find_for_run(UUID(campaign["children"][0]["run_id"]))
                if row.task_id == "verify"
            )
            task.attempt = 2
            await journal.tasks.save(task)
        report = await reports.report(project["id"], campaign["id"])
        assert report["state"] == "unknown" and report["counts"] is None
        assert report["reason"] == "evidence_integrity"
    finally:
        CURRENT_PRINCIPAL.reset(context)


async def test_completion_without_a_prior_exact_intent_is_not_receipted(store, tmp_path):
    from uuid import NAMESPACE_URL, UUID, uuid5

    project, manager, journal, campaign, context = await seeded(store, tmp_path)
    try:
        run_id = campaign["children"][0]["run_id"]
        await journal.ensure_plan(run_id)
        key = uuid5(NAMESPACE_URL, f"forge:{run_id}:verify:1").hex
        async with store._unit():
            await journal._run(run_id)
            entry = await journal._append(run_id, "verify", key, "receipt", {"checks": []})
            task = next(row for row in await journal.tasks.find_for_run(UUID(run_id)) if row.task_id == "verify")
            task.status, task.data = "done", {"idempotency_key": key, "evidence": entry["payload"]}
            await journal.tasks.save(task)
        report = await service(store, manager, journal).report(project["id"], campaign["id"])
        assert report["state"] == "unknown" and report["counts"] is None
    finally:
        CURRENT_PRINCIPAL.reset(context)


@pytest.mark.parametrize("status", ["running", "needs_reconciliation"])
async def test_success_cannot_hide_an_actual_started_unknown_effect(store, tmp_path, status):
    from uuid import UUID

    project, manager, journal, campaign, context = await synthetic_delivery(store, tmp_path)
    try:
        run_id = campaign["children"][0]["run_id"]
        await journal.intent(run_id, "memory:owned")
        async with store._unit():
            task = next(row for row in await journal.tasks.find_for_run(UUID(run_id)) if row.task_id == "memory:owned")
            task.status = status
            await journal.tasks.save(task)
        report = await service(store, manager, journal).report(project["id"], campaign["id"])
        assert report["state"] == report["components"][0]["state"] == "unknown"
        assert report["components"][0]["verification"] == "not_verified"
    finally:
        CURRENT_PRINCIPAL.reset(context)


async def test_unstarted_optional_remote_task_does_not_invent_an_unknown_effect(store, tmp_path):
    from uuid import UUID

    from firefly_forge.persistence import ForgeTaskEntity

    project, manager, journal, campaign, context = await synthetic_delivery(store, tmp_path)
    try:
        run_id = campaign["children"][0]["run_id"]
        async with store._unit():
            plan = await journal.plans.find_current(UUID(run_id))
            await journal.tasks.save(
                ForgeTaskEntity(
                    id=uuid4(),
                    run_id=UUID(run_id),
                    plan_id=plan.id,
                    task_id="memory:optional",
                    status="queued",
                    attempt=2,
                    data={"kind": "remote", "idempotency_key": "f" * 32, "evidence": {"old": True}},
                )
            )
        report = await service(store, manager, journal).report(project["id"], campaign["id"])
        assert report["state"] == "verified"
        optional = next(task for task in report["components"][0]["tasks"] if task["id"] == "memory:optional")
        assert optional["state"] == "not_started" and optional["receipt"] is None
    finally:
        CURRENT_PRINCIPAL.reset(context)


async def test_task_from_another_plan_is_not_mixed_into_the_current_snapshot(store, tmp_path):
    from uuid import UUID

    project, manager, journal, campaign, context = await seeded(store, tmp_path)
    try:
        run_id = campaign["children"][0]["run_id"]
        await journal.ensure_plan(run_id)
        _, _, other_journal, other_campaign = await one_component(store, tmp_path)
        other_run = other_campaign["children"][0]["run_id"]
        await other_journal.ensure_plan(other_run)
        async with store._unit():
            other_plan = await journal.plans.find_current(UUID(other_run))
            task = next(row for row in await journal.tasks.find_for_run(UUID(run_id)) if row.task_id == "verify")
            task.plan_id = other_plan.id
            await journal.tasks.save(task)
        report = await service(store, manager, journal).report(project["id"], campaign["id"])
        assert report["state"] == "unknown" and report["counts"] is None
    finally:
        CURRENT_PRINCIPAL.reset(context)
