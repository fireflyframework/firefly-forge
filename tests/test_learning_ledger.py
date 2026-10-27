# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
import importlib
import sys

import pytest
from test_role_execution import fixture_run

from firefly_forge.engine import Engine
from firefly_forge.journal import Journal
from firefly_forge.models import Command, RunInput
from firefly_forge.providers import AgentResult


async def completed(store, tmp_path, *, scoped=False, framework_remote=None):
    initial = await fixture_run(store, tmp_path)
    source = tmp_path / "source"
    # Native positive callers explicitly select the same version as their admitted corpus.
    framework_version = "26.10.1"
    if framework_remote is not None:
        from native_client_knowledge_fixtures import VERSION

        framework_version = VERSION
    (source / "pyproject.toml").write_text(
        '[project]\nname="owned-stock"\nversion="0.1.0"\n' + f'dependencies=["pyfly=={framework_version}"]\n'
    )
    from firefly_forge.process import checked

    await checked(["git", "add", "pyproject.toml"], source)
    await checked(
        [
            "git",
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-m",
            "Declare framework version",
        ],
        source,
    )

    if scoped:
        from firefly_forge.project_models import ProjectDefinition
        from firefly_forge.projects import Projects

        project = await Projects(store).configure(
            ProjectDefinition(
                name="Source product",
                description="Owned source scope",
                topology="monorepo",
                owners=["Source team"],
                components=[
                    {
                        "id": "stock",
                        "name": "Stock",
                        "repository_id": initial["repository_id"],
                        "responsibility": "Own stock assertions",
                    }
                ],
            )
        )
        initial = await store.create_run(
            RunInput(
                repository_id=initial["repository_id"],
                project_id=project["id"],
                component_id="stock",
                brief="Verify stock behavior",
                skills=[],
                commands=[Command(argv=[sys.executable, "-c", "from product import stock; assert stock == 5"])],
            )
        )

    class Worker:
        async def execute(self, workspace, *args):
            (workspace / "product.py").write_text("stock = 5\n")
            return AgentResult("Observed stock assertion implemented", "fixture")

    engine = Engine(store, tmp_path / "workspaces", lambda _: Worker(), journal=Journal(store))
    if framework_remote is not None:
        from native_client_knowledge_fixtures import (
            bind_framework_knowledge,
            preserve_product_checks_with_framework_fixture,
        )

        from firefly_forge.sandbox import SandboxExecutor

        bind_framework_knowledge(engine, framework_remote)
        engine.sandbox = SandboxExecutor(store, engine.journal)
        preserve_product_checks_with_framework_fixture(engine)
    await engine.execute_run(initial["id"])
    final = await store.get_run(initial["id"])
    assert final["status"] == "succeeded", final.get("error")
    return engine, final


@pytest.mark.asyncio
async def test_success_flag_or_snippet_without_real_wal_cannot_become_verified(store, tmp_path):
    module = importlib.import_module("firefly_forge.learning")
    run = await fixture_run(store, tmp_path)
    ledger = module.LearningLedger(store, Journal(store))
    await store.update(
        run["id"],
        status="succeeded",
        source_tree="d" * 40,
        final_verification=[{"argv": ["fake-test"], "exit_code": 0}],
        summary="Model says everything passed",
    )
    with pytest.raises(ValueError, match="receipt"):
        await ledger.capture_outcome(run["id"])
    assert await ledger.records(run["repository_id"]) == []


@pytest.mark.asyncio
async def test_actual_source_and_command_receipts_create_only_bounded_local_outcome(store, tmp_path):
    module = importlib.import_module("firefly_forge.learning")
    engine, run = await completed(store, tmp_path)
    ledger = module.LearningLedger(store, engine.journal)
    ledger.engine = engine
    outcome = await ledger.capture_outcome(run["id"])
    assert outcome["state"] == "verified" and outcome["kind"] == "local_outcome"
    assert outcome["envelope"]["question"] == run["brief"]
    assert outcome["envelope"]["context"]["source_tree"] == run["source_tree"]
    assert outcome["envelope"]["receipts"]
    assert "exit code 0" in outcome["envelope"]["statement"]
    assert "web answer" not in outcome["envelope"]["statement"]
    assert (await ledger.capture_outcome(run["id"]))["id"] == outcome["id"]
    decision = await ledger.lookup(run["id"], run["brief"], outcome["envelope"]["context"])
    assert decision["reason"] == "verified_exact_match" and decision["eligible"]
    (tmp_path / "unrelated").mkdir()
    unrelated = await fixture_run(store, tmp_path / "unrelated")
    assert await ledger.records(unrelated["repository_id"]) == []
    await ledger.invalidate(outcome["id"], reason="The declared dependency version changed", request_id="a" * 32)
    invalidated = await ledger.lookup(run["id"], run["brief"], outcome["envelope"]["context"])
    assert not invalidated["eligible"] and invalidated["reason"] == "invalidated"


@pytest.mark.asyncio
async def test_shared_repository_does_not_share_product_private_learning(store, tmp_path):
    from uuid import uuid4

    from firefly_forge.identity import Principal
    from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied, ProjectAccess
    from firefly_forge.project_models import ProjectDefinition
    from firefly_forge.projects import Projects

    module = importlib.import_module("firefly_forge.learning")
    engine, run = await completed(store, tmp_path, scoped=True)
    ledger = module.LearningLedger(store, engine.journal)
    ledger.engine = engine
    outcome = await ledger.capture_outcome(run["id"])
    other = await Projects(store).configure(
        ProjectDefinition(
            name="Other product",
            description="Separate authority on shared source",
            topology="monorepo",
            owners=["Other team"],
            components=[
                {
                    "id": "stock",
                    "name": "Stock",
                    "repository_id": run["repository_id"],
                    "responsibility": "Own separate stock work",
                }
            ],
        )
    )
    actor = Principal(uuid4(), "https://id.example/realms/forge", uuid4().hex)
    await ProjectAccess(store).set_membership(other["id"], actor, "owner")
    token = CURRENT_PRINCIPAL.set(actor)
    try:
        assert await ledger.records(run["repository_id"]) == []
        from firefly_forge.knowledge import KnowledgeManager
        from firefly_forge.learning_projection import LearningProjection

        projection = LearningProjection(store, engine.journal, ledger, KnowledgeManager(store))
        with pytest.raises(AccessDenied):
            await projection.status(outcome["id"])
        with pytest.raises(AccessDenied):
            await ledger.invalidate(outcome["id"], reason="Unauthorized cross-product change", request_id="c" * 32)
    finally:
        CURRENT_PRINCIPAL.reset(token)
    assert (await ledger.records(run["repository_id"]))[0]["state"] == "verified"


@pytest.mark.asyncio
async def test_native_failed_check_is_candidate_not_verified_negative_or_skipped_verification(store, tmp_path):
    from firefly_forge.learning import LearningLedger

    run = await fixture_run(store, tmp_path)
    calls = []

    class Worker:
        async def execute(self, workspace, *args):
            calls.append(1)
            (workspace / "product.py").write_text("stock = 4\n")
            return AgentResult("Wrong stock amount", "fixture")

    engine = Engine(store, tmp_path / "workspaces", lambda _: Worker(), journal=Journal(store))
    ledger = LearningLedger(store, engine.journal)
    ledger.engine = engine
    engine.learning = ledger
    await engine.execute_run(run["id"])
    final = await store.get_run(run["id"])
    assert final["status"] == "failed" and final["verification"]
    records = await ledger.records(run["repository_id"])
    assert records and all(r["state"] == "candidate" and r["kind"] == "observed_failure" for r in records)
    assert all(r["envelope"]["purpose"] == "verification" for r in records)
    assert all(
        "Observed check" in r["envelope"]["statement"] and "defect" not in r["envelope"]["statement"] for r in records
    )
    assert len(calls) >= 1
    for record in records:
        assert not (await ledger.lookup(run["id"], run["brief"], record["envelope"]["context"]))["eligible"]


@pytest.mark.asyncio
async def test_outcome_uses_frozen_freshness_and_legacy_snapshot_has_no_learning_grant(store, tmp_path):
    from datetime import datetime

    from firefly_forge.learning import LearningLedger
    from firefly_forge.learning_configuration import configure_learning, frozen_learning
    from firefly_forge.learning_models import LearningPolicy

    configure_learning(store.configuration, LearningPolicy(max_age_hours=12))
    engine, run = await completed(store, tmp_path)
    configure_learning(store.configuration, LearningPolicy(max_age_hours=24))
    ledger = LearningLedger(store, engine.journal)
    ledger.engine = engine
    outcome = await ledger.capture_outcome(run["id"])
    age = datetime.fromisoformat(outcome["expires_at"]) - datetime.fromisoformat(outcome["verified_at"])
    assert age.total_seconds() == 12 * 3600
    assert not frozen_learning(None)["settings"]["enabled"]
    assert run["learning_snapshot"]["settings"]["max_age_hours"] == 12
