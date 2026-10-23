# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Real polling parsing, immutable grants and dispatch with controlled model persistence."""

import copy
import importlib
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from test_scm_polling import poll_case as poll_case

from firefly_forge.journal import digest
from firefly_forge.product_provenance import content_digest
from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied, ProjectAccess


async def setup(c, monkeypatch):
    module = importlib.import_module("firefly_forge.scm_poll_functional")
    monkeypatch.setattr(module, "current_unit_of_work", lambda: object() if c.state["depth"] else None)
    monkeypatch.setattr(module, "ForgeIdentityUserRepository", lambda: c.service.users)
    project_records = importlib.import_module("firefly_forge.scm_polling").ForgeProjectRepository
    monkeypatch.setattr(module, "ForgeProjectRepository", project_records)
    c.definition["definition"]["product_repository_id"] = c.repository
    settings = c.db["settings"][c.repository].data
    settings.update(channel_commands=["respond", "assistant"], configuration_actors=["7"], assistant_agent="bridge")
    profile = dict(provider="bridge", revision=2, settings={"FORGE_BRIDGE_MODEL": "public-functional"})
    c.state["profile"] = profile
    c.store.task_router = SimpleNamespace(
        current=lambda: {"bindings": {"product": "functional"}, "digest": "f" * 64},
        profiles=SimpleNamespace(_read=lambda name: copy.deepcopy(profile)),
    )
    c.state["sessions"] = {}

    async def find(value):
        return c.state["sessions"].get(value.hex)

    async def authorize():
        actor = CURRENT_PRINCIPAL.get()
        assert actor.authentication_method == "scm_mapping"
        await ProjectAccess(c.store).authorize_actor(ProjectAccess.actor_snapshot(actor), c.project, "edit")

    async def create(repository, request):
        await authorize()
        assert request.model_profile_id == "functional"
        assert request.agent == profile["provider"]
        conversation = dict(
            id=uuid4().hex,
            repository_id=repository,
            project_id=c.project,
            project_snapshot=copy.deepcopy(c.definition),
            model_selection=dict(
                profile_id="functional",
                provider=profile["provider"],
                profile_revision=2,
                profile_digest=content_digest({"provider": profile["provider"], "settings": profile["settings"]}),
            ),
        )
        c.state["sessions"][conversation["id"]] = SimpleNamespace(repository_id=UUID(repository), data=conversation)
        return copy.deepcopy(conversation)

    async def session(repository, conversation):
        return dict(id=conversation, project_id=c.project, status="ready", turns=[])

    async def turn(repository, conversation, request):
        await authorize()
        c.state["model_calls"] = c.state.get("model_calls", 0) + 1
        if c.state.get("return_unknown"):
            return dict(id=uuid4().hex, status="needs_reconciliation", detail="Outcome unknown")
        if c.state.get("unknown_model"):
            raise RuntimeError("Controlled ambiguous provider outcome")
        hook = c.state.get("model_hook")
        if hook:
            hook()
        await authorize()
        return dict(
            id=uuid4().hex,
            status="completed",
            reply=dict(intent="dialogue", message="Fictional functional reply", questions=[]),
        )

    c.collaboration.product_assistant = SimpleNamespace(
        create=create, session=session, turn=turn, records=SimpleNamespace(find_by_id=find)
    )
    service = module.SCMPollFunctionalAuthority(c.store, c.service)
    c.collaboration.poll_functional_authority = service
    c.store.scm_poll_functional = service
    c.state["functional"] = service
    await c.ready()
    binding = await c.consent()
    body = dict(
        version=1,
        transport="github_rest_poll_v1",
        request_id=uuid4().hex,
        expected_previous_digest=None,
        expected_project_binding_digest=digest(c.definition),
        expected_binding_request_id=binding["request"]["request_id"],
        expected_binding_digest=binding["digest"],
        component_ids=["api"],
        issue_number=3,
        allowed_actions=["assistant_start", "assistant_status", "assistant_turn"],
        max_model_turns=3,
        expires_at=(datetime.now(UTC) + timedelta(minutes=30)).isoformat(),
        model_profile_id="functional",
        expected_model_profile_revision=2,
        expected_model_profile_digest=content_digest(
            {"provider": profile["provider"], "settings": profile["settings"]}
        ),
        expected_routing_digest="f" * 64,
        expected_functional_policy_digest=module.functional_policy_digest(),
        active=True,
        acknowledge_separate_assistant_allowance=True,
    )
    return module, body


async def command(c, operation, **options):
    number = 100 + len(c.state["comments"])
    c.state["clock"] += 3600
    c.state["comments"].append(
        c.comment(number, "/forge assistant " + json.dumps(dict(operation=operation, project_id=c.project, **options)))
    )
    for _ in range(3):
        await c.service.step(c.repository)
    return next(copy.deepcopy(row.data) for row in c.db["inbox"].values() if row.source_key == f"comment:{number}")


async def test_old_polling_consent_never_admits_functional_start(poll_case, monkeypatch):  # noqa: F811
    async with poll_case() as c:
        await setup(c, monkeypatch)
        delivery = await command(c, "start")
        assert delivery["status"] == "received"
        result = await c.collaboration.process(delivery["id"])
        assert result["status"] == "denied"
        assert c.state["sessions"] == {} and c.state.get("model_calls", 0) == 0


async def test_new_consent_runs_same_issue_assistant_and_caps_three_durable_turns(poll_case, monkeypatch):  # noqa: F811
    async with poll_case() as c:
        module, body = await setup(c, monkeypatch)
        receipt = await c.state["functional"].configure(
            c.project, c.repository, module.SCMPollFunctionalGrantInputV1(**body)
        )
        original_run = copy.deepcopy(c.db["runs"][c.run_id].data)
        delivery = await command(c, "start")
        await c.state["functional"].process(delivery["id"])
        result = await c.collaboration.receipt(delivery["id"])
        assert result["status"] == "processed", result
        conversation = result["action_result"]["conversation_id"]
        assert c.state.get("model_calls", 0) == 0
        for index in range(3):
            delivery = await command(
                c, "turn", conversation_id=conversation, request_id=uuid4().hex, message=f"Public turn {index}"
            )
            await c.state["functional"].process(delivery["id"])
            before = c.state["model_calls"]
            await c.state["functional"].process(delivery["id"])
            assert c.state["model_calls"] == before
        assert c.state["model_calls"] == 3
        delivery = await command(c, "turn", conversation_id=conversation, request_id=uuid4().hex, message="Fourth")
        with pytest.raises(ValueError, match="exhausted"):
            await c.state["functional"].process(delivery["id"])
        assert c.state["model_calls"] == 3
        assert c.db["runs"][c.run_id].data == original_run
        assert await c.state["functional"].get(c.project, c.repository, body["request_id"]) == receipt


async def test_unknown_model_result_is_held_and_its_slot_never_retried(poll_case, monkeypatch):  # noqa: F811
    async with poll_case() as c:
        module, body = await setup(c, monkeypatch)
        await c.state["functional"].configure(c.project, c.repository, module.SCMPollFunctionalGrantInputV1(**body))
        first = await command(c, "start")
        result = await c.state["functional"].process(first["id"])
        conversation = result["action_result"]["conversation_id"]
        delivery = await command(
            c, "turn", conversation_id=conversation, request_id=uuid4().hex, message="Public uncertain turn"
        )
        c.state["unknown_model"] = True
        with pytest.raises(RuntimeError, match="ambiguous"):
            await c.state["functional"].process(delivery["id"])
        repeated = await c.state["functional"].process(delivery["id"])
        assert repeated["action_status"] == "needs_reconciliation"
        assert c.state["model_calls"] == 1


async def test_explicit_profile_provider_does_not_fall_back_to_legacy_assistant_agent(poll_case, monkeypatch):  # noqa: F811
    async with poll_case() as c:
        module, body = await setup(c, monkeypatch)
        c.state["profile"]["provider"] = "agentic"
        body["expected_model_profile_digest"] = content_digest(
            {"provider": "agentic", "settings": c.state["profile"]["settings"]}
        )
        await c.state["functional"].configure(c.project, c.repository, module.SCMPollFunctionalGrantInputV1(**body))
        first = await command(c, "start")
        result = await c.state["functional"].process(first["id"])
        assert result["status"] == "processed"


async def test_provider_returning_unknown_state_holds_delivery_without_feedback(poll_case, monkeypatch):  # noqa: F811
    async with poll_case() as c:
        module, body = await setup(c, monkeypatch)
        await c.state["functional"].configure(c.project, c.repository, module.SCMPollFunctionalGrantInputV1(**body))
        first = await command(c, "start")
        result = await c.state["functional"].process(first["id"])
        c.state["return_unknown"] = True
        delivery = await command(
            c,
            "turn",
            conversation_id=result["action_result"]["conversation_id"],
            request_id=uuid4().hex,
            message="Public uncertain turn",
        )
        result = await c.state["functional"].process(delivery["id"])
        assert result["status"] == "needs_reconciliation"
        assert result["action_status"] == "needs_reconciliation"
        assert result.get("feedback_status") != "pending"
        await c.state["functional"].process(delivery["id"])
        assert c.state["model_calls"] == 1


@pytest.mark.parametrize(
    "change", ["edited", "binding_revoked", "membership", "profile", "collaboration", "issue", "conversation"]
)
async def test_dispatch_fences_current_source_identity_scope_and_profile(poll_case, monkeypatch, change):  # noqa: F811
    async with poll_case() as c:
        module, body = await setup(c, monkeypatch)
        await c.state["functional"].configure(c.project, c.repository, module.SCMPollFunctionalGrantInputV1(**body))
        first = await command(c, "start")
        result = await c.state["functional"].process(first["id"])
        delivery = await command(
            c,
            "turn",
            conversation_id=result["action_result"]["conversation_id"],
            request_id=uuid4().hex,
            message="Public functional question",
        )
        if change == "edited":
            c.state["edited"] = True
        elif change == "binding_revoked":
            for row in c.db["events"].values():
                if row.data["kind"] == "scm_poll_actor_binding_v2":
                    row.data["receipt"]["request"]["active"] = False
        elif change == "membership":
            c.db["memberships"]["member"].active = False
        elif change == "profile":
            c.state["profile"]["revision"] += 1
        elif change == "collaboration":
            c.db["settings"][c.repository].data["channel_commands"].remove("assistant")
        elif change == "issue":
            c.db["inbox"][delivery["id"]].data["verification"]["issue_number"] = 4
            delivery = await c.collaboration.receipt(delivery["id"])
        else:
            c.state["sessions"][result["action_result"]["conversation_id"]].data["project_snapshot"] = {}
        with pytest.raises((AccessDenied, ValueError)):
            await c.state["functional"].process(delivery["id"])
        assert c.state.get("model_calls", 0) == 0


async def test_missing_durable_allowance_index_never_replenishes_model_turns(poll_case, monkeypatch):  # noqa: F811
    async with poll_case() as c:
        module, body = await setup(c, monkeypatch)
        grant = await c.state["functional"].configure(
            c.project, c.repository, module.SCMPollFunctionalGrantInputV1(**body)
        )
        first = await command(c, "start")
        result = await c.state["functional"].process(first["id"])
        c.db["settings"][c.repository].data[module.USAGE_KEY]["turns"].pop(grant["digest"], None)
        delivery = await command(
            c,
            "turn",
            conversation_id=result["action_result"]["conversation_id"],
            request_id=uuid4().hex,
            message="Must not replenish",
        )
        with pytest.raises((AccessDenied, ValueError), match="ledger"):
            await c.state["functional"].process(delivery["id"])
        assert c.state.get("model_calls", 0) == 0


async def test_revocation_after_model_wait_withholds_result_and_feedback(poll_case, monkeypatch):  # noqa: F811
    async with poll_case() as c:
        module, body = await setup(c, monkeypatch)
        await c.state["functional"].configure(c.project, c.repository, module.SCMPollFunctionalGrantInputV1(**body))
        first = await command(c, "start")
        result = await c.state["functional"].process(first["id"])
        delivery = await command(
            c,
            "turn",
            conversation_id=result["action_result"]["conversation_id"],
            request_id=uuid4().hex,
            message="Public waiting turn",
        )
        c.state["model_hook"] = lambda: c.state["profile"].update(revision=3)
        with pytest.raises(AccessDenied, match="profile"):
            await c.state["functional"].process(delivery["id"])
        held = await c.collaboration.receipt(delivery["id"])
        assert c.state["model_calls"] == 1
        assert held["action_status"] == "needs_reconciliation"
        assert held.get("action_result") is None and held.get("feedback_status") != "pending"


async def test_natural_dialogue_does_not_fall_back_to_legacy_allowance_and_typed_turn_stays_bound(
    poll_case, monkeypatch
):  # noqa: F811
    async with poll_case() as c:
        module, body = await setup(c, monkeypatch)
        await c.state["functional"].configure(c.project, c.repository, module.SCMPollFunctionalGrantInputV1(**body))
        first = await command(c, "start")
        result = await c.state["functional"].process(first["id"])
        conversation = result["action_result"]["conversation_id"]
        number = 100 + len(c.state["comments"])
        c.state["clock"] += 3600
        c.state["comments"].append(c.comment(number, "@forge, hablemos de las oportunidades del CRM"))
        for _ in range(3):
            await c.service.step(c.repository)
        delivery = next(
            copy.deepcopy(row.data) for row in c.db["inbox"].values() if row.source_key == f"comment:{number}"
        )
        assert delivery["command"]["adapter"] == "addressed_status_v1"
        legacy_router = c.store.task_router

        async def no_communication(task):
            assert task == "communication"
            return None

        c.store.task_router = SimpleNamespace(resolve=no_communication)
        natural = await c.collaboration.process(delivery["id"])
        assert natural["natural_turn"]["state"] == "waiting_configuration"
        assert set(c.state["sessions"]) == {conversation} and c.state.get("model_calls", 0) == 0
        c.store.task_router = legacy_router
        explicit = await command(
            c,
            "turn",
            conversation_id=conversation,
            request_id=uuid4().hex,
            message="A retained explicit compatibility turn",
        )
        result = await c.state["functional"].process(explicit["id"])
        assert result["action_result"]["conversation_id"] == conversation
        assert result["action_result"]["reply"] == "Fictional functional reply"
        assert c.state["model_calls"] == 1


async def test_retained_command_must_still_equal_its_api_observed_source(poll_case, monkeypatch):  # noqa: F811
    async with poll_case() as c:
        module, body = await setup(c, monkeypatch)
        await c.state["functional"].configure(c.project, c.repository, module.SCMPollFunctionalGrantInputV1(**body))
        first = await command(c, "start")
        result = await c.state["functional"].process(first["id"])
        delivery = await command(
            c,
            "turn",
            conversation_id=result["action_result"]["conversation_id"],
            request_id=uuid4().hex,
            message="Original source",
        )
        c.db["inbox"][delivery["id"]].data["command"]["message"] = "Different retained command"
        with pytest.raises(AccessDenied, match="source"):
            await c.state["functional"].process(delivery["id"])
        assert c.state.get("model_calls", 0) == 0


async def test_suspended_functional_grant_administrator_cannot_leave_live_model_authority(poll_case, monkeypatch):  # noqa: F811
    from firefly_forge.identity import Principal

    async with poll_case() as c:
        module, body = await setup(c, monkeypatch)
        other = Principal(uuid4(), "local:bootstrap", "other-operator", authentication_method="local_bootstrap")
        c.db["users"][other.id.hex] = SimpleNamespace(
            id=other.id, issuer=other.issuer, subject=other.subject, enabled=True
        )
        token = CURRENT_PRINCIPAL.set(other)
        try:
            await c.state["functional"].configure(c.project, c.repository, module.SCMPollFunctionalGrantInputV1(**body))
        finally:
            CURRENT_PRINCIPAL.reset(token)
        c.db["users"][other.id.hex].enabled = False
        first = await command(c, "start")
        with pytest.raises(AccessDenied, match="administrator"):
            await c.state["functional"].process(first["id"])
        assert c.state["sessions"] == {}


async def test_runtime_policy_change_revokes_dispatch_and_pending_feedback(poll_case, monkeypatch):  # noqa: F811
    async with poll_case() as c:
        module, body = await setup(c, monkeypatch)
        await c.state["functional"].configure(c.project, c.repository, module.SCMPollFunctionalGrantInputV1(**body))
        first = await command(c, "start")
        result = await c.state["functional"].process(first["id"])
        monkeypatch.setattr(module, "functional_policy_digest", lambda: "0" * 64, raising=False)
        delivery = await command(
            c,
            "turn",
            conversation_id=result["action_result"]["conversation_id"],
            request_id=uuid4().hex,
            message="Must not run changed policy",
        )
        with pytest.raises(AccessDenied, match="policy"):
            await c.state["functional"].process(delivery["id"])
        with pytest.raises(AccessDenied, match="policy"):
            await c.state["functional"].authorize_feedback(first["id"], remote=False)
        assert c.state.get("model_calls", 0) == 0


async def test_new_grant_never_retroactively_admits_preconsent_commands(poll_case, monkeypatch):  # noqa: F811
    async with poll_case() as c:
        module, body = await setup(c, monkeypatch)
        old = await command(c, "start")
        await c.state["functional"].configure(c.project, c.repository, module.SCMPollFunctionalGrantInputV1(**body))
        with pytest.raises(AccessDenied, match="predates"):
            await c.state["functional"].process(old["id"])
        assert c.state["sessions"] == {}


async def test_review_and_admission_require_mapped_membership_for_each_action(poll_case, monkeypatch):  # noqa: F811
    async with poll_case() as c:
        module, body = await setup(c, monkeypatch)
        service = c.state["functional"]
        c.db["memberships"]["member"].role = "viewer"
        review = await service.review(c.project, c.repository, body["expected_binding_request_id"])
        readiness = review["native_user_readiness"]
        assert readiness["native_user_id"] == c.user_id
        assert readiness["enabled"] is True
        assert readiness["actions"] == {
            "assistant_start": {"permission": "edit", "ready": False},
            "assistant_status": {"permission": "read", "ready": True},
            "assistant_turn": {"permission": "edit", "ready": False},
        }
        before = (
            copy.deepcopy(c.db["settings"][c.repository].data),
            copy.deepcopy({name: row.data for name, row in c.db["events"].items()}),
        )
        with pytest.raises(AccessDenied, match="project membership"):
            await service.configure(c.project, c.repository, module.SCMPollFunctionalGrantInputV1(**body))
        assert (c.db["settings"][c.repository].data, {name: row.data for name, row in c.db["events"].items()}) == before
        c.db["memberships"]["member"].role = "developer"
        granted = await service.configure(c.project, c.repository, module.SCMPollFunctionalGrantInputV1(**body))
        assert granted["request"] == body
        review = await service.review(c.project, c.repository, body["expected_binding_request_id"])
        assert all(item["ready"] for item in review["native_user_readiness"]["actions"].values())


@pytest.mark.parametrize("revoked", ["disabled", "inactive"])
async def test_revoked_mapped_user_is_not_ready_and_cannot_admit_grant(poll_case, monkeypatch, revoked):  # noqa: F811
    async with poll_case() as c:
        module, body = await setup(c, monkeypatch)
        service = c.state["functional"]
        if revoked == "disabled":
            c.db["users"][c.user_id].enabled = False
        else:
            c.db["memberships"]["member"].active = False
        review = await service.review(c.project, c.repository, body["expected_binding_request_id"])
        assert not any(item["ready"] for item in review["native_user_readiness"]["actions"].values())
        before = (
            copy.deepcopy(c.db["settings"][c.repository].data),
            copy.deepcopy({name: row.data for name, row in c.db["events"].items()}),
        )
        with pytest.raises(AccessDenied, match="project membership"):
            await service.configure(c.project, c.repository, module.SCMPollFunctionalGrantInputV1(**body))
        assert (c.db["settings"][c.repository].data, {name: row.data for name, row in c.db["events"].items()}) == before


async def test_viewer_can_consent_to_status_only_without_model_allowance(poll_case, monkeypatch):  # noqa: F811
    async with poll_case() as c:
        module, body = await setup(c, monkeypatch)
        c.db["memberships"]["member"].role = "viewer"
        body.update(allowed_actions=["assistant_status"], max_model_turns=0)
        receipt = await c.state["functional"].configure(
            c.project, c.repository, module.SCMPollFunctionalGrantInputV1(**body)
        )
        assert receipt["request"]["allowed_actions"] == ["assistant_status"]
        assert receipt["request"]["max_model_turns"] == 0
        assert c.state.get("model_calls", 0) == 0


async def test_membership_drift_during_preflight_refuses_before_grant_write(poll_case, monkeypatch):  # noqa: F811
    async with poll_case() as c:
        module, body = await setup(c, monkeypatch)
        service = c.state["functional"]
        original = service._preflight

        async def preflight(context):
            await original(context)
            c.db["memberships"]["member"].role = "viewer"

        service._preflight = preflight
        before = (
            copy.deepcopy(c.db["settings"][c.repository].data),
            copy.deepcopy({name: row.data for name, row in c.db["events"].items()}),
        )
        with pytest.raises(AccessDenied, match="project membership"):
            await service.configure(c.project, c.repository, module.SCMPollFunctionalGrantInputV1(**body))
        assert (c.db["settings"][c.repository].data, {name: row.data for name, row in c.db["events"].items()}) == before


async def test_viewer_can_revoke_existing_edit_allowance(poll_case, monkeypatch):  # noqa: F811
    async with poll_case() as c:
        module, body = await setup(c, monkeypatch)
        service = c.state["functional"]
        original = await service.configure(c.project, c.repository, module.SCMPollFunctionalGrantInputV1(**body))
        c.db["memberships"]["member"].role = "viewer"
        body.update(request_id=uuid4().hex, expected_previous_digest=original["digest"], active=False)
        revoked = await service.configure(c.project, c.repository, module.SCMPollFunctionalGrantInputV1(**body))
        assert revoked["request"]["active"] is False
