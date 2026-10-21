# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
import importlib

import pytest

from firefly_forge.configuration import ConfigurationStore


async def test_task_bindings_select_independent_frozen_models_without_global_environment_edits(tmp_path):
    profiles_mod = importlib.import_module("firefly_forge.model_profiles")
    routing_mod = importlib.import_module("firefly_forge.task_routing")
    profiles = profiles_mod.ModelProfiles(ConfigurationStore(tmp_path))
    for name, model in (("planning", "openai-responses:model-a"), ("coding", "anthropic:model-b")):
        profiles.save(
            name,
            profiles_mod.ModelProfileInput(
                name=name, provider="agentic", values={"model": model, "api_key": name + "-key"}
            ),
        )
    routing = routing_mod.TaskRouting(profiles)
    assert await routing.resolve("implementation") is None
    saved = await routing.save(
        routing_mod.TaskRoutingInput(bindings={"product": "planning", "implementation": "coding"})
    )
    planner, coder = await routing.resolve("product"), await routing.resolve("implementation")
    assert planner["profile_id"] == "planning" and coder["profile_id"] == "coding"
    assert planner["routing_revision"] == saved["revision"]
    assert profiles.validate_snapshot(coder)["FORGE_AGENTIC_MODEL"] == "anthropic:model-b"
    await routing.save(routing_mod.TaskRoutingInput(bindings={"product": "coding"}))
    assert profiles.validate_snapshot(planner)["FORGE_AGENTIC_MODEL"] == "openai-responses:model-a"


async def test_invalid_task_or_model_binding_does_not_replace_saved_routes(tmp_path):
    profiles_mod = importlib.import_module("firefly_forge.model_profiles")
    routing_mod = importlib.import_module("firefly_forge.task_routing")
    profiles = profiles_mod.ModelProfiles(ConfigurationStore(tmp_path))
    profiles.save("cli", profiles_mod.ModelProfileInput(name="cli", provider="codex"))
    routing = routing_mod.TaskRouting(profiles)
    with pytest.raises(ValueError):
        await routing.save(routing_mod.TaskRoutingInput(bindings={"product": "cli"}))
    assert routing.current()["bindings"] == {}
    with pytest.raises(ValueError):
        routing_mod.TaskRoutingInput(bindings={"imaginary": "cli"})

async def test_communication_settings_refuse_stale_routing_without_overwriting_other_tasks(tmp_path):
    from firefly_forge.model_profiles import ModelProfileInput, ModelProfiles
    from firefly_forge.task_routing import TaskRouting, TaskRoutingInput
    profiles = ModelProfiles(ConfigurationStore(tmp_path))
    profiles.save("updates", ModelProfileInput(name="Updates", provider="bridge", values={"endpoint": "http://127.0.0.1:12345/v1", "token": "fixture-token"}))
    routing = TaskRouting(profiles)
    original = routing.current()
    current = await routing.save(TaskRoutingInput(bindings={"product": "updates"}))
    with pytest.raises(ValueError, match="changed"):
        await routing.save(TaskRoutingInput(bindings={"communication": "updates"}, expected_digest=original["digest"]))
    assert routing.current() == current
    saved = await routing.save(TaskRoutingInput(bindings={**current["bindings"], "communication": "updates"}, expected_digest=current["digest"]))
    assert saved["bindings"] == {"product": "updates", "communication": "updates"}
