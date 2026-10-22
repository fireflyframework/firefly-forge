# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Canonical request contracts across retained native SDK versions; no model calls."""

import hashlib
from dataclasses import dataclass, field

import pytest
from pydantic_ai.messages import ModelRequest, UserPromptPart
from pydantic_ai.models import ModelRequestContext, ModelRequestParameters
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.tools import RunContext
from pydantic_ai.usage import RunUsage, UsageLimits

from firefly_forge.agentic_checkpoint_boundary import AgenticBoundary, BoundaryCheckpoint


@dataclass(kw_only=True)
class TelemetryParameters(ModelRequestParameters):
    _telemetry_mcp_url_ids: set[str] | None = None
    _telemetry_mcp_url_toolset_ids: set[str] = field(default_factory=set)
    _telemetry_mcp_url_tool_names: set[str] | None = None


def request(parameters):
    def unused(*args):
        raise AssertionError("Contract fixtures must not request a model")

    model = FunctionModel(unused)
    history = [ModelRequest(parts=[UserPromptPart("Fictional contract fixture")])]
    context = RunContext(deps=None, model=model, usage=RunUsage(), usage_limits=UsageLimits(), messages=history)
    return context, ModelRequestContext(
        model=model, messages=history, model_settings=None, model_request_parameters=parameters
    )


FIELDS = ("_telemetry_mcp_url_ids", "_telemetry_mcp_url_toolset_ids", "_telemetry_mcp_url_tool_names")


async def test_checkpoint_captures_actual_native_request_parameters():
    parameters = ModelRequestParameters()
    boundary = AgenticBoundary(lambda: {"files_written": []})
    context, pending = request(parameters)
    with boundary.invocation():
        await boundary.before_model_request(context, pending)
        checkpoint = boundary.capture(b"actual-native-wire")
    contract = checkpoint.record.contract["parameters"]
    if hasattr(parameters, "_telemetry_mcp_url_toolset_ids"):
        assert contract["_telemetry_mcp_url_toolset_ids"] == []
        assert contract["_telemetry_mcp_url_ids"] is None
        assert contract["_telemetry_mcp_url_tool_names"] is None
    assert checkpoint.wire_sha256 == hashlib.sha256(b"actual-native-wire").hexdigest()


@pytest.mark.parametrize("name", ["_telemetry_mcp_url_ids", "_telemetry_mcp_url_tool_names"])
async def test_checkpoint_preserves_unknown_telemetry_provenance_instead_of_empty_set(name):
    parameters = TelemetryParameters()
    boundary = AgenticBoundary(lambda: {"files_written": []})
    context, pending = request(parameters)
    with boundary.invocation():
        await boundary.before_model_request(context, pending)
        checkpoint = boundary.capture(b"fictional-wire")
        assert checkpoint.record.contract["parameters"][name] is None
        setattr(parameters, name, set())
        with pytest.raises(ValueError, match="dynamic"):
            boundary.capture(b"fictional-wire")


async def test_checkpoint_requires_toolset_telemetry_provenance_to_be_a_set():
    parameters = TelemetryParameters(_telemetry_mcp_url_toolset_ids=None)
    boundary = AgenticBoundary(lambda: {"files_written": []})
    context, pending = request(parameters)
    with boundary.invocation():
        await boundary.before_model_request(context, pending)
        with pytest.raises(ValueError, match="telemetry"):
            boundary.capture(b"fictional-wire")


@pytest.mark.parametrize("name", FIELDS)
@pytest.mark.parametrize("values", [set(), {"zeta", "alpha"}])
async def test_checkpoint_retains_known_telemetry_sets_in_canonical_order(name, values):
    parameters = TelemetryParameters(**{name: values})
    boundary = AgenticBoundary(lambda: {"files_written": []})
    context, pending = request(parameters)
    with boundary.invocation():
        await boundary.before_model_request(context, pending)
        first = boundary.capture(b"exact-fictional-wire")
        expected = [] if not values else ["alpha", "zeta"]
        assert first.record.contract["parameters"][name] == expected
        assert first.digest == hashlib.sha256(first.payload).hexdigest()
        assert first.wire_sha256 == hashlib.sha256(b"exact-fictional-wire").hexdigest()
        setattr(parameters, name, set(reversed(expected)))
        second = boundary.capture(b"exact-fictional-wire")
        assert second.payload == first.payload and second.digest == first.digest
        assert BoundaryCheckpoint.decode(first.payload, first.digest).record.contract == first.record.contract


@pytest.mark.parametrize("name", FIELDS)
async def test_changed_telemetry_contract_refuses_replay_even_with_identical_wire(name):
    parameters = TelemetryParameters(**{name: {"original"}})
    boundary = AgenticBoundary(lambda: {"files_written": []})
    context, pending = request(parameters)
    with boundary.invocation():
        await boundary.before_model_request(context, pending)
        checkpoint = boundary.capture(b"same-fictional-wire")
        setattr(parameters, name, {"changed"})
        with pytest.raises(ValueError, match="dynamic"):
            boundary.capture(b"same-fictional-wire")
    restored = AgenticBoundary(lambda: checkpoint.state, restore=checkpoint)
    context, pending = request(parameters)
    with restored.invocation():
        await restored.before_model_request(context, pending)
        with pytest.raises(ValueError, match="state"):
            restored.capture(b"same-fictional-wire")


@pytest.mark.parametrize("name", FIELDS)
@pytest.mark.parametrize("value", [["tool"], {"tool": True}, {1}, {"tool", 1}])
async def test_checkpoint_refuses_malformed_telemetry_sets(name, value):
    parameters = TelemetryParameters(**{name: value})
    boundary = AgenticBoundary(lambda: {"files_written": []})
    context, pending = request(parameters)
    with boundary.invocation():
        await boundary.before_model_request(context, pending)
        with pytest.raises(ValueError, match="telemetry"):
            boundary.capture(b"fictional-wire")


async def test_unknown_nonserializable_contract_field_is_never_coerced_or_dropped():
    @dataclass(kw_only=True)
    class UnknownParameters(TelemetryParameters):
        future_contract: set[str] = field(default_factory=lambda: {"unsupported"})

    boundary = AgenticBoundary(lambda: {"files_written": []})
    context, pending = request(UnknownParameters())
    with boundary.invocation():
        await boundary.before_model_request(context, pending)
        with pytest.raises(TypeError, match="not JSON serializable"):
            boundary.capture(b"fictional-wire")
