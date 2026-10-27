# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Live progress derives from native rows without charging or rewriting custody."""

import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from test_framework_knowledge_service_host import host_refresh_case

from firefly_forge.framework_knowledge_models import FrameworkRefreshCancelInput
from firefly_forge.framework_knowledge_persistence import FrameworkKnowledgeRecords
from firefly_forge.framework_knowledge_worker import FrameworkKnowledgeWorker


async def partial(tmp_path):
    manager, remote = await host_refresh_case(tmp_path)

    async def second_post(payload):
        if len(remote.posts) == 1:
            raise httpx.ReadTimeout("Retain second source outcome")

    remote.before_post = second_post
    await FrameworkKnowledgeWorker(manager).advance("f" * 32)
    return manager, remote


async def test_active_refresh_and_status_show_durable_ready_parts_without_ledger_writes(tmp_path):
    manager, remote = await partial(tmp_path)
    before = copy.deepcopy({key: row.data for key, row in manager.records.rows.items()})
    stored = await manager._get("operation", "f" * 32)
    assert stored["public"]["counts"]["ready_parts"] == 0

    async def no_pages(*args, **kwargs):
        raise AssertionError("Progress must not page or rescan per part")

    manager.records.page = no_pages
    observed = await manager.get_refresh("pyfly", "f" * 32)
    assert observed["counts"]["ready_parts"] == 1
    assert observed["counts"]["parts"] == 5 and observed["counts"]["total_files"] == 5
    assert observed["uncertain_sources"] == 1
    assert observed["spent_budget"] == stored["public"]["spent_budget"]
    assert observed["revision"] == stored["public"]["revision"]
    status = await manager._status("pyfly")
    assert status["active_operation"]["counts"] == observed["counts"]
    assert {key: row.data for key, row in manager.records.rows.items()} == before
    assert len(remote.posts) == 1


async def test_cancelled_progress_and_observation_preserve_historical_response_without_queries(tmp_path):
    manager, _ = await partial(tmp_path)
    stored = await manager._get("operation", "f" * 32)
    cancelled = await manager.cancel(
        "pyfly", "f" * 32, FrameworkRefreshCancelInput(version=1, expected_revision=stored["public"]["revision"])
    )
    rows = copy.deepcopy({key: row.data for key, row in manager.records.rows.items()})

    async def forbidden(*args):
        raise AssertionError("Terminal custody needs no progress scan")

    manager.records.progress_counts = forbidden
    assert await manager.get_refresh("pyfly", "f" * 32) == cancelled
    assert await manager.observe("pyfly", "f" * 32) == cancelled
    assert {key: row.data for key, row in manager.records.rows.items()} == rows


async def test_native_progress_query_count_is_bounded_and_scope_is_parameterized():
    session = SimpleNamespace(
        execute=AsyncMock(
            side_effect=[
                SimpleNamespace(
                    mappings=lambda: SimpleNamespace(
                        one=lambda: {
                            "total_files": 10000,
                            "files": 9998,
                            "excluded_files": 2,
                            "documents": 6000,
                            "examples": 2000,
                            "api": 1996,
                            "bytes": 123456,
                        }
                    )
                ),
                SimpleNamespace(
                    mappings=lambda: SimpleNamespace(
                        one=lambda: {"parts": 12000, "ready_parts": 7777, "excluded_parts": 3}
                    )
                ),
            ]
        )
    )
    records = FrameworkKnowledgeRecords(session=session)
    reference = "scope'; SELECT forbidden"
    result = await records.progress_counts(reference)
    assert result["ready_parts"] == 7777 and result["parts"] == 12000 and result["excluded_parts"] == 3
    assert session.execute.await_count == 2
    for call in session.execute.await_args_list:
        statement = call.args[0]
        assert reference not in str(statement)
        assert reference in statement.compile().params.values()


@pytest.mark.parametrize("entrypoint", ["get_refresh", "status", "check_updates"])
async def test_revoked_actor_during_progress_is_rejected_before_response(tmp_path, entrypoint):
    from firefly_forge.framework_knowledge_models import FrameworkCheckUpdatesInput
    from firefly_forge.project_access import AccessDenied

    manager, _ = await partial(tmp_path)
    original = manager.records.progress_counts
    revoked = False
    rows_at_revocation = None

    async def revoke_during_counts(reference):
        nonlocal revoked, rows_at_revocation
        counts = await original(reference)
        revoked = True
        rows_at_revocation = copy.deepcopy({key: row.data for key, row in manager.records.rows.items()})
        return counts

    async def authorize(*args, **kwargs):
        if revoked:
            raise AccessDenied("The current native authority was revoked during progress")
        return {"user_id": "a" * 32, "global_admin": True}

    manager.records.progress_counts = revoke_during_counts
    manager._authorize = authorize
    with pytest.raises(AccessDenied, match="revoked during progress"):
        if entrypoint == "get_refresh":
            await manager.get_refresh("pyfly", "f" * 32)
        elif entrypoint == "status":
            await manager.status()
        else:
            await manager.check_updates("pyfly", FrameworkCheckUpdatesInput())
    assert revoked and rows_at_revocation is not None
    assert {key: row.data for key, row in manager.records.rows.items()} == rows_at_revocation
