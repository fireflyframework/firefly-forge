# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
import sys

import pytest

from firefly_forge.engine import Engine
from firefly_forge.models import RepositoryInput, RunInput
from firefly_forge.process import checked
from firefly_forge.product_branding import BrandInput, ProductBranding
from firefly_forge.project_models import ProjectDefinition
from firefly_forge.projects import Projects
from firefly_forge.providers import AgentResult


def definition(repository_id, **changes):
    value = {
        "name": "Inventory",
        "description": "Stock and reservation services with a browser workspace.",
        "topology": "monorepo",
        "owners": ["Inventory team"],
        "components": [
            {
                "id": "stock",
                "name": "Stock service",
                "repository_id": repository_id,
                "path": "services/stock",
                "responsibility": "Own stock availability",
                "verification": [{"argv": [sys.executable, "-c", "assert 2 + 3 == 5"]}],
            },
            {
                "id": "workspace",
                "name": "Inventory workspace",
                "repository_id": repository_id,
                "path": "web",
                "kind": "web",
                "backend": "none",
                "frontend": "react",
                "depends_on": ["stock"],
                "responsibility": "Show stock and reservation journeys",
            },
        ],
    }
    value.update(changes)
    return ProjectDefinition.model_validate(value)


def test_topology_rejects_overlapping_roots_missing_dependencies_and_cycles():
    source = definition("a" * 32).model_dump()
    for path in (".", "services", "services/stock", "../outside", ".git", "/root"):
        invalid = {**source, "components": [{**source["components"][0]}, {**source["components"][1], "path": path}]}
        with pytest.raises(ValueError):
            ProjectDefinition.model_validate(invalid)
    source["components"][0]["depends_on"] = ["workspace"]
    with pytest.raises(ValueError, match="cycles"):
        ProjectDefinition.model_validate(source)


async def test_component_context_freezes_stack_scope_and_revision_in_native_run(store, tmp_path):
    repository = await store.add_repository(RepositoryInput(name="Inventory source", url=str(tmp_path)))
    projects = Projects(store)
    project = await projects.configure(definition(repository["id"]))
    run = await store.create_run(
        RunInput(
            repository_id=repository["id"],
            project_id=project["id"],
            component_id="stock",
            brief="Maintain stock validation",
        )
    )
    assert run["component_snapshot"]["path"] == "services/stock"
    assert run["frontend"] == "none" and run["commands"]
    await projects.configure(definition(repository["id"], name="Renamed product"), project["id"])
    assert (await store.get_run(run["id"]))["component_snapshot"]["project_revision"] == 1
    assert (await projects.overview(project["id"]))["components"][0]["latest_status"] == "queued"
    with pytest.raises(ValueError, match="stack"):
        await store.create_run(
            RunInput(
                repository_id=repository["id"],
                project_id=project["id"],
                component_id="stock",
                brief="Maintain",
                frontend="vue",
            )
        )


async def test_multi_repository_product_links_real_repositories_without_fake_execution(store, tmp_path):
    first = await store.add_repository(RepositoryInput(name="Service", url=str(tmp_path / "service")))
    second = await store.add_repository(RepositoryInput(name="Web", url=str(tmp_path / "web")))
    value = definition(first["id"]).model_dump()
    value["topology"] = "multi_repo"
    value["components"][0]["path"] = "."
    value["components"][1].update(repository_id=second["id"], path=".")
    project = await Projects(store).configure(ProjectDefinition.model_validate(value))
    overview = await Projects(store).overview(project["id"])
    assert overview["counts"] == {"not_started": 2}
    with pytest.raises(ValueError, match="repository"):
        await store.create_run(
            RunInput(repository_id=first["id"], project_id=project["id"], component_id="workspace", brief="Build web")
        )


async def test_component_reassignment_does_not_attribute_prior_repository_delivery_to_new_component(store, tmp_path):
    first = await store.add_repository(RepositoryInput(name="Original", url=str(tmp_path / "original")))
    replacement = await store.add_repository(RepositoryInput(name="Replacement", url=str(tmp_path / "replacement")))
    projects = Projects(store)
    project = await projects.configure(definition(first["id"]))
    run = await store.create_run(
        RunInput(
            repository_id=first["id"],
            project_id=project["id"],
            component_id="stock",
            brief="Maintain stock",
        )
    )
    await store.update(run["id"], status="succeeded", pr_url="https://github.com/team/original/pull/1")
    changed = definition(first["id"]).model_dump()
    changed["topology"] = "multi_repo"
    changed["components"][0]["repository_id"] = replacement["id"]
    await projects.configure(ProjectDefinition.model_validate(changed), project["id"])
    component = (await projects.overview(project["id"]))["components"][0]
    assert component["latest_status"] == "not_started" and component["runs"] == []


async def test_monorepo_delivery_refuses_source_changes_outside_component(store, tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "services/stock").mkdir(parents=True)
    (source / "services/stock/product.py").write_text("value = 1\n")
    (source / "web").mkdir()
    (source / "web/README.md").write_text("Existing web source\n")
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
            "Create acceptance fixture",
        ],
        source,
    )
    repo = await store.add_repository(RepositoryInput(name="Existing monorepo", url=str(source)))
    project = await Projects(store).configure(definition(repo["id"]))
    run = await store.create_run(
        RunInput(
            repository_id=repo["id"], project_id=project["id"], component_id="stock", brief="Maintain stock", skills=[]
        )
    )

    class Worker:
        async def execute(self, workspace, *args):
            (workspace / "services/stock/product.py").write_text("value = 7\n")
            (workspace / "web/README.md").write_text("Unexpected sibling mutation\n")
            return AgentResult("Changed", "fixture")

    engine = Engine(store, tmp_path / "workspaces", lambda _: Worker())
    await engine.execute_run(run["id"])
    result = await store.get_run(run["id"])
    assert result["status"] == "failed" and "component" in result["error"]
    assert (source / "web/README.md").read_text() == "Existing web source\n"


async def test_logical_product_freezes_shared_design_source_and_entire_component_graph(store, tmp_path):
    first = await store.add_repository(RepositoryInput(name="Service", url=str(tmp_path / "service")))
    second = await store.add_repository(RepositoryInput(name="Web", url=str(tmp_path / "web")))
    value = definition(first["id"]).model_dump()
    value["topology"] = "multi_repo"
    value["product_repository_id"] = second["id"]
    value["components"][0]["path"] = "."
    value["components"][1].update(repository_id=second["id"], path=".")
    manager = Projects(store)
    project = await manager.configure(ProjectDefinition.model_validate(value))
    shared_brand = await ProductBranding(store).configure(
        second["id"], BrandInput(name="Inventory product", primary="#416eab")
    )
    await ProductBranding(store).configure(first["id"], BrandInput(name="Repository local", primary="#9b5521"))
    run = await store.create_run(
        RunInput(repository_id=first["id"], project_id=project["id"], component_id="stock", brief="Maintain")
    )
    assert run["component_snapshot"]["product_repository_id"] == second["id"]
    assert run["branding_snapshot"]["digest"] == shared_brand["digest"]
    assert run["component_snapshot"]["project_definition"] == project["definition"]
    value["components"][1]["responsibility"] = "Changed after the queued run"
    await manager.configure(ProjectDefinition.model_validate(value), project["id"])
    frozen = (await store.get_run(run["id"]))["component_snapshot"]
    assert frozen["project_definition"]["components"][1]["responsibility"] != value["components"][1]["responsibility"]
    assert (await manager.revision(project["id"], 1))["digest"] == project["digest"]
    value["product_repository_id"] = "f" * 32
    with pytest.raises(ValueError, match="member"):
        ProjectDefinition.model_validate(value)


def test_component_working_directory_rejects_symlinks_outside_the_workspace(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (workspace / "service").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="escapes"):
        Engine.component_directory({"component_snapshot": {"path": "service"}}, workspace)
