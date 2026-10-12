# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
import json
from pathlib import Path

import pytest

from firefly_forge.models import RepositoryInput, RunInput
from firefly_forge.policies import PolicyCatalog, PolicySync, ProjectPolicies
from firefly_forge.process import checked


def catalog(instructions="Use explicit domain errors"):
    return {
        "version": "1.0",
        "skills": [
            {
                "id": "domain-errors",
                "name": "Domain errors",
                "description": "Use explicit errors",
                "category": "design",
                "kind": "recommendation",
                "default": True,
                "instructions": instructions,
                "commands": [],
            }
        ],
    }


async def test_central_git_policy_sync_is_pinned_data_only_and_snapshotted(tmp_path: Path, store_factory):
    source = tmp_path / "central"
    source.mkdir()
    await checked(["git", "init", "-b", "main"], source)
    await checked(["git", "config", "user.email", "fixture@example.invalid"], source)
    await checked(["git", "config", "user.name", "Policy fixture"], source)
    (source / "catalog.json").write_text(json.dumps(catalog()))
    await checked(["git", "add", "catalog.json"], source)
    await checked(["git", "commit", "-m", "Add policies"], source)
    revision = await checked(["git", "rev-parse", "HEAD"], source)
    policies = PolicyCatalog(tmp_path / "state")
    result = await policies.sync(PolicySync(repository_url=str(source), ref=revision))
    assert result["revision"] == revision
    assert result["skills"][0]["id"] == "domain-errors"
    assert len(result["digest"]) == 64
    store = await store_factory(tmp_path / "state" / "factory.db")
    repo = await store.add_repository(RepositoryInput(name="App", url=str(source)))
    run = await store.create_run(RunInput(repository_id=repo["id"], brief="Build product"))
    assert run["skills"] == ["domain-errors"]
    assert run["policy_snapshot"]["skills"][0]["instructions"] == "Use explicit domain errors"
    assert (await store.create_run(RunInput(repository_id=repo["id"], brief="Build", skills=[])))["skills"] == []
    await store.set_project_policies(repo["id"], ProjectPolicies(skills=[]))
    assert (await store.create_run(RunInput(repository_id=repo["id"], brief="Build")))["skills"] == []


def test_policy_catalog_rejects_unknown_fields_and_unbounded_commands(tmp_path):
    value = catalog()
    value["skills"][0]["script"] = "arbitrary executable"
    with pytest.raises(ValueError):
        PolicyCatalog.validate(value)
    value = catalog()
    value["skills"][0].update(kind="quality_gate", commands=[])
    with pytest.raises(ValueError):
        PolicyCatalog.validate(value)
    with pytest.raises(ValueError):
        PolicySync(repository_url="https://example.com/policies.git", ref="main")
    with pytest.raises(ValueError):
        PolicySync(repository_url="https://example.com/policies.git", ref="a" * 40, catalog_path="../credentials.json")
