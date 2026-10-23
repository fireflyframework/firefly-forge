# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Actual publication flow with public Git, controlled AI and in-memory custody; no remote effects."""

import copy
import hashlib
import json
from types import SimpleNamespace

import httpx
import pytest
from communication_model_fixture import reviewed_model
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.function import FunctionModel
from test_communication_writer import configured_store
from test_execution import seed_repository

from firefly_forge.communication import CommunicationWriter
from firefly_forge.engine import Engine
from firefly_forge.process import checked
from firefly_forge.secrets import contains_known_credential


def card(title="Add health checks", text="The worker reports bounded health checks. One saved check passed."):
    return {
        "title": {"text": title, "fact_ids": ["publication"]},
        "paragraphs": [{"text": text, "fact_ids": ["publication", "verification", "delivery"]}],
    }


@pytest.fixture
async def publication(tmp_path, monkeypatch):
    import firefly_forge.engine as module

    configured = await configured_store(tmp_path / "private")
    run_id = "a" * 32
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    workspace = await seed_repository(jobs / run_id)
    base = await checked(["git", "rev-parse", "HEAD"], workspace)
    (workspace / "health.py").write_text("HEALTH = True\n")
    await checked(["git", "add", "health.py"], workspace)
    await checked(["git", "commit", "-m", "Add public health fixture"], workspace)
    revision = await checked(["git", "rev-parse", "HEAD"], workspace)
    tree = await checked(["git", "rev-parse", "HEAD^{tree}"], workspace)
    run = {
        "id": run_id,
        "repository_id": "b" * 32,
        "status": "succeeded",
        "attempt": 1,
        "pr_url": "",
        "branch": "forge/public-fixture",
        "brief": "PRIVATE_CUSTOMER_BRIEF",
        "summary": "Added bounded health checks.",
        "base_revision": base,
        "source_tree": tree,
        "checkpoint_revision": revision,
        "changed_files": ["health.py"],
        "final_verification": [{"argv": ["python3", "-m", "pytest"], "exit_code": 0, "attempt": 1}],
    }
    repository = {
        "id": "b" * 32,
        "provider": "github",
        "url": "https://github.com/org/public",
        "default_branch": "main",
    }
    state = {"effects": [], "requests": [], "deny_credentials": False, "secret": "fixture-private-credential"}

    class Store:
        path = tmp_path / "factory.db"
        runtime_roles = None
        task_router = configured.task_router

        async def get_run(self, identifier):
            assert identifier == run_id
            return copy.deepcopy(run)

        async def repository(self, identifier):
            assert identifier == repository["id"]
            return copy.deepcopy(repository)

        async def update(self, identifier, **fields):
            assert identifier == run_id
            self.reject_credentials(fields)
            run.update(copy.deepcopy(fields))

        async def update_publication(self, identifier, expected, *, ownership=None, **fields):
            keys = ("publication_draft", "publication_status", "publication_actor", "pr_url")
            if any(run.get(key) != expected.get(key) for key in keys):
                raise ValueError("Publication custody changed; no publication update was recorded")
            await self.update(identifier, **fields)

        async def event(self, *args):
            pass

        def reject_credentials(self, value):
            if contains_known_credential(value, {"TOKEN": state["secret"]}):
                raise ValueError("Configured credential is not public")

        async def admit_credentials(self, value):
            if state["deny_credentials"]:
                raise ValueError("Credential admission unavailable")
            self.reject_credentials(value)

    store = Store()
    engine = Engine(store, jobs)
    engine.communication_writer = CommunicationWriter(store, model=reviewed_model(card()))

    async def push(*args, **kwargs):
        state["effects"].append("push")
        state["before_push"] = copy.deepcopy(run.get("publication_draft"))
        if mutate := state.get("after_push"):
            await mutate()

    original_client = httpx.AsyncClient

    def transport(request):
        state["requests"].append(json.loads(request.content))
        state["effects"].append("pr")
        if action := state.get("after_response"):
            action()
        if state.get("lost_response"):
            raise httpx.ReadTimeout("Public fixture lost response")
        return httpx.Response(
            201,
            json={
                "html_url": "https://github.com/org/public/pull/1",
                "web_url": "https://gitlab.com/org/public/-/merge_requests/1",
                "links": {"html": {"href": "https://bitbucket.org/org/public/pull/1"}},
            },
        )

    monkeypatch.setattr(module, "push", push)
    monkeypatch.setattr(
        module.httpx, "AsyncClient", lambda **kw: original_client(transport=httpx.MockTransport(transport), **kw)
    )
    for name in ["GITHUB", "GITLAB", "BITBUCKET"]:
        monkeypatch.setenv("FORGE_" + name + "_TOKEN", "public-fixture-scm-token")
    return SimpleNamespace(engine=engine, store=store, run=run, repo=repository, state=state, workspace=workspace)


@pytest.mark.parametrize(
    "provider,body_field,title_prefix",
    [
        ("github", "body", ""),
        ("gitlab", "description", "Draft: "),
        ("bitbucket", "description", ""),
    ],
)
@pytest.mark.parametrize("profile_provider", ["agentic", "bridge"])
async def test_selected_ai_owns_exact_pr_payload_retained_before_push(
    publication, provider, body_field, title_prefix, profile_provider
):
    p = publication
    if profile_provider == "bridge":
        from firefly_forge.model_profiles import ModelProfileInput

        p.store.task_router.profiles.save(
            "updates",
            ModelProfileInput(
                name="Subscription updates",
                provider="bridge",
                values={"endpoint": "http://127.0.0.1:12345/v1", "token": "public-fixture-bridge-token"},
            ),
        )
    p.repo.update(provider=provider, url=f"https://{provider}.com/org/public")
    calls = []
    p.engine.communication_writer.model = reviewed_model(card(), calls=calls)
    await p.engine.publish(p.run["id"])
    draft = p.state["before_push"]
    assert draft is not None, "Publication must retain its complete AI draft before push"
    assert draft["communication"]["mode"] == "ai"
    assert draft["title"] == "Add health checks"
    assert draft["body"].startswith("### Add health checks\n")
    assert "Implemented by firefly forge" not in draft["body"]
    assert draft["body_sha256"] == hashlib.sha256(draft["body"].encode()).hexdigest()
    assert p.state["requests"][0][body_field] == draft["body"]
    assert p.state["requests"][0]["title"] == title_prefix + draft["title"]
    assert p.state["effects"] == ["push", "pr"]
    assert [phase for phase, *_ in calls] == ["draft", "review"]
    prompts = str(calls)
    assert "Added bounded health checks." in prompts and "PRIVATE_CUSTOMER_BRIEF" not in prompts
    facts = draft["communication"]["facts"]["publication"]
    assert facts["source_tree"] == p.run["source_tree"]
    assert facts["checkpoint_revision"] == p.run["checkpoint_revision"]
    await p.engine.publish(p.run["id"])
    assert len(calls) == 2 and p.state["effects"] == ["push", "pr"]


async def test_ai_pr_corrects_once_then_publishes_without_second_veto(publication):
    p = publication
    calls = []
    p.engine.communication_writer.model = reviewed_model(
        card(text="The changes are already merged."),
        verdict="unsupported",
        issues=["unrecorded_action"],
        correction=card(text="The worker reports bounded health checks. One saved check passed; review is next."),
        calls=calls,
    )
    await p.engine.publish(p.run["id"])
    assert [phase for phase, *_ in calls] == ["draft", "review", "draft"]
    assert "already merged" not in p.state["requests"][0]["body"]
    evidence = p.run["publication_draft"]["communication"]
    assert evidence["repair_attempted"] and evidence["review_applies_to"] == "original_candidate"


@pytest.mark.parametrize(
    "failure", ["provider", "invalid", "credential_admission", "summary_secret", "summary_type", "summary_bound"]
)
async def test_selected_ai_refusal_retains_empty_text_and_never_pushes(publication, failure):
    p = publication
    calls = []
    if failure == "provider":

        def unavailable(*_args):
            calls.append("failed")
            raise RuntimeError("Public fixture provider unavailable")

        p.engine.communication_writer.model = FunctionModel(unavailable)
    elif failure == "invalid":
        p.engine.communication_writer.model = reviewed_model(card(title="Notify @everyone"), calls=calls)
    elif failure == "credential_admission":
        p.state["deny_credentials"] = True
    elif failure == "summary_secret":
        p.run["summary"] = p.state["secret"]
    elif failure == "summary_type":
        p.run["summary"] = ["not prose"]
    else:
        p.run["summary"] = "x" * 6001
    with pytest.raises(ValueError):
        await p.engine.publish(p.run["id"])
    draft = p.run.get("publication_draft")
    assert draft and draft["communication"]["mode"] == "held"
    assert draft["body"] == "" and draft["title"] == ""
    assert p.state["effects"] == []
    assert p.state["secret"] not in json.dumps(draft)


@pytest.mark.parametrize(
    "drift", ["summary", "checks", "source_tree", "checkpoint", "repository", "profile", "workspace"]
)
async def test_post_draft_currentness_prevents_any_outbound_effect(publication, drift):
    p = publication

    def change(_messages, info):
        from pydantic_ai.messages import ModelResponse, ToolCallPart

        if drift == "summary":
            p.run["summary"] = "Changed report"
        elif drift == "checks":
            p.run["final_verification"][0]["exit_code"] = 1
        elif drift == "source_tree":
            p.run["source_tree"] = "f" * 40
        elif drift == "checkpoint":
            p.run["checkpoint_revision"] = "f" * 40
        elif drift == "repository":
            p.repo["url"] = "https://github.com/other/destination"
        else:
            (p.workspace / "health.py").write_text("HEALTH = False\n")
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, card())])

    if drift == "profile":
        from firefly_forge.model_profiles import ModelProfileInput

        def change(_messages, info):
            from pydantic_ai.messages import ModelResponse, ToolCallPart

            p.store.task_router.profiles.save(
                "updates",
                ModelProfileInput(
                    name="Changed",
                    provider="agentic",
                    values={"model": "openai-responses:other", "api_key": "public-other"},
                ),
            )
            return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, card())])

    p.engine.communication_writer.model = reviewed_model(change)
    with pytest.raises(ValueError):
        await p.engine.publish(p.run["id"])
    assert p.state["effects"] == []
    assert p.run["publication_draft"]["body"] == ""
    assert p.run["publication_draft"]["communication"]["mode"] == "held"
    assert p.run["publication_draft"]["communication"]["reason"] != "generated"


async def test_explicit_recorded_mode_remains_distinct_and_uses_no_model(publication):
    from firefly_forge.task_routing import TaskRoutingInput

    p = publication
    await p.store.task_router.save(TaskRoutingInput(bindings={}))
    calls = []
    p.engine.communication_writer.model = reviewed_model(card(), calls=calls)
    await p.engine.publish(p.run["id"])
    assert not calls
    assert p.run["publication_draft"]["communication"]["mode"] == "deterministic"
    assert p.state["requests"][0]["body"].startswith("Implemented by firefly forge.")


async def test_unknown_remote_outcome_preserves_draft_and_never_regenerates(publication):
    p = publication
    calls = []
    p.engine.communication_writer.model = reviewed_model(card(), calls=calls)
    p.state["lost_response"] = True
    with pytest.raises(httpx.ReadTimeout):
        await p.engine.publish(p.run["id"])
    original = copy.deepcopy(p.run["publication_draft"])
    with pytest.raises(ValueError, match="ambiguous"):
        await p.engine.publish(p.run["id"])
    assert len(calls) == 2 and p.state["effects"] == ["push", "pr"]
    assert p.run["publication_draft"] == original


@pytest.mark.parametrize("boundary", ["before_draft", "during_draft", "after_push"])
async def test_current_releaser_is_required_before_model_and_each_remote_effect(publication, monkeypatch, boundary):
    from pydantic_ai.messages import ModelResponse, ToolCallPart

    from firefly_forge.project_access import AccessDenied, ProjectAccess

    p = publication
    denied = boundary == "before_draft"
    actor = {"user_id": "c" * 32, "authentication_method": "public-fixture"}
    calls = []

    async def authorize(_self, current, project_id, permission="release"):
        assert current == actor and permission == "release"
        if denied:
            raise AccessDenied("Public fixture release permission revoked")
        return True

    def model(_messages, info):
        nonlocal denied
        if boundary == "during_draft":
            denied = True
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, card())])

    async def after_push():
        nonlocal denied
        denied = True

    monkeypatch.setattr(ProjectAccess, "authorize_actor", authorize)
    p.engine.communication_writer.model = reviewed_model(model, calls=calls)
    if boundary == "after_push":
        p.state["after_push"] = after_push
    with pytest.raises(AccessDenied):
        await p.engine.publish(p.run["id"], actor=actor)
    assert p.state["effects"] == (["push"] if boundary == "after_push" else [])
    assert len(calls) == (0 if boundary == "before_draft" else 2)


@pytest.mark.parametrize("drift", ["checks", "profile", "repository", "source"])
async def test_post_push_drift_blocks_pr_and_automatic_regeneration(publication, drift):
    from firefly_forge.model_profiles import ModelProfileInput

    p = publication
    calls = []
    p.engine.communication_writer.model = reviewed_model(card(), calls=calls)

    async def after_push():
        if drift == "checks":
            p.run["final_verification"][0]["exit_code"] = 7
        elif drift == "repository":
            p.repo["url"] = "https://github.com/other/destination"
        elif drift == "source":
            (p.workspace / "health.py").write_text("HEALTH = False\n")
        else:
            p.store.task_router.profiles.save(
                "updates",
                ModelProfileInput(
                    name="Changed",
                    provider="agentic",
                    values={"model": "openai-responses:other", "api_key": "public-other"},
                ),
            )

    p.state["after_push"] = after_push
    with pytest.raises(ValueError):
        await p.engine.publish(p.run["id"])
    draft = copy.deepcopy(p.run["publication_draft"])
    assert p.state["effects"] == ["push"] and p.run["publication_status"] == "publishing"
    with pytest.raises(ValueError):
        await p.engine.publish(p.run["id"])
    assert len(calls) == 2 and p.run["publication_draft"] == draft


async def test_late_credential_admission_refuses_before_any_retained_public_text(publication):
    p = publication
    original = p.store.admit_credentials

    async def late_admission(value):
        if isinstance(value, dict) and "body" in value:
            p.state["secret"] = "Add health checks"
        await original(value)

    p.store.admit_credentials = late_admission
    with pytest.raises(ValueError):
        await p.engine.publish(p.run["id"])
    assert p.state["effects"] == []
    assert "Add health checks" not in json.dumps(p.run["publication_draft"])


async def test_existing_publication_intent_refuses_before_regenerating_a_draft(publication):
    from firefly_forge.engine import ReconciliationRequired

    p = publication
    calls = []
    p.engine.communication_writer.model = reviewed_model(card(), calls=calls)

    class RetainedIntent:
        async def plan(self, _identifier):
            return {"tasks": [{"id": "publish", "status": "running"}]}

        async def intent(self, *_args, **_kwargs):
            return {"disposition": "needs_reconciliation"}

    p.engine.journal = RetainedIntent()
    with pytest.raises(ReconciliationRequired):
        await p.engine.publish(p.run["id"])
    assert calls == [] and p.state["effects"] == []


async def test_drift_after_draft_storage_clears_public_text_before_push(publication):
    p = publication
    original = p.store.update

    async def update(identifier, **fields):
        await original(identifier, **fields)
        if (fields.get("publication_draft") or {}).get("communication", {}).get("mode") == "ai":
            p.run["summary"] = "Changed while saving the draft"

    p.store.update = update
    with pytest.raises(ValueError):
        await p.engine.publish(p.run["id"])
    assert p.state["effects"] == []
    assert p.run["publication_draft"]["body"] == ""
    assert p.run["publication_draft"]["communication"]["mode"] == "held"


async def test_exact_title_and_body_are_durable_before_publication_intent(publication):
    p = publication
    observed = []

    class Journal:
        async def plan(self, _identifier):
            return {"tasks": []}

        async def intent(self, identifier, task_id, *, payload):
            assert identifier == p.run["id"] and task_id == "publish"
            draft = copy.deepcopy(p.run["publication_draft"])
            assert draft["body_sha256"] == payload["publication_body_sha256"]
            assert draft["title_sha256"] == payload["publication_title_sha256"]
            assert p.state["effects"] == []
            observed.append(draft)
            return {"disposition": "execute", "idempotency_key": "public-intent"}

        async def receipt(self, *_args, **_kwargs):
            pass

    p.engine.journal = Journal()
    await p.engine.publish(p.run["id"])
    assert observed[0] == p.state["before_push"] == p.run["publication_draft"]


async def test_recorded_title_drift_during_draft_storage_blocks_push(publication):
    from firefly_forge.task_routing import TaskRoutingInput

    p = publication
    await p.store.task_router.save(TaskRoutingInput(bindings={}))
    original = p.store.update

    async def update(identifier, **fields):
        await original(identifier, **fields)
        if (fields.get("publication_draft") or {}).get("body"):
            p.run["brief"] = "Changed publication title"

    p.store.update = update
    with pytest.raises(ValueError):
        await p.engine.publish(p.run["id"])
    assert p.state["effects"] == [] and p.run["publication_draft"]["body"] == ""


@pytest.mark.parametrize("boundary", ["during_model", "before_push"])
@pytest.mark.parametrize("successor", [None, "published", "publishing"])
async def test_lost_owner_never_writes_a_hold_over_current_publication(publication, boundary, successor):
    p = publication
    state = {"lost": False, "unauthorized_writes": [], "retained": None}
    original_update = p.store.update

    def lose_owner():
        state["lost"] = True
        if successor:
            p.run.update(
                publication_draft={"title": "Successor draft", "body": "Successor retained body"},
                publication_status=successor,
                pr_url="https://github.com/org/public/pull/2" if successor == "published" else "",
            )
        state["retained"] = copy.deepcopy(p.run)

    async def assert_ownership():
        if boundary == "before_push" and p.run.get("publication_status") == "publishing" and not state["lost"]:
            lose_owner()
        if state["lost"]:
            raise RuntimeError("PostgreSQL execution ownership was lost")

    async def update(identifier, **fields):
        if state["lost"]:
            state["unauthorized_writes"].append(copy.deepcopy(fields))
        await original_update(identifier, **fields)

    def draft(_messages, info):
        if boundary == "during_model":
            lose_owner()
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, card())])

    p.engine.assert_ownership = assert_ownership
    p.store.update = update
    p.engine.communication_writer.model = reviewed_model(draft)
    with pytest.raises((RuntimeError, ValueError)) as failure:
        await p.engine.publish(p.run["id"])
    assert state["retained"] is not None, str(failure.value)
    assert state["unauthorized_writes"] == []
    assert p.run == state["retained"]
    assert "ownership" in str(failure.value)
    assert p.state["effects"] == []


@pytest.mark.parametrize("successor", ["published", "publishing"])
async def test_later_publication_custody_is_not_replaced_by_an_older_draft(publication, successor):
    p = publication
    retained = {}

    def draft(_messages, info):
        p.run.update(
            publication_draft={"title": "Later draft", "body": "Later body"},
            publication_status=successor,
            pr_url="https://github.com/org/public/pull/2" if successor == "published" else "",
        )
        retained.update(copy.deepcopy(p.run))
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, card())])

    p.engine.communication_writer.model = reviewed_model(draft)
    with pytest.raises(ValueError):
        await p.engine.publish(p.run["id"])
    assert p.run == retained
    assert p.state["effects"] == []


@pytest.mark.parametrize("field", ["publication_draft", "publication_status", "publication_actor", "pr_url"])
async def test_each_publication_custody_field_refuses_a_stale_draft(publication, field):
    p = publication
    retained = {}
    values = {
        "publication_draft": {"title": "Later draft", "body": "Later body"},
        "publication_status": "publishing",
        "publication_actor": {"user_id": "c" * 32},
        "pr_url": "https://github.com/org/public/pull/2",
    }

    def draft(_messages, info):
        p.run[field] = values[field]
        retained.update(copy.deepcopy(p.run))
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, card())])

    p.engine.communication_writer.model = reviewed_model(draft)
    with pytest.raises(ValueError, match="custody"):
        await p.engine.publish(p.run["id"])
    assert p.run == retained and p.state["effects"] == []


@pytest.mark.parametrize("successor", ["published", "publishing"])
async def test_final_remote_response_never_overwrites_later_custody(publication, successor):
    p = publication
    retained = {}

    def successor_writes():
        p.run.update(
            publication_draft={"title": "Later draft", "body": "Later body"},
            publication_status=successor,
            publication_actor={"user_id": "c" * 32},
            pr_url="https://github.com/org/public/pull/2" if successor == "published" else "",
        )
        retained.update(copy.deepcopy(p.run))

    p.state["after_response"] = successor_writes
    with pytest.raises(ValueError, match="custody"):
        await p.engine.publish(p.run["id"])
    assert p.run == retained and p.state["effects"] == ["push", "pr"]


@pytest.mark.parametrize("change", ["closed", "invalidated", "released", "replaced", "stopping"])
async def test_native_store_publication_fence_refuses_without_reconnecting(tmp_path, change):
    from firefly_forge.store import Store

    store = Store(tmp_path / "factory.db")
    original = SimpleNamespace(closed=False, invalidated=False)
    store._ownership_connection = original
    token = store.execution_ownership_token
    if change in {"closed", "invalidated"}:
        setattr(original, change, True)
    elif change == "released":
        store._ownership_connection = None
    elif change == "replaced":
        store._ownership_connection = SimpleNamespace(closed=False, invalidated=False)
    else:
        store.execution_stopping = True
    with pytest.raises(RuntimeError, match="ownership"):
        await store.update_publication("a" * 32, {}, ownership=token, publication_draft={"body": "stale"})
