# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Frozen wireframe text is distinct from active QA tools or image history."""

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic_ai import models
from test_agentic_checkpoint_activation_host import (
    RUN,
    activated,
    controlled_source_snapshot,
    queue_original,
)
from test_agentic_checkpoint_static_tools_host import GRANTS, nothing
from test_bridge_completion_native import mock_model_requests
from test_product_design import definition, review

from firefly_forge.agent_checkpoint_models import CheckpointPayloadV2
from firefly_forge.agentic_checkpoint_boundary import AgenticBoundary
from firefly_forge.agentic_checkpoint_invocation import CheckpointInvocation, invocation_cut, runtime_contract
from firefly_forge.configuration import ConfigurationStore
from firefly_forge.engine import Engine
from firefly_forge.product_design import ProductDesign


@pytest.fixture
async def frozen_run(tmp_path):
    async def repository(key):
        return {"id": key}

    store = SimpleNamespace(configuration=ConfigurationStore(tmp_path / "design-configuration"), repository=repository)
    bundle = await ProductDesign(store).preview("a" * 32, definition())
    return {
        "id": RUN,
        "backend": "pyfly",
        "frontend": "react",
        "design_snapshot": bundle,
        "product_definition": {
            "revision": 1,
            "definition": definition().model_copy(update={"design_review": review(bundle)}).model_dump(mode="json"),
        },
    }


def attach(tmp_path, run, references, *, grants=GRANTS):
    invocation = CheckpointInvocation(
        SimpleNamespace(credential_registry=dict), SimpleNamespace(), run, {}, tmp_path, nothing
    )
    invocation.attach(
        AgenticBoundary(lambda: {"files_written": []}),
        "Build",
        "pyfly",
        run["frontend"],
        SimpleNamespace(allowed_tools=grants, qa_references=references),
        {},
    )
    return invocation


async def test_real_reviewed_wireframe_descriptors_attach_as_frozen_text(tmp_path, frozen_run):
    references = tuple(Engine._qa_reference_descriptors(frozen_run))
    assert len(references) == 12 and all("source_sha256" in row for row in references)
    assert "<svg" not in json.dumps(references)
    invocation = attach(tmp_path, frozen_run, references)
    assert invocation.binding is not None
    assert invocation.binding.run_cut == invocation_cut(frozen_run)
    assert invocation.binding.grants == sorted(GRANTS)


@pytest.mark.parametrize(
    "change", ["hash", "revision", "order", "review", "review_digest", "frontend", "float", "extra", "image", "missing"]
)
async def test_changed_or_unreviewed_reference_metadata_refuses_before_scope(tmp_path, frozen_run, change):
    references = copy.deepcopy(Engine._qa_reference_descriptors(frozen_run))
    if change == "hash":
        references[0]["source_sha256"] = "f" * 64
    elif change == "revision":
        frozen_run["design_snapshot"]["digest"] = "f" * 64
    elif change == "order":
        references.reverse()
    elif change == "review":
        frozen_run["product_definition"]["definition"]["design_review"]["reviewed"] = False
    elif change == "review_digest":
        frozen_run["product_definition"]["definition"]["design_review"]["digest"] = "f" * 64
    elif change == "frontend":
        frozen_run["frontend"] = "none"
    elif change == "float":
        references[0]["zoom"] = 100.0
    elif change == "extra":
        references[0]["arbitrary"] = "not a native descriptor"
    elif change == "image":
        references[0]["image"] = b"\x89PNG\r\n\x1a\n"
    else:
        references.clear()
    with pytest.raises(ValueError):
        attach(tmp_path, frozen_run, tuple(references))


@pytest.mark.parametrize("tool", ["browser_check", "browser_journey", "profile_python", "external_mcp"])
async def test_frozen_text_does_not_grant_active_qa_or_other_dynamic_tools(tmp_path, frozen_run, tool):
    references = tuple(Engine._qa_reference_descriptors(frozen_run))
    with pytest.raises(ValueError, match="explicit static"):
        attach(tmp_path, frozen_run, references, grants=GRANTS | {tool})


def test_descriptor_generator_engine_bytes_are_part_of_checkpoint_runtime_contract(monkeypatch):
    before = runtime_contract()
    read = Path.read_bytes

    def changed(path):
        value = read(path)
        return value + b"\n# controlled semantic drift" if path.name == "engine.py" else value

    monkeypatch.setattr(Path, "read_bytes", changed)
    assert runtime_contract() != before


async def test_original_result_restores_with_the_same_frozen_text_and_no_image_tools(tmp_path, monkeypatch, frozen_run):
    env = await activated(tmp_path, monkeypatch)
    env.store.run.data.update({key: value for key, value in frozen_run.items() if key != "id"})
    try:
        posts, _, checkpoint, _, request = await queue_original(env, monkeypatch, source="http_response")
        assert "source_sha256" in posts[0].decode()
        binding = CheckpointPayloadV2.model_validate(checkpoint["payload"]).invocation
        assert not {"browser_check", "browser_journey"}.intersection(binding.grants)
        assert binding.run_cut == invocation_cut(await env.store.get_run(RUN))

        async def snapshot(*args, **kwargs):
            return ["service.py"], controlled_source_snapshot("Frozen textual design fixture"), "c" * 40

        monkeypatch.setattr(env.engine, "_snapshot", snapshot)
        before = (env.workspace / "service.py").stat().st_mtime_ns
        with models.override_allow_model_requests(False), mock_model_requests(env.model):
            await env.restores.execute(request.request_id)
        assert (await env.requests.get(request.request_id))["status"] == "completed"
        assert len(posts) == 2 and (env.workspace / "service.py").stat().st_mtime_ns == before
        assert len([row for row in env.journal.records.rows if row.kind == "agent_consume"]) == 2
    finally:
        await env.model.client.close()
