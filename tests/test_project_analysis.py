# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0

import asyncio

import pytest

from firefly_forge.models import RepositoryInput
from firefly_forge.process import checked
from firefly_forge.project_models import ProjectDefinition
from firefly_forge.projects import Projects
from firefly_forge.reverse_engineering import AnalysisInput, AnalysisReview, ReverseEngineering


async def existing_project(store, tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    marker = tmp_path / "executed-source"
    (source / "api.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('executed')\n"
        "@get_mapping('/inventory/{sku}')\nasync def inventory(sku):\n    return {}\n"
    )
    (source / ".env").write_text("TOKEN=private-value-must-never-appear\n")
    (source / "external.py").symlink_to("/etc/passwd")
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
            "Existing product",
        ],
        source,
    )
    repo = await store.add_repository(RepositoryInput(name="Inventory", url=str(source)))
    project = await Projects(store).configure(
        ProjectDefinition(
            name="Inventory",
            description="Existing stock service",
            topology="monorepo",
            owners=["Inventory team"],
            components=[
                {"id": "stock", "name": "Stock", "repository_id": repo["id"], "responsibility": "Manage stock"}
            ],
        )
    )
    return source, marker, repo, project


async def test_as_is_analysis_reads_real_source_with_citations_and_never_executes_it(store, tmp_path):
    source, marker, repo, project = await existing_project(store, tmp_path)
    manager = ReverseEngineering(store, Projects(store), tmp_path / "analysis")
    report = await manager.analyze(project["id"], AnalysisInput())
    assert report["status"] == "ready_for_review" and report["reviewed"] is False
    assert report["project_snapshot"]["digest"] == project["digest"]
    evidence = report["components"][0]
    assert evidence["repository_id"] == repo["id"]
    assert evidence["commit"] == await checked(["git", "rev-parse", "HEAD"], source)
    fact = next(item for item in evidence["facts"] if item["kind"] == "http_route")
    assert fact["value"] == "/inventory/{sku}" and fact["source"]["path"] == "api.py"
    assert fact["source"]["line"] == 3 and "@get_mapping" in fact["source"]["excerpt"]
    assert not marker.exists()
    assert "private-value" not in str(report) and "root:" not in str(report)
    assert report["open_questions"] and "AS-IS" in report["documents"]["functional-as-is.md"]
    assert (await manager.analysis(project["id"], report["id"])) == report


async def test_analysis_is_project_scoped_and_does_not_claim_business_journeys_from_routes(store, tmp_path):
    _, _, _, project = await existing_project(store, tmp_path)
    manager = ReverseEngineering(store, Projects(store), tmp_path / "analysis")
    report = await manager.analyze(project["id"], AnalysisInput())
    assert report["functional_definition"] is None
    with pytest.raises(KeyError):
        await manager.analysis("f" * 32, report["id"])
    with pytest.raises(ValueError, match="component"):
        await manager.analyze(project["id"], AnalysisInput(component_ids=["invented"]))


async def test_source_review_requires_the_exact_evidence_digest(store, tmp_path):
    _, _, _, project = await existing_project(store, tmp_path)
    manager = ReverseEngineering(store, Projects(store), tmp_path / "analysis")
    report = await manager.analyze(project["id"], AnalysisInput())
    with pytest.raises(ValueError, match="digest"):
        await manager.review(project["id"], report["id"], AnalysisReview(digest="f" * 64))
    reviewed = await manager.review(project["id"], report["id"], AnalysisReview(digest=report["digest"]))
    assert reviewed["reviewed"] is True and reviewed["status"] == "reviewed"
    assert reviewed["functional_definition"] is None
    assert reviewed["history"][-1]["status"] == "reviewed"


async def test_stale_analysis_recovery_preserves_known_source_inspection_receipt(store, tmp_path, monkeypatch):
    _, _, _, project = await existing_project(store, tmp_path)
    manager = ReverseEngineering(store, Projects(store), tmp_path / "analysis")
    entered, release = asyncio.Event(), asyncio.Event()
    inspect = manager._inspect

    async def delayed_inspection(*args):
        entered.set()
        await release.wait()
        return await inspect(*args)

    monkeypatch.setattr(manager, "_inspect", delayed_inspection)
    task = asyncio.create_task(manager.analyze(project["id"], AnalysisInput()))
    await entered.wait()
    stale = (await manager.analyses(project["id"]))[0]
    assert stale["status"] == "analyzing"
    release.set()
    ready = await task
    recovered = await manager._change(stale, status="interrupted", detail="Stale reader recovery")
    assert recovered["status"] == "ready_for_review"
    assert recovered["digest"] == ready["digest"] and recovered["documents"] == ready["documents"]
