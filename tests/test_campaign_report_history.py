# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Explicit report archives retain one bounded evidence cut without executing work."""

from uuid import uuid4

import pytest
from test_campaign_reports import seeded, service

from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied, ProjectAccess


def history_service(store, reports):
    from firefly_forge.campaign_report_history import CampaignReportHistory
    from firefly_forge.campaign_report_history_persistence import ForgeCampaignReportArchiveRepository

    return CampaignReportHistory(store, reports, ForgeCampaignReportArchiveRepository())


def archive_input(report, request_id=None):
    from firefly_forge.campaign_report_history import CampaignReportArchiveInput

    return CampaignReportArchiveInput(request_id=request_id or uuid4().hex, expected_report_digest=report["digest"])


async def test_explicit_archive_and_exact_read_survive_lost_response_without_changing_live_report(store, tmp_path):
    project, manager, journal, campaign, token = await seeded(store, tmp_path)
    try:
        reports = service(store, manager, journal)
        before = await reports.report(project["id"], campaign["id"])
        from firefly_forge.campaign_report_history import CampaignReportArchiveInput, CampaignReportHistory
        from firefly_forge.campaign_report_history_persistence import ForgeCampaignReportArchiveRepository

        history = CampaignReportHistory(store, reports, ForgeCampaignReportArchiveRepository())
        request = CampaignReportArchiveInput(request_id=uuid4().hex, expected_report_digest=before["digest"])
        archived = await history.archive(project["id"], campaign["id"], request)
        assert archived["report"] == before
        assert archived["source"]["campaign_journal_head"] == before["campaign"]["journal_head"]
        assert archived["repositories"] == [project["definition"]["components"][0]["repository_id"]]
        assert await history.get(project["id"], campaign["id"], request.request_id) == archived
        assert await history.archive(project["id"], campaign["id"], request) == archived
        assert await reports.report(project["id"], campaign["id"]) == before
        assert len((await history.list(project["id"], campaign["id"]))["items"]) == 1
    finally:
        CURRENT_PRINCIPAL.reset(token)


async def test_archive_is_historical_after_live_cut_advances_and_exact_get_does_not_recompute(
    store, tmp_path, monkeypatch
):
    project, manager, journal, campaign, token = await seeded(store, tmp_path)
    try:
        reports = service(store, manager, journal)
        report = await reports.report(project["id"], campaign["id"])
        history = history_service(store, reports)
        request = archive_input(report)
        saved = await history.archive(project["id"], campaign["id"], request)
        await store.finish(campaign["children"][0]["run_id"], "failed", error="Fictional check failed")
        assert (await reports.report(project["id"], campaign["id"]))["digest"] != report["digest"]

        async def no_snapshot(*args):
            pytest.fail("Retained reads must not regenerate a report")

        monkeypatch.setattr(reports, "_capture", no_snapshot)
        assert await history.get(project["id"], campaign["id"], request.request_id) == saved
        assert await history.archive(project["id"], campaign["id"], request) == saved
    finally:
        CURRENT_PRINCIPAL.reset(token)


async def test_digest_drift_and_identity_conflicts_refuse_without_overwriting(store, tmp_path):
    project, manager, journal, campaign, token = await seeded(store, tmp_path)
    try:
        reports = service(store, manager, journal)
        history = history_service(store, reports)
        report = await reports.report(project["id"], campaign["id"])
        request = archive_input(report)
        saved = await history.archive(project["id"], campaign["id"], request)
        await store.finish(campaign["children"][0]["run_id"], "failed", error="Fictional check failed")
        changed = await reports.report(project["id"], campaign["id"])
        with pytest.raises(ValueError):
            await history.archive(project["id"], campaign["id"], archive_input(report))
        with pytest.raises(ValueError):
            await history.archive(project["id"], campaign["id"], archive_input(changed, request.request_id))
        assert await history.get(project["id"], campaign["id"], request.request_id) == saved
        assert len((await history.list(project["id"], campaign["id"]))["items"]) == 1
    finally:
        CURRENT_PRINCIPAL.reset(token)


@pytest.mark.parametrize("operation", ["UPDATE", "DELETE"])
async def test_native_database_refuses_archive_mutation(store, tmp_path, operation):
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    project, manager, journal, campaign, token = await seeded(store, tmp_path)
    try:
        reports = service(store, manager, journal)
        history = history_service(store, reports)
        report = await reports.report(project["id"], campaign["id"])
        request = archive_input(report)
        saved = await history.archive(project["id"], campaign["id"], request)
        statement = (
            "UPDATE forge_campaign_report_archives SET data=data"
            if operation == "UPDATE"
            else "DELETE FROM forge_campaign_report_archives"
        )
        with pytest.raises(DBAPIError):
            async with store._unit() as session:
                await session.execute(text(statement))
        assert await history.get(project["id"], campaign["id"], request.request_id) == saved
    finally:
        CURRENT_PRINCIPAL.reset(token)


async def test_concurrent_same_identity_retains_one_receipt_and_unique_ids_obey_quota(store, tmp_path, monkeypatch):
    import asyncio

    import firefly_forge.campaign_report_history as module

    project, manager, journal, campaign, token = await seeded(store, tmp_path)
    try:
        reports = service(store, manager, journal)
        history = history_service(store, reports)
        report = await reports.report(project["id"], campaign["id"])
        request = archive_input(report)
        results = await asyncio.gather(*(history.archive(project["id"], campaign["id"], request) for _ in range(2)))
        assert results[0] == results[1]
        monkeypatch.setattr(module, "MAX_ARCHIVES", 2)
        results = await asyncio.gather(
            *(history.archive(project["id"], campaign["id"], archive_input(report)) for _ in range(2)),
            return_exceptions=True,
        )
        assert sum(isinstance(item, ValueError) for item in results) == 1
        assert len((await history.list(project["id"], campaign["id"]))["items"]) == 2
    finally:
        CURRENT_PRINCIPAL.reset(token)


async def test_history_paginates_summaries_without_report_bodies(store, tmp_path, monkeypatch):
    import firefly_forge.campaign_report_history as module

    project, manager, journal, campaign, token = await seeded(store, tmp_path)
    try:
        reports = service(store, manager, journal)
        history = history_service(store, reports)
        report = await reports.report(project["id"], campaign["id"])
        saved = [await history.archive(project["id"], campaign["id"], archive_input(report)) for _ in range(3)]
        monkeypatch.setattr(module, "PAGE_SIZE", 2)
        first = await history.list(project["id"], campaign["id"])
        second = await history.list(project["id"], campaign["id"], after=first["next_after"])
        assert [item["request_id"] for item in first["items"] + second["items"]] == [s["request_id"] for s in saved]
        assert first["next_after"] == 2 and second["next_after"] is None
        assert all("report" not in item for item in first["items"])
    finally:
        CURRENT_PRINCIPAL.reset(token)


@pytest.mark.parametrize("unavailable", [False, True])
async def test_coherent_unknown_archives_but_integrity_unavailable_does_not(store, tmp_path, unavailable):
    project, manager, journal, campaign, token = await seeded(store, tmp_path)
    try:
        reports = service(store, manager, journal)
        history = history_service(store, reports)
        if unavailable:
            await store.update(campaign["children"][0]["run_id"], source_tree="a" * 40)
        else:
            await store.finish(campaign["children"][0]["run_id"], "succeeded")
        report = await reports.report(project["id"], campaign["id"])
        assert report["state"] == "unknown"
        if unavailable:
            assert report["counts"] is None
            with pytest.raises(ValueError):
                await history.archive(project["id"], campaign["id"], archive_input(report))
            assert (await history.list(project["id"], campaign["id"]))["items"] == []
        else:
            saved = await history.archive(project["id"], campaign["id"], archive_input(report))
            assert saved["report"] == report and saved["report"]["state"] == "unknown"
    finally:
        CURRENT_PRINCIPAL.reset(token)


async def test_viewer_cannot_archive_and_revocation_during_credentials_prevents_insert(store, tmp_path):
    from pyfly.data.transaction import is_transaction_active

    from firefly_forge.identity import Principal

    project, manager, journal, campaign, token = await seeded(store, tmp_path)
    try:
        reports = service(store, manager, journal)
        history = history_service(store, reports)
        report = await reports.report(project["id"], campaign["id"])
        viewer = Principal(uuid4(), "https://identity.invalid", "archive-reader")
        access = ProjectAccess(store)
        await access.set_membership(project["id"], viewer, "viewer")
        actor = CURRENT_PRINCIPAL.set(viewer)
        try:
            with pytest.raises(AccessDenied):
                await history.archive(project["id"], campaign["id"], archive_input(report))
            await access.set_membership(project["id"], viewer, "developer")

            async def revoke(value):
                assert not is_transaction_active()
                await access.set_membership(project["id"], viewer, None)

            store.vault_credential_admission = revoke
            with pytest.raises(AccessDenied):
                await history.archive(project["id"], campaign["id"], archive_input(report))
        finally:
            CURRENT_PRINCIPAL.reset(actor)
        store.vault_credential_admission = None
        assert (await history.list(project["id"], campaign["id"]))["items"] == []
    finally:
        CURRENT_PRINCIPAL.reset(token)


@pytest.mark.parametrize("read", ["get", "list", "replay"])
async def test_newly_registered_credential_blocks_retained_reads(store, tmp_path, monkeypatch, read):
    project, manager, journal, campaign, token = await seeded(store, tmp_path)
    try:
        reports = service(store, manager, journal)
        history = history_service(store, reports)
        report = await reports.report(project["id"], campaign["id"])
        request = archive_input(report)
        saved = await history.archive(project["id"], campaign["id"], request)
        monkeypatch.setenv("OWNED_ARCHIVE_API_TOKEN", saved["digest"])
        with pytest.raises(ValueError) as error:
            if read == "get":
                await history.get(project["id"], campaign["id"], request.request_id)
            elif read == "list":
                await history.list(project["id"], campaign["id"])
            else:
                await history.archive(project["id"], campaign["id"], request)
        assert saved["digest"] not in str(error.value)
    finally:
        CURRENT_PRINCIPAL.reset(token)


async def test_different_ids_with_same_prepared_sequence_have_one_explicit_zero_write_conflict(store, tmp_path):
    import asyncio

    project, manager, journal, campaign, token = await seeded(store, tmp_path)
    try:
        reports = service(store, manager, journal)
        history = history_service(store, reports)
        report = await reports.report(project["id"], campaign["id"])
        arrived = 0
        gate = asyncio.Event()

        async def admission(value):
            nonlocal arrived
            if "receipt" in value:
                arrived += 1
                if arrived == 2:
                    gate.set()
                await asyncio.wait_for(gate.wait(), 2)

        store.vault_credential_admission = admission
        results = await asyncio.gather(
            *(history.archive(project["id"], campaign["id"], archive_input(report)) for _ in range(2)),
            return_exceptions=True,
        )
        refused = [item for item in results if isinstance(item, ValueError)]
        assert len(refused) == 1 and "history advanced" in str(refused[0])
        assert len((await history.list(project["id"], campaign["id"]))["items"]) == 1
    finally:
        CURRENT_PRINCIPAL.reset(token)


async def test_manifest_is_captured_with_the_report_before_later_project_changes(store, tmp_path):
    from firefly_forge.models import RepositoryInput
    from firefly_forge.project_models import ProjectDefinition
    from firefly_forge.projects import Projects

    project, manager, journal, campaign, token = await seeded(store, tmp_path)
    try:
        reports = service(store, manager, journal)
        history = history_service(store, reports)
        report = await reports.report(project["id"], campaign["id"])
        original_repo = project["definition"]["components"][0]["repository_id"]
        replacement = await store.add_repository(RepositoryInput(name="New source", url=str(tmp_path / "new")))
        changed = False

        async def advance(value):
            nonlocal changed
            if "receipt" in value and not changed:
                changed = True
                await Projects(store).configure(
                    ProjectDefinition.model_validate(
                        {
                            **project["definition"],
                            "product_repository_id": replacement["id"],
                            "components": [
                                {**project["definition"]["components"][0], "repository_id": replacement["id"]}
                            ],
                        }
                    ),
                    project["id"],
                )

        store.vault_credential_admission = advance
        saved = await history.archive(project["id"], campaign["id"], archive_input(report))
        assert saved["report"] == report and saved["repositories"] == [original_repo]
        assert replacement["id"] not in saved["repositories"]
    finally:
        CURRENT_PRINCIPAL.reset(token)


async def test_all_historical_repository_access_precedes_retained_credential_read(store, tmp_path):
    from firefly_forge.identity import Principal
    from firefly_forge.models import RepositoryInput
    from firefly_forge.project_models import ProjectDefinition
    from firefly_forge.projects import Projects

    project, manager, journal, campaign, token = await seeded(store, tmp_path)
    try:
        reports = service(store, manager, journal)
        history = history_service(store, reports)
        saved = await history.archive(
            project["id"], campaign["id"], archive_input(await reports.report(project["id"], campaign["id"]))
        )
        original = project["definition"]["components"][0]["repository_id"]
        new = await store.add_repository(RepositoryInput(name="Changed", url=str(tmp_path / "new")))
        await Projects(store).configure(
            ProjectDefinition.model_validate(
                {
                    **project["definition"],
                    "product_repository_id": new["id"],
                    "components": [{**project["definition"]["components"][0], "repository_id": new["id"]}],
                }
            ),
            project["id"],
        )
        await Projects(store).configure(
            ProjectDefinition(
                name="Foreign old repository",
                description="Private prior source",
                topology="monorepo",
                owners=["Team"],
                components=[dict(id="api", name="API", repository_id=original, responsibility="Original")],
            )
        )
        reader = Principal(uuid4(), "https://identity.invalid", "current-project-only")
        await ProjectAccess(store).set_membership(project["id"], reader, "viewer")
        calls = []

        async def credentials(value):
            calls.append(value)

        store.vault_credential_admission = credentials
        actor = CURRENT_PRINCIPAL.set(reader)
        try:
            with pytest.raises(AccessDenied):
                await history.get(project["id"], campaign["id"], saved["request_id"])
            with pytest.raises(AccessDenied):
                await history.list(project["id"], campaign["id"])
            assert calls == []
        finally:
            CURRENT_PRINCIPAL.reset(actor)
    finally:
        CURRENT_PRINCIPAL.reset(token)


@pytest.mark.parametrize("read", ["get", "list"])
async def test_current_reader_revocation_during_credentials_denies_retained_output(store, tmp_path, read):
    from firefly_forge.identity import Principal

    project, manager, journal, campaign, token = await seeded(store, tmp_path)
    try:
        reports = service(store, manager, journal)
        history = history_service(store, reports)
        saved = await history.archive(
            project["id"], campaign["id"], archive_input(await reports.report(project["id"], campaign["id"]))
        )
        viewer = Principal(uuid4(), "https://identity.invalid", "revoked-reader")
        await ProjectAccess(store).set_membership(project["id"], viewer, "viewer")
        actor = CURRENT_PRINCIPAL.set(viewer)
        try:

            async def revoke(value):
                await ProjectAccess(store).set_membership(project["id"], viewer, None)

            store.vault_credential_admission = revoke
            with pytest.raises(AccessDenied):
                if read == "get":
                    await history.get(project["id"], campaign["id"], saved["request_id"])
                else:
                    await history.list(project["id"], campaign["id"])
        finally:
            CURRENT_PRINCIPAL.reset(actor)
    finally:
        CURRENT_PRINCIPAL.reset(token)


async def test_upgrade_from_revision24_adds_immutable_archive_without_changing_campaign(store, tmp_path):
    from sqlalchemy import text

    from firefly_forge.migrations import SCHEMA_REVISION, migrate

    project, manager, journal, campaign, token = await seeded(store, tmp_path)
    try:
        assert SCHEMA_REVISION == 25
        before = await manager.campaign(project["id"], campaign["id"])
        async with store._unit() as session:
            await session.execute(text("DROP TABLE forge_campaign_report_archives"))
            await session.execute(text("DELETE FROM forge_schema_migrations WHERE revision=25"))
            await session.execute(
                text("INSERT INTO forge_schema_migrations(revision) VALUES (24) ON CONFLICT DO NOTHING")
            )
            await migrate(session, store.schema)
            assert await session.scalar(text("SELECT max(revision) FROM forge_schema_migrations")) == 25
        reports = service(store, manager, journal)
        history = history_service(store, reports)
        report = await reports.report(project["id"], campaign["id"])
        assert (await history.archive(project["id"], campaign["id"], archive_input(report)))["report"] == report
        assert await manager.campaign(project["id"], campaign["id"]) == before
    finally:
        CURRENT_PRINCIPAL.reset(token)


@pytest.mark.parametrize("limit", ["envelope", "stored", "history"])
async def test_byte_bounds_refuse_before_unbounded_archive_materialization(store, tmp_path, monkeypatch, limit):
    import firefly_forge.campaign_report_history as module

    project, manager, journal, campaign, token = await seeded(store, tmp_path)
    try:
        reports = service(store, manager, journal)
        history = history_service(store, reports)
        report = await reports.report(project["id"], campaign["id"])
        request = archive_input(report)
        if limit != "envelope":
            await history.archive(project["id"], campaign["id"], request)
        monkeypatch.setattr(module, "MAX_ARCHIVE_BYTES", 1)
        with pytest.raises(ValueError, match="byte bound"):
            if limit == "envelope":
                await history.archive(project["id"], campaign["id"], request)
            elif limit == "stored":
                await history.get(project["id"], campaign["id"], request.request_id)
            else:
                await history.list(project["id"], campaign["id"])
    finally:
        CURRENT_PRINCIPAL.reset(token)


async def test_full_envelope_and_summary_digests_are_admitted_before_native_insert_without_locks(store, tmp_path):
    from pyfly.data.transaction import is_transaction_active
    from sqlalchemy import func, select

    from firefly_forge.campaign_report_history_persistence import ForgeCampaignReportArchiveEntity
    from firefly_forge.journal import digest

    project, manager, journal, campaign, token = await seeded(store, tmp_path)
    try:
        reports = service(store, manager, journal)
        history = history_service(store, reports)
        report = await reports.report(project["id"], campaign["id"])
        admitted = []

        async def credentials(value):
            assert not is_transaction_active()
            if "receipt" in value:
                async with store._unit() as session:
                    assert await session.scalar(select(func.count()).select_from(ForgeCampaignReportArchiveEntity)) == 0
                assert value["receipt"]["digest"] == digest(
                    {k: v for k, v in value["receipt"].items() if k != "digest"}
                )
                admitted.append(value)

        store.vault_credential_admission = credentials
        saved = await history.archive(project["id"], campaign["id"], archive_input(report))
        assert len(admitted) == 1 and admitted[0]["receipt"] == saved
        assert "prepared_at" in saved and "archived_at" not in saved
    finally:
        CURRENT_PRINCIPAL.reset(token)


@pytest.mark.parametrize("change", ["sequence", "request_id", "sentinel"])
async def test_list_binds_each_summary_and_sentinel_to_its_scalar_row_identity(store, tmp_path, monkeypatch, change):
    from sqlalchemy import text

    import firefly_forge.campaign_report_history as module
    from firefly_forge.journal import digest

    project, manager, journal, campaign, token = await seeded(store, tmp_path)
    try:
        reports = service(store, manager, journal)
        history = history_service(store, reports)
        report = await reports.report(project["id"], campaign["id"])
        first = await history.archive(project["id"], campaign["id"], archive_input(report))
        target = first
        if change == "sentinel":
            target = await history.archive(project["id"], campaign["id"], archive_input(report))
            monkeypatch.setattr(module, "PAGE_SIZE", 1)
        async with store._unit() as session:
            row = await history.records.request(__import__("uuid").UUID(campaign["id"]), target["request_id"])
            summary = dict(row.summary)
            if change == "sequence":
                summary["sequence"] += 10
            elif change == "request_id":
                summary["request_id"] = uuid4().hex
            else:
                summary["campaign_id"] = uuid4().hex
            summary["digest"] = digest({k: v for k, v in summary.items() if k != "digest"})
            # Owned-schema counterexample: bypass the guard solely to qualify read-side integrity.
            await session.execute(
                text(
                    "ALTER TABLE forge_campaign_report_archives "
                    "DISABLE TRIGGER forge_campaign_report_archives_immutable"
                )
            )
            row.summary = summary
            await history.records.save(row)
            await session.flush()
            await session.execute(
                text(
                    "ALTER TABLE forge_campaign_report_archives ENABLE TRIGGER forge_campaign_report_archives_immutable"
                )
            )
        with pytest.raises(ValueError, match="integrity"):
            await history.list(project["id"], campaign["id"])
    finally:
        CURRENT_PRINCIPAL.reset(token)


@pytest.mark.parametrize("existing", [True, False])
async def test_same_id_post_rechecks_execute_after_retained_credentials_await(store, tmp_path, existing):
    from firefly_forge.identity import Principal

    project, manager, journal, campaign, token = await seeded(store, tmp_path)
    try:
        reports = service(store, manager, journal)
        history = history_service(store, reports)
        report = await reports.report(project["id"], campaign["id"])
        actor = Principal(uuid4(), "https://identity.invalid", "archive-developer")
        await ProjectAccess(store).set_membership(project["id"], actor, "developer")
        acting = CURRENT_PRINCIPAL.set(actor)
        try:
            request = archive_input(report)
            saved = await history.archive(project["id"], campaign["id"], request) if existing else None
            called = []

            async def demote(value):
                if "report" in value:
                    called.append(True)
                    await ProjectAccess(store).set_membership(project["id"], actor, "viewer")

            store.vault_credential_admission = demote
            with pytest.raises(AccessDenied):
                await history.archive(project["id"], campaign["id"], request)
            assert called == [True]
            store.vault_credential_admission = None
            exact = await history.get(project["id"], campaign["id"], request.request_id)
            assert exact["report"] == report
            if existing:
                assert exact == saved
            assert len((await history.list(project["id"], campaign["id"]))["items"]) == 1
        finally:
            CURRENT_PRINCIPAL.reset(acting)
    finally:
        CURRENT_PRINCIPAL.reset(token)
