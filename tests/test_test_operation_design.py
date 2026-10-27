# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Existing-source checks preserve historical definitions without approving old designs."""

from __future__ import annotations

import copy
import hashlib
import json
import sys
from uuid import uuid4

import pytest
from starlette.testclient import TestClient
from test_execution import seed_repository
from test_product_design import definition, review

from firefly_forge.api import create_application
from firefly_forge.collaboration import Collaboration, ProductDefinition
from firefly_forge.collaboration_persistence import ForgeProductDefinitionEntity
from firefly_forge.engine import Engine
from firefly_forge.journal import Journal
from firefly_forge.models import Command, RepositoryInput, RunInput
from firefly_forge.process import checked
from firefly_forge.product_design import ProductDesign, definition_digest
from firefly_forge.store import identifier


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


async def historical_product(store, repository_id):
    service = ProductDesign(store)
    bundle = await service.preview(repository_id, definition())
    legacy = definition().model_dump(mode="json", exclude={"design_review", "ai_origin"})
    legacy.pop("integrations")
    legacy["visual_design"]["renderer"] = "fixed-reference-template-v1"
    legacy["visual_design"]["geometry_configurable"] = False
    bundle["definition_digest"] = digest(legacy)
    bundle["digest"] = digest({key: value for key, value in bundle.items() if key != "digest"})
    (service.directory / repository_id / (bundle["digest"] + ".json")).write_text(json.dumps(bundle))
    legacy["design_review"] = review(bundle).model_dump()
    assert definition_digest(ProductDefinition.model_validate(legacy)) != bundle["definition_digest"]
    snapshot = {
        "repository_id": repository_id,
        "revision": 1,
        "definition": legacy,
        "created_at": "2026-10-12T00:00:00+00:00",
    }
    snapshot["digest"] = digest(snapshot)
    async with store._unit():
        await store.product_records.save(
            ForgeProductDefinitionEntity(
                id=uuid4(),
                repository_id=identifier(repository_id),
                revision=1,
                digest=snapshot["digest"],
                data=snapshot,
            )
        )
    return snapshot


@pytest.mark.parametrize("explicit_snapshot", [False, True])
async def test_existing_source_check_preserves_legacy_product_without_design_or_model(
    store, tmp_path, explicit_snapshot
):
    source = await seed_repository(tmp_path / "source")
    repository = await store.add_repository(RepositoryInput(name="Historical product", url=str(source)))
    product = await historical_product(store, repository["id"])
    source_head = await checked(["git", "rev-parse", "HEAD"], source)
    source_tree = await checked(["git", "rev-parse", "HEAD^{tree}"], source)
    request = {
        "repository_id": repository["id"],
        "brief": "Verify the existing committed README without changing this product",
        "operation": "test",
        "commands": [
            Command(
                argv=[
                    sys.executable,
                    "-c",
                    "from pathlib import Path; assert Path('README.md').read_text() == 'Fixture repository\\n'",
                ]
            )
        ],
    }
    if explicit_snapshot:
        request["product_definition"] = product
    run = await store.create_run(RunInput.model_validate(request))
    assert run["product_definition"] == product
    assert run["design_snapshot"] is None

    def forbidden_provider(_):
        raise AssertionError("Existing-source checks must not dispatch a model")

    engine = Engine(store, tmp_path / "jobs", forbidden_provider, journal=Journal(store))
    await engine.execute_run(run["id"])
    result = await store.get_run(run["id"])
    assert result["status"] == "succeeded"
    assert result["final_verification"][0]["exit_code"] == 0
    assert result["changed_files"] == []
    assert result["base_revision"] == result["checkpoint_revision"] == source_head
    assert result["source_tree"] == source_tree
    assert result["review"]["status"] == "not_requested" and result["pr_url"] is None
    assert (await Collaboration(store).product(repository["id"])) == product
    assert await checked(["git", "status", "--porcelain"], source) == ""
    plan = await engine.journal.plan(run["id"])
    assert plan["product_definition"] == product and plan["design_reference"] is None
    entries = await engine.journal.entries(run["id"])
    assert Journal.verify_chain(entries)
    assert {entry["task_id"] for entry in entries} == {"plan", "prepare", "verify", "checkpoint"}
    with pytest.raises(ValueError, match="Test operations"):
        await engine.publish(run["id"])


@pytest.mark.parametrize("operation", ["build", "maintain"])
def test_historical_design_still_blocks_implementation_http_admission(tmp_path, postgres_schema, operation):
    from native_client_knowledge_fixtures import activate_native_client_target

    token = "test-only-historical-design-operator"
    app = create_application(tmp_path, token, role="api")
    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        client.portal.call(activate_native_client_target, app.state.store, tmp_path / "framework-prerequisite")
        prerequisite_runs = client.get("/api/v1/runs", headers={"Authorization": f"Bearer {token}"}).json()
        repository = client.post(
            "/api/v1/repositories",
            headers={"Authorization": f"Bearer {token}"},
            json={"name": "Historical product", "url": "https://github.com/example/product.git"},
        ).json()
        product = client.portal.call(historical_product, app.state.store, repository["id"])
        response = client.post(
            "/api/v1/runs",
            headers={"Authorization": f"Bearer {token}"},
            json={"repository_id": repository["id"], "brief": "Implement a change", "operation": operation},
        )
        assert response.status_code == 409
        assert "different product definition" in response.json()["detail"]
        assert (
            client.get(
                f"/api/v1/repositories/{repository['id']}/product", headers={"Authorization": f"Bearer {token}"}
            ).json()
            == product
        )
        assert client.get("/api/v1/runs", headers={"Authorization": f"Bearer {token}"}).json() == prerequisite_runs


def test_test_http_admission_freezes_exact_historical_revision_without_approving_design(tmp_path, postgres_schema):
    token = "test-only-historical-design-operator"
    app = create_application(tmp_path, token, role="api")
    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        headers = {"Authorization": f"Bearer {token}"}
        repository = client.post(
            "/api/v1/repositories",
            headers=headers,
            json={"name": "Historical product", "url": "https://github.com/example/product.git"},
        ).json()
        product = client.portal.call(historical_product, app.state.store, repository["id"])
        response = client.post(
            "/api/v1/runs",
            headers=headers,
            json={
                "repository_id": repository["id"],
                "brief": "Verify existing source without implementation",
                "operation": "test",
                "product_definition": product,
            },
        )
        assert response.status_code == 201
        run = response.json()
        assert run["product_definition"] == product and run["design_snapshot"] is None
        assert run["status"] == "queued" and run["assurance"]["review_agent"] is None
        forged = copy.deepcopy(product)
        forged["definition"]["customer"] = "Client-forged approval"
        forged["digest"] = digest({key: value for key, value in forged.items() if key != "digest"})
        denied = client.post(
            "/api/v1/runs",
            headers=headers,
            json={
                "repository_id": repository["id"],
                "brief": "Verify existing source with a client-supplied revision",
                "operation": "test",
                "product_definition": forged,
            },
        )
        assert denied.status_code == 409 and "existing immutable product revision" in denied.json()["detail"]
        assert client.get(f"/api/v1/repositories/{repository['id']}/product", headers=headers).json() == product
