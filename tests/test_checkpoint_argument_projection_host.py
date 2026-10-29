# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Argument custody uses public SDK normalization without invoking callbacks."""

import copy
import hashlib
from typing import Any

import pytest
from pydantic_ai.messages import ToolCallPart
from pydantic_ai.tools import Tool

from firefly_forge.agentic_tool_results import encoded
from firefly_forge.checkpoint_continuation_proof import _retained_argument, _tool_arguments
from firefly_forge.journal import digest
from firefly_forge.worker_evidence_query import WorkerEvidenceReadInput


def _list_schema():
    async def list_files(path_prefix: str = "", cursor: str | None = None, page_size: int = 100) -> dict[str, Any]:
        raise AssertionError("Schema inspection must never invoke the tool")

    return Tool(list_files).tool_def.parameters_json_schema


def test_schema_compilation_is_reused_without_caching_call_arguments(monkeypatch):
    from firefly_forge import invalid_read_reconstruction as reconstruction

    schema = _list_schema()
    built = []
    original = reconstruction.Tool

    def compiler(*args, **kwargs):
        built.append(kwargs.get("name"))
        return original(*args, **kwargs)

    monkeypatch.setattr(reconstruction, "Tool", compiler)
    assert reconstruction._ordered_schema("list_files", schema) == schema
    assert reconstruction._ordered_schema("list_files", schema) == schema
    assert len(built) <= 1


def test_cached_schema_still_checks_each_capture_and_returns_unshared_metadata():
    from firefly_forge import invalid_read_reconstruction as reconstruction

    schema = _list_schema()
    first = reconstruction._ordered_schema("list_files", schema)
    first["properties"]["page_size"]["default"] = 999
    assert reconstruction._ordered_schema("list_files", schema)["properties"]["page_size"]["default"] == 100
    changed = copy.deepcopy(schema)
    changed["properties"]["page_size"]["type"] = "string"
    with pytest.raises(ValueError, match="frozen original"):
        reconstruction._ordered_schema("list_files", changed)


def test_signature_defaults_and_coercions_preserve_the_original_raw_arguments():
    async def list_files(path_prefix: str = "", cursor: str | None = None, page_size: int = 100) -> dict[str, Any]:
        raise AssertionError("Argument inspection must never invoke the tool")

    schema = Tool(list_files).tool_def.parameters_json_schema
    original = ' {"page_size":"17"} '
    raw, public = _tool_arguments(ToolCallPart("list_files", original, "call-list"), schema)
    assert raw == original
    assert public == {"path_prefix": "", "cursor": None, "page_size": 17}


def test_from_schema_evidence_tool_keeps_its_raw_dictionary_projection():
    original = '{"query":{"kind":"tool","limit":5}}'
    raw, public = _tool_arguments(
        ToolCallPart("read_run_evidence", original, "call-evidence"), WorkerEvidenceReadInput.model_json_schema()
    )
    assert raw == original
    assert public == {"query": {"kind": "tool", "limit": 5}}


def test_large_argument_descriptor_is_derived_without_reading_or_replaying_an_effect():
    expected = {"content": "x" * 130_000}
    raw = encoded(expected)
    descriptor = {
        "version": 1,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "bytes": len(raw),
        "media_type": "application/json",
    }
    payload = {"arguments_sha256": digest({"arguments": expected}), "arguments_artifact": descriptor}
    _retained_argument(payload, "arguments", expected)


@pytest.mark.parametrize(
    "field,value", [("version", True), ("sha256", "0" * 64), ("bytes", 1), ("media_type", "text/plain")]
)
def test_argument_artifact_metadata_drift_is_rejected(field, value):
    expected = {"content": "known source"}
    raw = encoded(expected)
    descriptor = {
        "version": 1,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "bytes": len(raw),
        "media_type": "application/json",
    }
    descriptor[field] = value
    with pytest.raises(ValueError, match="artifact"):
        _retained_argument(
            {"arguments_sha256": digest({"arguments": expected}), "arguments_artifact": descriptor},
            "arguments",
            expected,
        )


def test_inline_boolean_and_numeric_arguments_are_not_equivalent():
    expected = {"refresh": 1}
    payload = {"arguments_sha256": digest({"arguments": expected}), "arguments": {"refresh": True}}
    with pytest.raises(ValueError, match="arguments"):
        _retained_argument(payload, "arguments", expected)


def test_ambiguous_inline_and_artifact_arguments_are_rejected():
    expected = {"path": "existing.py"}
    payload = {
        "arguments_sha256": digest({"arguments": expected}),
        "arguments": copy.deepcopy(expected),
        "arguments_artifact": {},
    }
    with pytest.raises(ValueError, match="arguments"):
        _retained_argument(payload, "arguments", expected)
