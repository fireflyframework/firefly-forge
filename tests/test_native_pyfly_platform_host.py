# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Native PyFly owns runtime templates and lossless retained message encoding."""

import importlib.util
import json
from importlib.metadata import version


def test_packaged_prompts_work_without_the_retired_agentic_distribution():
    from pyfly.agentic.prompts import PromptRegistry

    assert version("pyfly") == "26.10.16"
    assert importlib.util.find_spec("fireflyframework_agentic") is None
    from firefly_forge.prompts import prompt_catalog

    catalog = prompt_catalog()
    assert isinstance(catalog.registry, PromptRegistry)
    assert (
        catalog.text("implementation/request", brief="Café {{ unchanged }}") == "PRODUCT BRIEF:\nCafé {{ unchanged }}"
    )


def test_native_codec_preserves_retained_legacy_message_bytes():
    from pyfly.agentic.model_utils import deserialize_model_messages, serialize_model_messages

    # This is the public message encoding used by retained 26.10.1 checkpoints.
    history = json.loads(
        '[{"parts":[{"content":"Retained brief","timestamp":"2026-10-26T00:00:00Z",'
        '"part_kind":"user-prompt"}],"timestamp":"2026-10-26T00:00:00Z",'
        '"instructions":null,"kind":"request","run_id":null,"conversation_id":null,"metadata":null,"state":"complete"}]'
    )
    assert serialize_model_messages(deserialize_model_messages(history)) == history


async def test_native_checkpoint_preserves_missing_usage_provenance():
    from pydantic_ai.messages import ModelRequest, UserPromptPart
    from pydantic_ai.models import ModelRequestContext, ModelRequestParameters
    from pydantic_ai.models.function import FunctionModel
    from pydantic_ai.tools import RunContext
    from pydantic_ai.usage import RunUsage, UsageLimits

    from firefly_forge.agentic_checkpoint_boundary import AgenticBoundary, BoundaryCheckpoint

    def unused(*args):
        raise AssertionError("Checkpoint capture must not call a provider")

    model = FunctionModel(unused)
    history = [ModelRequest(parts=[UserPromptPart("Retained usage provenance")])]
    for count in (0, 2):
        usage = RunUsage(unmeasured_requests=count)
        context = RunContext(deps=None, model=model, usage=usage, usage_limits=UsageLimits(), messages=history)
        pending = ModelRequestContext(
            model=model, messages=history, model_settings=None, model_request_parameters=ModelRequestParameters()
        )
        boundary = AgenticBoundary(lambda: {"files_written": []})
        with boundary.invocation():
            await boundary.before_model_request(context, pending)
            checkpoint = boundary.capture(b"fictional-native-request")
        restored = BoundaryCheckpoint.decode(checkpoint.payload, checkpoint.digest)
        assert restored.usage.unmeasured_requests == count
        assert ("unmeasured_requests" in restored.record.usage) is (count != 0)


def test_api_provider_setup_points_to_the_native_platform(monkeypatch):
    from firefly_forge.providers import provider_catalog

    monkeypatch.setattr("firefly_forge.providers.shutil.which", lambda _: None)
    selected = next(item for item in provider_catalog() if item["id"] == "agentic")
    assert selected["setup_url"] == "https://github.com/fireflyframework/fireflyframework-pyfly"
