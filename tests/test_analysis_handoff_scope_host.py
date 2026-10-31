# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Reviewed-source selection cardinality and actual snapshot coverage contracts."""

import copy
import hashlib
import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from firefly_forge.analysis_context import AnalysisContextInput, ReviewedAnalysisContext
from firefly_forge.product_assistant import AssistantSessionInput


@pytest.mark.parametrize("count", [51, 60, 5000])
def test_reviewed_handoff_preserves_explicit_component_scope_without_a_count_ceiling(count):
    ids = [f"component-{index}" for index in range(count)]
    context = AnalysisContextInput(digest="d" * 64, component_ids=ids)
    session = AssistantSessionInput(
        project_id="a" * 32,
        analysis_id="b" * 32,
        analysis_digest=context.digest,
        analysis_component_ids=ids,
    )
    assert context.component_ids == session.analysis_component_ids == ids


def test_handoff_keeps_unique_selection_and_exact_reviewed_identity_guards():
    with pytest.raises(ValidationError, match="unique"):
        AnalysisContextInput(digest="d" * 64, component_ids=["stock", "stock"])
    with pytest.raises(ValidationError, match="logical project"):
        AssistantSessionInput(analysis_id="b" * 32, analysis_digest="d" * 64)


def reviewed_context(count):
    components = [
        {
            "id": f"component-{index}",
            "name": f"API {index}",
            "repository_id": "a" * 32,
            "path": f"api/{index}",
            "commit": "c" * 40,
            "facts": [],
            "skipped_files": 0,
            "execution": "source_only",
        }
        for index in range(count)
    ]
    project = {
        "id": "b" * 32,
        "digest": "d" * 64,
        "definition": {"components": components},
    }
    report = {
        "id": "e" * 32,
        "status": "reviewed",
        "reviewed": True,
        "project_snapshot": project,
        "component_ids": [item["id"] for item in components],
        "components": components,
        "reviewed_at": "2026-10-31T00:00:00Z",
        "open_questions": [],
    }
    report["digest"] = hashlib.sha256(
        json.dumps({"project_digest": project["digest"], "components": components}, sort_keys=True).encode()
    ).hexdigest()

    async def analysis(project_id, analysis_id):
        assert project_id == project["id"] and analysis_id == report["id"]
        return copy.deepcopy(report)

    async def current(project_id):
        assert project_id == project["id"]
        return copy.deepcopy(project)

    return (
        ReviewedAnalysisContext(SimpleNamespace(analysis=analysis), SimpleNamespace(project=current)),
        project,
        report,
    )


async def test_sixty_reviewed_components_can_enter_a_valid_frozen_context_without_truncating_ids():
    helper, project, report = reviewed_context(60)
    request = AnalysisContextInput(digest=report["digest"], component_ids=list(reversed(report["component_ids"])))
    snapshot = await helper.snapshot(project["id"], report["id"], request)
    assert [item["id"] for item in snapshot["components"]] == report["component_ids"]
    assert snapshot["included_facts"] == snapshot["omitted_facts"] == 0
    assert snapshot["truncated"] is False
    helper.validate_frozen(snapshot)


async def test_large_metadata_exceeds_the_disclosed_context_budget_instead_of_dropping_selected_components():
    helper, project, report = reviewed_context(5000)
    request = AnalysisContextInput(digest=report["digest"], component_ids=report["component_ids"])
    with pytest.raises(ValueError, match="metadata exceeds 40 KB; select fewer components"):
        await helper.snapshot(project["id"], report["id"], request)
