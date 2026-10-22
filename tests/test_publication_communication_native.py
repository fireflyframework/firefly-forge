# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Real PyFly Store/Journal custody and identity checks; public Git and no remote or model service."""

import copy
import hashlib
import json

import httpx
import pytest
import pytest_asyncio
from communication_model_fixture import communication_phase, reviewed_model
from pydantic_ai.models.function import FunctionModel
from test_operations import release_fixture
from test_publication_communication_host import card

from firefly_forge.configuration import ProviderConfiguration
from firefly_forge.journal import Journal
from firefly_forge.model_profiles import ModelProfileInput, ModelProfiles
from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied, ProjectAccess
from firefly_forge.store import identifier
from firefly_forge.task_routing import TaskRouting, TaskRoutingInput


@pytest_asyncio.fixture
async def native_publication(store, store_factory, tmp_path, monkeypatch):
    import firefly_forge.engine as module

    manager, run, project, owner, builder, _ = await release_fixture(store, tmp_path)
    engine = manager.engine
    workspace = engine.workspaces / run["id"]
    files, _, _ = await engine._snapshot(workspace, run["base_revision"])
    checks = [{**value, "attempt": 1} for value in run["final_verification"]]
    await store.update(
        run["id"], changed_files=files, summary="Added bounded health checks.", attempt=1, final_verification=checks
    )
    await engine._checkpoint_commit(run["id"], workspace)
    profiles = ModelProfiles(store.configuration)
    profiles.save(
        "updates",
        ModelProfileInput(
            name="Publication fixture",
            provider="agentic",
            values={"model": "openai-responses:fixture", "api_key": "public-fixture-model-key"},
        ),
    )
    store.task_router = TaskRouting(profiles)
    await store.task_router.save(TaskRoutingInput(bindings={"communication": "updates"}))
    engine.journal = Journal(store)
    await engine.journal.ensure_plan(run["id"])
    repository = await store.repository(run["repository_id"])
    async with store._unit():
        row = await store.repository_records.find_by_id(identifier(repository["id"]))
        row.data = {**row.data, "provider": "github", "url": "https://github.com/fixture/public"}
        await store.repository_records.save(row)
    shadow = await store_factory(store.path)
    state = {"effects": [], "requests": [], "calls": [], "shadow": shadow}
    engine.communication_writer.model = reviewed_model(card(), calls=state["calls"])

    async def push(*_args, **_kwargs):
        retained = await shadow.get_run(run["id"])
        state["durable_before_push"] = copy.deepcopy(retained["publication_draft"])
        state["entries_before_push"] = await Journal(shadow).entries(run["id"])
        state["effects"].append("push")
        if action := state.get("after_push"):
            await action()

    original_client = httpx.AsyncClient

    def transport(request):
        state["effects"].append("pr")
        state["requests"].append(json.loads(request.content))
        if state.get("lost_response"):
            raise httpx.ReadTimeout("Public fixture response not retained")
        return httpx.Response(201, json={"html_url": "https://github.com/fixture/public/pull/1"})

    monkeypatch.setattr(module, "push", push)
    monkeypatch.setattr(
        module.httpx,
        "AsyncClient",
        lambda **kwargs: original_client(transport=httpx.MockTransport(transport), **kwargs),
    )
    monkeypatch.setenv("FORGE_GITHUB_TOKEN", "public-fixture-scm-token")
    token = CURRENT_PRINCIPAL.set(owner)
    try:
        yield engine, store, await store.get_run(run["id"]), project, owner, builder, workspace, state
    finally:
        CURRENT_PRINCIPAL.reset(token)


async def test_native_ai_publication_draft_commits_before_intent_and_exact_payload(native_publication):
    engine, store, run, _, owner, builder, _, state = native_publication
    url = await engine.publish(run["id"])
    draft = state["durable_before_push"]
    assert draft["title"] == "Add health checks" and draft["communication"]["mode"] == "ai"
    assert draft["body_sha256"] == hashlib.sha256(draft["body"].encode()).hexdigest()
    intent = next(e for e in state["entries_before_push"] if e["task_id"] == "publish" and e["kind"] == "intent")
    assert intent["payload"]["publication_body_sha256"] == draft["body_sha256"]
    assert intent["payload"]["publication_title_sha256"] == draft["title_sha256"]
    assert state["requests"][0]["title"] == draft["title"]
    assert state["requests"][0]["body"] == draft["body"]
    saved = await state["shadow"].get_run(run["id"])
    assert saved["publication_actor"]["user_id"] == owner.id.hex
    assert saved["execution_actor"]["user_id"] == builder.id.hex
    assert saved["publication_draft"] == draft
    assert await engine.publish(run["id"]) == url
    assert len(state["calls"]) == 2 and state["effects"] == ["push", "pr"]


async def test_native_selected_ai_failure_commits_empty_hold_without_publish_intent(native_publication):
    engine, _, run, _, _, _, _, state = native_publication

    def unavailable(*_args):
        state["calls"].append("failed")
        raise RuntimeError("Public fixture model unavailable")

    engine.communication_writer.model = FunctionModel(unavailable)
    with pytest.raises(ValueError, match="held"):
        await engine.publish(run["id"])
    saved = await state["shadow"].get_run(run["id"])
    assert saved["publication_draft"]["body"] == "" and saved["publication_draft"]["title"] == ""
    assert saved["publication_draft"]["communication"]["mode"] == "held"
    assert not any(e["task_id"] == "publish" and e["kind"] == "intent" for e in await engine.journal.entries(run["id"]))
    assert state["effects"] == [] and state["calls"] == ["failed"]


async def test_native_recorded_mode_is_explicit_and_does_not_call_model(native_publication):
    engine, store, run, _, _, _, _, state = native_publication
    await store.task_router.save(TaskRoutingInput(bindings={}))
    await engine.publish(run["id"])
    saved = await state["shadow"].get_run(run["id"])
    assert saved["publication_draft"]["communication"]["mode"] == "deterministic"
    assert state["requests"][0]["body"].startswith("Implemented by firefly forge.")
    assert state["calls"] == []


@pytest.mark.parametrize("boundary", ["during_draft", "after_push"])
async def test_native_actual_release_membership_revocation_stops_next_effect(native_publication, boundary):
    from pydantic_ai.messages import ModelResponse, ToolCallPart

    engine, store, run, project, owner, builder, _, state = native_publication
    access = ProjectAccess(store)
    await access.set_membership(project["id"], builder, "owner")

    async def revoke():
        await access.set_membership(project["id"], owner, "viewer")

    async def draft(_messages, info):
        phase = communication_phase(info)
        state["calls"].append(phase)
        if phase == "draft" and boundary == "during_draft":
            await revoke()
        payload = (
            card()
            if phase == "draft"
            else {
                "verdict": "supported",
                "issues": [],
                "fact_ids": ["publication", "verification", "delivery"],
                "rationale": "The public report and saved check support this candidate.",
            }
        )
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, payload)])

    engine.communication_writer.model = FunctionModel(draft)
    if boundary == "after_push":
        state["after_push"] = revoke
    with pytest.raises(AccessDenied):
        await engine.publish(run["id"])
    assert state["effects"] == (["push"] if boundary == "after_push" else [])
    if boundary == "during_draft":
        saved = await state["shadow"].get_run(run["id"])
        assert saved["publication_draft"]["body"] == ""


async def test_native_unknown_remote_outcome_keeps_original_sql_custody_without_replay(native_publication):
    engine, _, run, _, _, _, _, state = native_publication
    state["lost_response"] = True
    with pytest.raises(httpx.ReadTimeout):
        await engine.publish(run["id"])
    original = await state["shadow"].get_run(run["id"])
    assert original["publication_status"] == "publishing"
    entries = await engine.journal.entries(run["id"])
    assert len([e for e in entries if e["kind"] == "intent" and e["task_id"] == "publish"]) == 1
    assert not any(e["kind"] == "receipt" and e["task_id"] == "publish" for e in entries)
    with pytest.raises(ValueError, match="ambiguous"):
        await engine.publish(run["id"])
    saved = await state["shadow"].get_run(run["id"])
    assert saved["publication_draft"] == original["publication_draft"]
    assert len(state["calls"]) == 2 and state["effects"] == ["push", "pr"]


async def test_native_changed_source_during_ai_draft_holds_before_push(native_publication):
    from pydantic_ai.messages import ModelResponse, ToolCallPart

    engine, _, run, _, _, _, workspace, state = native_publication

    def draft(_messages, info):
        (workspace / "service.py").write_text("def status():\n    return 'changed'\n")
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, card())])

    engine.communication_writer.model = reviewed_model(draft)
    with pytest.raises(ValueError, match="held"):
        await engine.publish(run["id"])
    saved = await state["shadow"].get_run(run["id"])
    assert saved["publication_draft"]["body"] == "" and state["effects"] == []


async def test_native_configured_credential_never_enters_model_or_publication_custody(native_publication):
    engine, store, run, _, _, _, _, state = native_publication
    secret = "public-fixture-credential-sentinel-20261022"
    store.configuration.update("bridge", ProviderConfiguration(values={"token": secret}))
    await store.update(run["id"], summary=secret)
    with pytest.raises(ValueError, match="held"):
        await engine.publish(run["id"])
    saved = await state["shadow"].get_run(run["id"])
    assert secret not in json.dumps(saved["publication_draft"])
    assert saved["publication_draft"]["body"] == ""
    assert state["calls"] == [] and state["effects"] == []
