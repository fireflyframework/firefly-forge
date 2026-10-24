# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Complete brand assets stay on disk; model context carries verified references."""

import copy
import hashlib
import json

import pytest
from fireflyframework_agentic.model_utils import serialize_model_messages
from pydantic_ai.messages import ModelResponse, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel
from test_agentic_checkpoint_activation_host import RUN, activated, lost_completion

from firefly_forge.agentic_runtime import execute_agentic
from firefly_forge.engine import Engine
from firefly_forge.product_branding import BrandInput, ProductBranding
from firefly_forge.workspace_tools import WorkspaceTools


@pytest.fixture
async def brand_case(tmp_path, monkeypatch):
    env = await activated(tmp_path, monkeypatch)
    brand = await ProductBranding(env.store).configure(
        env.store.run.data["repository_id"], BrandInput(name="Support CRM")
    )
    env.store.run.data["branding_snapshot"] = brand
    try:
        yield env, brand
    finally:
        await env.model.client.close()


async def test_frozen_brand_manifest_preserves_every_full_utf8_asset(brand_case):
    _, brand = brand_case
    manifest = Engine._branding_manifest(brand)
    assert len(json.dumps(manifest)) < 4000
    assert sum(len(text.encode()) for text in brand["files"].values()) > 30000
    assert len(manifest["files"]) == len(brand["files"])
    for item in manifest["files"]:
        name = item["path"].rsplit("/", 1)[1]
        assert item["path"] == "docs/brand/" + brand["digest"] + "/" + name
        assert item["bytes"] == len(brand["files"][name].encode())
        assert item["sha256"] == hashlib.sha256(brand["files"][name].encode()).hexdigest()
    assert "data:font" not in json.dumps(manifest) and "<svg" not in json.dumps(manifest)


@pytest.mark.parametrize("change", ["digest", "path"])
async def test_brand_projection_refuses_tampering_before_any_file_effect(brand_case, change):
    _, brand = brand_case
    value = copy.deepcopy(brand)
    if change == "digest":
        value["files"]["brand-guide.md"] += "changed"
    else:
        value["files"]["../outside.css"] = "unsafe"
        value["digest"] = hashlib.sha256(json.dumps(value["files"], sort_keys=True).encode()).hexdigest()
    with pytest.raises(ValueError, match="branding"):
        Engine._branding_manifest(value)


async def test_native_brand_materialization_precedes_the_provider_and_is_idempotent(brand_case, monkeypatch):
    env, brand = brand_case
    posts, _, _ = await lost_completion(env, monkeypatch)
    manifest = Engine._branding_manifest(brand)
    for item in manifest["files"]:
        path = env.workspace / item["path"]
        assert path.read_text() == brand["files"][path.name]
    wire = json.loads(posts[0])
    assert "data:font/woff2;base64," not in json.dumps(wire)
    assert "<svg" not in json.dumps(wire)
    assert all(item["path"] in json.dumps(wire) for item in manifest["files"])
    before = {item["path"]: (env.workspace / item["path"]).stat().st_mtime_ns for item in manifest["files"]}
    await env.engine._materialize_branding(RUN, env.workspace, {})
    assert before == {item["path"]: (env.workspace / item["path"]).stat().st_mtime_ns for item in manifest["files"]}


async def test_conflicting_frozen_brand_content_is_never_overwritten(brand_case):
    env, brand = brand_case
    path = env.workspace / "docs/brand" / brand["digest"] / "tokens.json"
    path.parent.mkdir(parents=True)
    path.write_text("existing user content")
    with pytest.raises(ValueError, match="brand|Brand"):
        await env.engine._materialize_branding(RUN, env.workspace, {})
    assert path.read_text() == "existing user content"
    assert len(list(path.parent.iterdir())) == 1


async def test_full_brand_files_keep_credential_admission_before_staging(brand_case):
    env, brand = brand_case
    secret = "configured-example-secret-89d9787019"
    value = copy.deepcopy(brand)
    value["files"]["brand-guide.md"] += secret
    value["digest"] = hashlib.sha256(json.dumps(value["files"], sort_keys=True).encode()).hexdigest()
    env.store.run.data["branding_snapshot"] = value
    with pytest.raises(ValueError, match="credential"):
        await env.engine._materialize_branding(RUN, env.workspace, {"FORGE_TEST_SECRET": secret})
    assert not (env.workspace / "docs/brand").exists()


async def test_brand_manifest_paths_are_relative_to_the_selected_monorepo_component(brand_case):
    env, brand = brand_case
    env.store.run.data["component_snapshot"] = {"path": "apps/customer-ui"}
    await env.engine._materialize_branding(RUN, env.workspace, {})
    manifest = Engine._branding_manifest(brand)
    for item in manifest["files"]:
        target = env.workspace / "apps/customer-ui" / item["path"]
        assert hashlib.sha256(target.read_bytes()).hexdigest() == item["sha256"]
    assert not (env.workspace / "docs/brand").exists()
    assert (await env.store.get_run(RUN))["branding_materialized_paths"] == [
        "apps/customer-ui/" + item["path"] for item in manifest["files"]
    ]


async def test_late_brand_conflict_is_refused_by_the_existing_file_cas(brand_case, monkeypatch):
    env, brand = brand_case
    write = WorkspaceTools.write_file

    async def conflict(tools, path, content, **kwargs):
        if path.endswith("/tokens.json"):
            target = tools.root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("late user content")
        return await write(tools, path, content, **kwargs)

    monkeypatch.setattr(WorkspaceTools, "write_file", conflict)
    with pytest.raises(ValueError, match="hash changed"):
        await env.engine._materialize_branding(RUN, env.workspace, {})
    target = env.workspace / "docs/brand" / brand["digest"] / "tokens.json"
    assert target.read_text() == "late user content"
    assert not (await env.store.get_run(RUN)).get("branding_materialized_digest")


async def test_complete_brief_occurs_once_in_native_model_context(tmp_path):
    marker = "SINGLE_CUSTOMER_REQUIREMENT_982fa70a"
    brief = marker + "\nBuild the customer workflow without dropping any requirements."
    observed = []

    def model(messages, info):
        text = json.dumps(serialize_model_messages(messages)) + (getattr(info, "instructions", "") or "")
        observed.append(text.count(marker))
        if not any(isinstance(part, ToolReturnPart) for message in messages for part in message.parts):
            return ModelResponse(parts=[ToolCallPart("write_file", {"path": "service.py", "content": "VALUE = 1\n"})])
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {"summary": "Context inspected"})])

    async def emit(*args):
        pass

    await execute_agentic(tmp_path, brief, "pyfly", "react", emit, model=FunctionModel(model))
    assert observed == [1, 1]
