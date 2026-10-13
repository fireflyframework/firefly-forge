# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Named model choices must reach real invocation settings and remain immutable."""

import pytest

from firefly_forge.model_profiles import ModelProfileInput, ModelProfiles
from firefly_forge.models import RepositoryInput
from firefly_forge.product_assistant import AssistantSessionInput, AssistantTurnInput, ProductAssistant
from firefly_forge.product_assistant_runtime import AssistantReply
from firefly_forge.task_routing import TaskRouting, TaskRoutingInput


@pytest.mark.asyncio
async def test_functional_dialogue_invokes_frozen_named_model_without_bridge(store, tmp_path):
    profiles = ModelProfiles(store.configuration)
    profiles.save(
        "functional",
        ModelProfileInput(
            name="Functional design",
            provider="agentic",
            values={
                "model": "model-functions-v1",
                "api_key": "fictional-functional-key-32chars",
                "endpoint": "https://models.example/v1",
            },
        ),
    )
    routing = TaskRouting(profiles)
    await routing.save(TaskRoutingInput(bindings={"functions": "functional"}))
    store.task_router = routing
    repo = await store.add_repository(RepositoryInput(name="Reservations", url=str(tmp_path)))
    observed = []

    async def designer(prompt, settings):
        observed.append((prompt, settings))
        return AssistantReply(message="Reservation behavior proposal")

    assistant = ProductAssistant(store, tmp_path / "dialogue", designer=designer)
    session = await assistant.create(repo["id"], AssistantSessionInput(task="functions"))
    profiles.save(
        "functional",
        ModelProfileInput(name="Functional design", provider="agentic", values={"model": "model-functions-v2"}),
    )
    result = await assistant.turn(
        repo["id"], session["id"], AssistantTurnInput(request_id="e" * 32, message="Map reservation failures")
    )
    assert result["status"] == "completed"
    assert observed[0][1]["FORGE_AGENTIC_MODEL"] == "model-functions-v1"
    assert observed[0][1]["ASSISTANT_PROVIDER"] == "agentic"
    assert "functions" in observed[0][0]
    assert result["context"]["model_selection"]["profile_id"] == "functional"
    assert session["model_selection"]["task"] == "functions"
    assert "fictional-functional-key" not in str(session)


@pytest.mark.asyncio
async def test_delivery_freezes_distinct_implementation_and_review_models(store, tmp_path):
    from firefly_forge.models import RunInput

    profiles = ModelProfiles(store.configuration)
    for name, model in [("builder", "build-v1"), ("reviewer", "review-v1")]:
        profiles.save(
            name,
            ModelProfileInput(
                name=name, provider="agentic", values={"model": model, "api_key": "fictional-" + name + "-key-32chars"}
            ),
        )
    routing = TaskRouting(profiles)
    await routing.save(TaskRoutingInput(bindings={"implementation": "builder", "review": "reviewer"}))
    store.task_router = routing
    repo = await store.add_repository(RepositoryInput(name="Reservations", url=str(tmp_path)))
    run = await store.create_run(RunInput(repository_id=repo["id"], brief="Reservation change"))
    assert run["agent"] == "agentic"
    assert run["assurance"]["review_agent"] == "agentic"
    assert store.configuration.load_snapshot(run["config_revision"])["FORGE_AGENTIC_MODEL"] == "build-v1"
    assert store.configuration.load_snapshot(run["review_config_revision"])["FORGE_AGENTIC_MODEL"] == "review-v1"
    profiles.save("builder", ModelProfileInput(name="Builder", provider="agentic", values={"model": "build-v2"}))
    assert profiles.validate_snapshot(run["model_selection"])["FORGE_AGENTIC_MODEL"] == "build-v1"
    assert run["review_model_selection"]["profile_id"] == "reviewer"
    assert "fictional-" not in str(run)


@pytest.mark.asyncio
async def test_queued_delivery_invokes_distinct_frozen_builder_and_reviewer(store, tmp_path, monkeypatch):
    import sys

    from test_assurance import Builder, Reviewer
    from test_execution import seed_repository

    from firefly_forge import providers
    from firefly_forge.assurance import Assurance
    from firefly_forge.engine import Engine
    from firefly_forge.journal import Journal
    from firefly_forge.models import Command, RunInput

    profiles = ModelProfiles(store.configuration)
    for name in ("builder", "reviewer"):
        profiles.save(
            name,
            ModelProfileInput(
                name=name,
                provider="agentic",
                values={"model": name + "-v1", "api_key": "fictional-" + name + "-key-32chars"},
            ),
        )
    routing = TaskRouting(profiles)
    await routing.save(TaskRoutingInput(bindings={"implementation": "builder", "review": "reviewer"}))
    store.task_router = routing
    source = await seed_repository(tmp_path / "source")
    repo = await store.add_repository(RepositoryInput(name="Inventory", url=str(source)))
    run = await store.create_run(
        RunInput(
            repository_id=repo["id"],
            brief="Reserve inventory",
            skills=[],
            commands=[Command(argv=[sys.executable, "-c", "from product import value; assert value == 7"])],
        )
    )
    profiles.save("builder", ModelProfileInput(name="Builder", provider="agentic", values={"model": "builder-v2"}))
    observed = []

    def factory(agent, settings=None):
        observed.append((agent, settings["FORGE_AGENTIC_MODEL"]))
        return Builder() if settings["FORGE_AGENTIC_MODEL"] == "builder-v1" else Reviewer()

    factory.__module__ = "firefly_forge.providers"
    monkeypatch.setattr(providers, "get_provider", factory)
    journal = Journal(store)
    engine = Engine(store, tmp_path / "jobs", journal=journal)
    engine.assurance = Assurance(store, tmp_path / "reviews", journal=journal)
    await engine.execute_run(run["id"])
    result = await store.get_run(run["id"])
    assert result["status"] == "succeeded", result.get("error")
    assert observed == [("agentic", "builder-v1"), ("agentic", "reviewer-v1")]
    assert result["review"]["status"] == "approved"
    assert journal.verify_chain(await journal.entries(run["id"]))
