# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
import asyncio

import pytest

from firefly_forge.models import RepositoryInput, RunInput


async def seed(store, brief="Build stock alerts"):
    repo = await store.add_repository(RepositoryInput(name="product", url="https://github.com/team/product.git"))
    return await store.create_run(RunInput(repository_id=repo["id"], brief=brief, backend="pyfly", frontend="none"))


async def profiled_pause(store):
    from firefly_forge.journal import Journal
    from firefly_forge.model_profiles import ModelProfileInput, ModelProfiles
    from firefly_forge.task_routing import TaskRouting

    profiles = ModelProfiles(store.configuration)
    for name, provider, values in (
        ("builder", "agentic", {"model": "openai:gpt-builder", "api_key": "private-builder-key"}),
        ("next-builder", "agentic", {"model": "openai:gpt-next", "api_key": "private-next-key"}),
        ("reviewer", "agentic", {"model": "openai:gpt-reviewer", "api_key": "private-review-key"}),
        (
            "bridge-builder",
            "bridge",
            {"model": "bridge/default", "endpoint": "https://bridge.example", "token": "private-bridge-key"},
        ),
        ("host-cli", "codex", {}),
    ):
        profiles.save(name, ModelProfileInput(name=name, provider=provider, values=values))
    store.task_router = TaskRouting(profiles)
    repo = await store.add_repository(RepositoryInput(name="Profiled", url="https://github.com/team/profiled.git"))
    run = await store.create_run(
        RunInput(
            repository_id=repo["id"],
            brief="Build with distinct model configurations",
            model_profile_id="builder",
            assurance={"review_model_profile_id": "reviewer"},
        )
    )
    journal = Journal(store)
    await journal.ensure_plan(run["id"])
    effect = await journal.intent(run["id"], "implement")
    await journal.fail(run["id"], effect["idempotency_key"], "quota", "Explicit provider quota pause")
    return journal, await store.get_run(run["id"]), profiles


async def test_normal_resume_preserves_frozen_profiles_despite_changed_routing(store):
    from firefly_forge.model_profiles import ModelProfileInput
    from firefly_forge.task_routing import TaskRoutingInput

    journal, paused, profiles = await profiled_pause(store)
    profiles.save(
        "builder",
        ModelProfileInput(name="Changed builder", provider="agentic", values={"model": "openai:gpt-changed"}),
    )
    await store.task_router.save(TaskRoutingInput(bindings={"implementation": "next-builder"}))
    result = await journal.resume(paused["id"])
    for field in ("model_selection", "review_model_selection", "config_revision", "review_config_revision"):
        assert result[field] == paused[field]
    assert (await journal.plan(paused["id"]))["model_selection"] == paused["model_selection"]
    assert profiles.validate_snapshot(result["model_selection"])["FORGE_AGENTIC_MODEL"] == "openai:gpt-builder"
    assert (await journal.entries(paused["id"]))[-1]["kind"] == "resume"


@pytest.mark.parametrize(
    "name,provider,credential_field,credential",
    [
        ("next-builder", "agentic", "FORGE_AGENTIC_API_KEY", "private-next-key"),
        ("bridge-builder", "bridge", "FORGE_BRIDGE_TOKEN", "private-bridge-key"),
    ],
)
async def test_explicit_profile_handoff_freezes_new_config_and_archives_old_selection(
    store, name, provider, credential_field, credential
):
    from sqlalchemy import select

    from firefly_forge.persistence import ForgePlanEntity

    journal, paused, profiles = await profiled_pause(store)
    result = await journal.resume(paused["id"], provider, model_profile_id=name)
    assert result["agent"] == provider
    assert result["model_profile_id"] == name
    assert result["model_selection"]["profile_id"] == name
    assert result["config_revision"] == result["model_selection"]["config_revision"]
    assert result["config_revision"] != paused["config_revision"]
    assert result["execution_config"] == result["model_selection"]["execution_config"]
    assert profiles.validate_snapshot(result["model_selection"])[credential_field] == credential
    for field in ("review_model_selection", "review_config_revision", "policy_snapshot", "repair_budget"):
        assert result[field] == paused[field]
    async with store._unit() as session:
        plans = list(await session.scalars(select(ForgePlanEntity).order_by(ForgePlanEntity.revision)))
        assert len(plans) == 2
        assert plans[0].data["model_selection"] == paused["model_selection"]
        assert plans[1].data["model_selection"] == result["model_selection"]
    event = (await journal.entries(paused["id"]))[-1]
    assert event["kind"] == "handoff"
    assert event["payload"]["previous_model_selection"] == paused["model_selection"]
    assert event["payload"]["model_selection"] == result["model_selection"]
    assert credential not in str(event)
    assert journal.verify_chain(await journal.entries(paused["id"]))


async def test_conflicting_profile_provider_handoff_changes_nothing(store):
    journal, paused, _ = await profiled_pause(store)
    entries = await journal.entries(paused["id"])
    with pytest.raises(ValueError, match="conflict"):
        await journal.resume(paused["id"], "claude", model_profile_id="next-builder")
    assert await store.get_run(paused["id"]) == paused
    assert await journal.entries(paused["id"]) == entries


async def test_explicit_legacy_handoff_clears_named_descriptor_with_auditable_transition(store):
    journal, paused, _ = await profiled_pause(store)
    result = await journal.resume(paused["id"], "claude")
    assert result["agent"] == "claude"
    assert result["model_selection"] is None
    assert result["model_profile_id"] is None
    assert result["review_model_selection"] == paused["review_model_selection"]
    assert (await journal.plan(paused["id"]))["model_selection"] is None
    event = (await journal.entries(paused["id"]))[-1]
    assert event["payload"]["previous_model_selection"] == paused["model_selection"]
    assert event["payload"]["model_selection"] is None
    assert event["payload"]["selection_mode"] == "legacy_provider"


async def test_named_handoff_checks_resolved_provider_isolation_before_state_change(store):
    from uuid import uuid4

    from firefly_forge.identity import Principal
    from firefly_forge.project_access import ProjectAccess
    from firefly_forge.project_models import ProjectDefinition
    from firefly_forge.projects import Projects

    journal, paused, _ = await profiled_pause(store)
    project = await Projects(store).configure(
        ProjectDefinition(
            name="Scoped build",
            description="A developer-scoped model handoff",
            topology="monorepo",
            owners=["Platform"],
            components=[
                dict(id="service", name="Service", repository_id=paused["repository_id"], responsibility="API")
            ],
        )
    )
    user = Principal(uuid4(), "https://identity.example/realms/forge", uuid4().hex)
    access = ProjectAccess(store)
    await access.set_membership(project["id"], user, "developer")
    await store.update(paused["id"], project_id=project["id"], execution_actor=access.actor_snapshot(user))
    before = await store.get_run(paused["id"])
    with pytest.raises(ValueError, match="administrator"):
        await journal.resume(paused["id"], model_profile_id="host-cli")
    assert await store.get_run(paused["id"]) == before


async def test_automatic_retry_cannot_change_named_profile(store):
    journal, paused, _ = await profiled_pause(store)
    with pytest.raises(ValueError, match="automatic"):
        await journal.resume(paused["id"], model_profile_id="next-builder", auto=True)
    assert await store.get_run(paused["id"]) == paused


async def test_concurrent_named_handoffs_commit_one_immutable_transition(store):
    journal, paused, _ = await profiled_pause(store)
    results = await asyncio.gather(
        journal.resume(paused["id"], model_profile_id="next-builder"),
        journal.resume(paused["id"], model_profile_id="bridge-builder"),
        return_exceptions=True,
    )
    assert sum(isinstance(result, dict) for result in results) == 1
    assert sum(isinstance(result, ValueError) for result in results) == 1
    resumed = await store.get_run(paused["id"])
    assert resumed["agent"] == resumed["model_selection"]["provider"]
    assert resumed["config_revision"] == resumed["model_selection"]["config_revision"]
    assert (await journal.plan(paused["id"]))["revision"] == 2
    assert len([entry for entry in await journal.entries(paused["id"]) if entry["kind"] == "handoff"]) == 1


async def test_named_handoff_without_router_fails_without_legacy_fallback(store):
    journal, paused, _ = await profiled_pause(store)
    store.task_router = None
    with pytest.raises(ValueError, match="configured"):
        await journal.resume(paused["id"], model_profile_id="next-builder")
    assert await store.get_run(paused["id"]) == paused


async def test_controller_accepts_named_handoff_and_rejects_empty_selection(store):
    from firefly_forge.controllers import JournalController, ResumeRequest

    journal, paused, _ = await profiled_pause(store)
    controller = JournalController(journal)
    with pytest.raises(ValueError, match="Choose"):
        await controller.handoff(paused["id"], ResumeRequest())
    result = await controller.handoff(paused["id"], ResumeRequest(model_profile_id="next-builder"))
    assert result["model_selection"]["profile_id"] == "next-builder"


async def test_plan_is_immutable_and_concurrent_intent_executes_once(store):
    from firefly_forge.journal import Journal

    run = await seed(store)
    journal = Journal(store)
    plan = await journal.ensure_plan(run["id"])
    assert [task["id"] for task in plan["tasks"]] == ["prepare", "implement", "verify", "checkpoint"]
    assert "stock alerts" in plan["tasks"][1]["instructions"]
    intents = await asyncio.gather(journal.intent(run["id"], "implement"), journal.intent(run["id"], "implement"))
    assert [item["disposition"] for item in intents].count("execute") == 1
    assert intents[0]["idempotency_key"] == intents[1]["idempotency_key"]
    await journal.receipt(
        run["id"],
        intents[0]["idempotency_key"],
        {"summary": "Stock alerts built"},
        checkpoint={"source_tree": "abc", "head": "def"},
    )
    assert (await journal.intent(run["id"], "implement"))["disposition"] == "completed"
    changed = await journal.plan(run["id"])
    assert changed["digest"] == plan["digest"]
    assert changed["tasks"][1]["status"] == "done"
    assert (await store.get_run(run["id"]))["checkpoint"]["source_tree"] == "abc"
    entries = await journal.entries(run["id"])
    assert [entry["sequence"] for entry in entries] == list(range(1, len(entries) + 1))
    assert journal.verify_chain(entries)


async def test_restart_requires_reconciliation_before_replaying_unknown_effect(store, store_factory):
    from firefly_forge.journal import Journal

    run = await seed(store)
    journal = Journal(store)
    await journal.ensure_plan(run["id"])
    effect = await journal.intent(
        run["id"], "implement", payload={"workspace": "/isolated/product", "base_revision": "old-head"}
    )
    reopened = Journal(await store_factory(store.path))
    assert await reopened.recover() == 1
    assert (await reopened.intent(run["id"], "implement"))["disposition"] == "needs_reconciliation"
    result = await reopened.resume(run["id"])
    assert result["checkpoint"] == {}
    assert (await reopened.intent(run["id"], "implement"))["disposition"] == "needs_reconciliation"
    await reopened.reconcile(
        run["id"],
        effect["idempotency_key"],
        evidence={"source_tree": "observed-tree", "head": "old-head"},
        outcome="retry",
    )
    next_effect = await reopened.intent(run["id"], "implement", attempt=2)
    assert next_effect["disposition"] == "execute"
    assert next_effect["idempotency_key"] != effect["idempotency_key"]


async def test_credit_pause_and_provider_handoff_keep_policy_workspace_and_history(store):
    from firefly_forge.journal import Journal

    run = await seed(store)
    journal = Journal(store)
    await journal.ensure_plan(run["id"])
    await store.update(run["id"], workspace="/isolated/product", base_revision="base-head")
    effect = await journal.intent(run["id"], "implement")
    await journal.fail(run["id"], effect["idempotency_key"], "insufficient_credits", "Provider credits exhausted")
    paused = await store.get_run(run["id"])
    assert paused["status"] == "paused"
    assert paused["pause"]["automatic"] is False
    result = await journal.resume(run["id"], agent="claude")
    assert result["agent"] == "claude"
    assert result["workspace"] == "/isolated/product"
    assert result["policy_snapshot"] == run["policy_snapshot"]
    assert result["config_revision"] != run["config_revision"]
    assert [entry["kind"] for entry in await journal.entries(run["id"])][-1] == "handoff"


async def test_receipt_failure_rolls_back_task_and_checkpoint(store, monkeypatch):
    from firefly_forge.journal import Journal
    from firefly_forge.persistence import ForgeJournalRepository

    run = await seed(store)
    journal = Journal(store)
    await journal.ensure_plan(run["id"])
    effect = await journal.intent(run["id"], "implement")

    async def fail(*args, **kwargs):
        raise RuntimeError("Journal receipt unavailable")

    monkeypatch.setattr(ForgeJournalRepository, "save", fail)
    with pytest.raises(RuntimeError, match="receipt unavailable"):
        await journal.receipt(
            run["id"], effect["idempotency_key"], {"summary": "Built"}, checkpoint={"source_tree": "new-tree"}
        )
    assert (await journal.plan(run["id"]))["tasks"][1]["status"] == "running"
    assert not (await store.get_run(run["id"])).get("checkpoint")


async def test_concurrent_recovery_and_project_journals_are_separate(store, store_factory):
    from firefly_forge.journal import Journal

    first, other = await seed(store, "First project"), await seed(store, "Other project")
    journal, peer = Journal(store), Journal(await store_factory(store.path))
    await journal.intent(first["id"], "implement")
    await journal.intent(other["id"], "implement")
    counts = await asyncio.gather(journal.recover(), peer.recover())
    assert sum(counts) == 2
    assert all(entry["run_id"] == first["id"] for entry in await journal.entries(first["id"]))
    assert all(entry["run_id"] == other["id"] for entry in await journal.entries(other["id"]))


async def test_completed_effect_cannot_be_overwritten_by_late_error(store):
    from firefly_forge.journal import Journal

    run = await seed(store)
    journal = Journal(store)
    effect = await journal.intent(run["id"], "implement")
    await journal.receipt(run["id"], effect["idempotency_key"], {"summary": "Completed"})
    with pytest.raises(ValueError, match="completed"):
        await journal.fail(run["id"], effect["idempotency_key"], "quota", "Stale failure")
    assert (await journal.plan(run["id"]))["tasks"][1]["status"] == "done"


async def test_rate_limit_resumes_only_after_known_reset_and_respects_retry_budget(store, monkeypatch):
    from firefly_forge.journal import Journal

    run = await seed(store)
    journal = Journal(store)
    current = 2000000000.0
    monkeypatch.setattr("firefly_forge.journal.time.time", lambda: current)
    effect = await journal.intent(run["id"], "implement")
    await journal.fail(run["id"], effect["idempotency_key"], "rate_limit", "Wait for reset", retry_after=30)
    assert await journal.ready_retries() == []
    current += 31
    assert await journal.ready_retries() == [run["id"]]
    resumed = await journal.resume(run["id"], auto=True)
    assert resumed["retry_count"] == 1
    assert resumed["resume_existing"] is True
    next_effect = await journal.intent(run["id"], "implement", attempt=2)
    await journal.fail(run["id"], next_effect["idempotency_key"], "quota", "Credit exhausted")
    current += 100000
    assert await journal.ready_retries() == []
    with pytest.raises(ValueError, match="operator"):
        await journal.resume(run["id"], auto=True)


async def test_explicit_failed_verification_allows_local_repair_but_not_remote_replay(store):
    from firefly_forge.journal import Journal

    run = await seed(store)
    journal = Journal(store)
    effect = await journal.intent(run["id"], "implement")
    await journal.receipt(run["id"], effect["idempotency_key"], {"summary": "First attempt"})
    repair = await journal.intent(
        run["id"],
        "implement",
        attempt=2,
        payload={"reason": "verification_repair", "failed_checks": [{"exit_code": 1, "output": "Wrong total"}]},
    )
    assert repair["disposition"] == "execute"
    publication = await journal.intent(run["id"], "publish")
    await journal.receipt(
        run["id"], publication["idempotency_key"], {"pr_url": "https://github.com/team/product/pull/1"}
    )
    assert (await journal.intent(run["id"], "publish", attempt=2))["disposition"] == "completed"


async def test_failed_remote_effect_preserves_successful_source_verification(store):
    from firefly_forge.journal import Journal

    run = await seed(store)
    await store.finish(run["id"], "succeeded")
    journal = Journal(store)
    effect = await journal.intent(run["id"], "deploy:staging")
    await journal.fail(run["id"], effect["idempotency_key"], "transient", "Remote target unavailable")
    assert (await store.get_run(run["id"]))["status"] == "succeeded"
    assert (await journal.plan(run["id"]))["tasks"][-1]["status"] == "paused"


async def test_prior_attempt_receipt_stays_idempotent_during_new_repair(store):
    from firefly_forge.journal import Journal

    run = await seed(store)
    journal = Journal(store)
    first = await journal.intent(run["id"], "implement")
    await journal.receipt(run["id"], first["idempotency_key"], {"summary": "Attempt one"})
    second = await journal.intent(
        run["id"],
        "implement",
        attempt=2,
        payload={"reason": "verification_repair", "failed_checks": [{"exit_code": 1}]},
    )
    assert second["disposition"] == "execute"
    assert (await journal.intent(run["id"], "implement", attempt=1))["disposition"] == "completed"
    await journal.receipt(run["id"], first["idempotency_key"], {"summary": "Repeated receipt"})
    assert (await journal.plan(run["id"]))["tasks"][1]["status"] == "running"


async def test_postgresql_refuses_updates_to_immutable_plan_and_journal(store):
    from sqlalchemy import text

    from firefly_forge.journal import Journal

    run = await seed(store)
    journal = Journal(store)
    await journal.ensure_plan(run["id"])
    for table in ["forge_journal", "forge_plans"]:
        with pytest.raises(Exception, match="immutable"):
            async with store._unit() as session:
                await session.execute(text(f"DELETE FROM {table}"))
    assert journal.verify_chain(await journal.entries(run["id"]))
    assert (await journal.plan(run["id"]))["revision"] == 1


async def test_retry_scheduler_does_not_lose_paused_runs_outside_recent_ui_history(store, monkeypatch):
    import copy
    import uuid

    from firefly_forge.journal import Journal
    from firefly_forge.persistence import ForgeRunEntity

    run = await seed(store)
    journal = Journal(store)
    effect = await journal.intent(run["id"], "implement")
    await journal.fail(run["id"], effect["idempotency_key"], "rate_limit", "Wait briefly", retry_after=1)
    async with store._unit():
        await store.run_records.save_all(
            [
                ForgeRunEntity(
                    id=(key := uuid.uuid4()),
                    repository_id=uuid.UUID(run["repository_id"]),
                    status="succeeded",
                    data={**copy.deepcopy(run), "id": key.hex, "status": "succeeded"},
                )
                for _ in range(501)
            ]
        )
        assert run["id"] not in [row.id.hex for row in await store.run_records.find_latest()]
    import time

    elapsed = time.time() + 2
    monkeypatch.setattr("firefly_forge.journal.time.time", lambda: elapsed)
    assert await journal.ready_retries() == [run["id"]]
