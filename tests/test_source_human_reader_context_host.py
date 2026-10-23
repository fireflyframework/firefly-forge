# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Exercise the real source invocation context after each sealed human renewal."""

# ruff: noqa: F401, F811
from types import SimpleNamespace
from uuid import NAMESPACE_URL, uuid4, uuid5

import pytest
from test_source_edit_continuations_host import controlled_metadata
from test_source_human_readmission_host import answer_successor, answered_source, human_request
from test_workspace_dependency_reconciliation import legacy_workspace
from test_workspace_reconciliation_journal import environment_journal
from test_workspace_reconciliation_service import reconciliation_service

from firefly_forge.source_edit_continuations import SourceEditInvocation
from firefly_forge.source_edit_model_catalog import model_catalog_path
from firefly_forge.source_edit_models import SourceReadRequest
from firefly_forge.source_human_readmission import SourceHumanReadmissions


async def test_human_renewal_preserves_actual_reader_context_for_attempts_three_and_four(
    reconciliation_service, tmp_path, monkeypatch
):
    env, parent = await answered_source(reconciliation_service, tmp_path, monkeypatch, observed=True, sanitized=True)
    service = SourceHumanReadmissions(env.service)
    path = str(model_catalog_path("fixture-model"))
    invoked = []

    async def invoke(run_id, task_id, tool, correlation_id, arguments, callback, **kwargs):
        assert kwargs["source"]["head"] == env.store.run.data["base_revision"]
        await kwargs["validate_admission"]()
        result = await callback()
        guard = await kwargs["validate_result"](result)
        guard()
        invoked.append(tool)
        return result

    async def unresolved(_run_id):
        return False

    env.recoveries.engine.tool_broker = SimpleNamespace(invoke=invoke, unresolved=unresolved)
    for attempt in (3, 4):
        review = await service.candidate(env.root.name, parent["request"]["request_id"], path)
        receipt = await service.admit(env.root.name, parent["request"]["request_id"], human_request(review, path))
        key = uuid5(NAMESPACE_URL, f"forge:{env.root.name}:implement:{attempt}").hex
        env.store.run.status = "developing"
        env.store.run.data.update(status="developing")
        env.task.status = "running"
        env.task.data["idempotency_key"] = key
        result = await SourceEditInvocation(env.recoveries, env.root.name, key).run(
            SourceReadRequest(tool="list_files", request_id=uuid4().hex)
        )
        assert "frontend/app.js" in result["files"]
        assert result["source_digest"] == receipt["completed_cut"]["source_digest"]
        if attempt == 3:
            await answer_successor(env, 3)
        parent = receipt
    assert invoked == ["list_files", "list_files"]
