# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Consumed local postprocessing is distinct from an answered worker clarification."""

import copy
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from firefly_forge.journal import digest
from firefly_forge.project_access import ProjectAccess
from firefly_forge.scm_natural_models import NaturalDecision, resolve_task

pytest_plugins = ["test_consumed_terminal_reconciliation_host"]
NATIVE_AUTHORIZE_ACTOR = ProjectAccess.authorize_actor
NATIVE_AUTHORIZE_EXECUTION = ProjectAccess.authorize_execution


@pytest.fixture(autouse=True)
def component_exists_before_claim_history_is_frozen(monkeypatch):
    import test_original_selection_readmission_host as fixture

    original = fixture.historical_case

    def component_case():
        value = original()
        value["run"]["component_id"] = "inventory-api"
        return value

    monkeypatch.setattr(fixture, "historical_case", component_case)


def consumed_choice():
    scope = {
        "repository_id": "1" * 32,
        "project_id": "2" * 32,
        "issue": {"kind": "issue", "number": 7},
        "actor": "101",
        "binding_digest": "3" * 64,
        "native_user_id": "4" * 32,
    }
    candidate = {
        **scope,
        "run_id": "5" * 32,
        "run_status": "paused",
        "origin": "consumed_terminal_claim",
        "question_id": "consumed-" + "6" * 32,
        "question_digest": "7" * 64,
        "question": "Continue the recorded local postprocessing for this same claimed result",
        "question_status": "pending",
        "consumed": False,
        "consumed_continuation": {
            "request_id": "6" * 32,
            "record_digest": "8" * 64,
            "claim_digest": "9" * 64,
        },
    }
    return scope, candidate


def test_natural_continue_selects_consumed_claim_without_fabricating_worker_answer():
    scope, candidate = consumed_choice()
    resolution = resolve_task(
        scope,
        [candidate],
        NaturalDecision(intent="resume", message="Continue the recorded local postprocessing."),
        answer="Continue",
    )
    assert resolution.state == "consumed_continue"
    assert resolution.run_id == candidate["run_id"]
    assert resolution.question_id == candidate["question_id"]
    assert resolution.answer is None and resolution.response_digest is None


def test_consumed_choice_preserves_status_and_refuses_answer_or_malformed_claim():
    scope, candidate = consumed_choice()
    status = resolve_task(
        scope, [candidate], NaturalDecision(intent="status", message="Current status."), answer="status"
    )
    assert status.state == "status" and status.run_id == candidate["run_id"]
    for intent, request in (("answer", "6" * 32), ("resume", "bad-request")):
        changed = copy.deepcopy(candidate)
        changed["consumed_continuation"]["request_id"] = request
        changed["question_id"] = "consumed-" + request
        result = resolve_task(scope, [changed], NaturalDecision(intent=intent, message="Continue."), answer="continue")
        assert result.state == "waiting"


@pytest.mark.parametrize("run_origin", ["api", "scm"])
def test_sealed_claim_delegation_preserves_the_separate_execution_actor(run_origin):
    """Pure origin contract; real execution authorization is exercised by the consumer fixture separately."""
    from contextvars import ContextVar

    from firefly_forge.identity import Principal
    from firefly_forge.project_access import AccessDenied
    from firefly_forge.scm_consumed_continuation_models import SCMConsumedContinuationAuthorityV1
    from firefly_forge.scm_consumed_continuations import PreparedConsumedContinuation

    scope = dict(
        version=1,
        transport="github_rest_poll_resume_v3",
        action="resume",
        verified_receipt_id="a" * 32,
        verified_reference_digest="b" * 64,
        run_id="5" * 32,
        project_id="2" * 32,
        repository_id="1" * 32,
        component_id="api",
        native_user_id="4" * 32,
        binding_digest="c" * 64,
        grant_digest="d" * 64,
    )
    value = dict(
        version=1,
        scope=scope,
        request_id="6" * 32,
        recovery_digest="8" * 64,
        claim_digest="9" * 64,
        original_actor_digest="e" * 64,
        intake_digest="f" * 64,
        policy_digest="0" * 64,
        local_postprocessing_only=True,
    )
    seal = SCMConsumedContinuationAuthorityV1.model_validate({**value, "digest": digest(value)})
    actor = Principal(
        UUID("4" * 32), "controlled-issuer", "mapped-person", authentication_method="scm_mapping", scm_delegation=scope
    )
    store = SimpleNamespace(
        execution_ownership_token=object(),
        configuration=SimpleNamespace(write_generation=object()),
        credential_registry=dict,
    )
    prepared = PreparedConsumedContinuation(
        SimpleNamespace(store=store),
        SimpleNamespace(_consumed=ContextVar("controlled-delegation", default=None)),
        actor,
        seal,
    )
    witness = dict(
        run_id=scope["run_id"],
        digest=value["recovery_digest"],
        payload=dict(
            request_id=value["request_id"],
            candidate={"actor_digest": value["original_actor_digest"]},
            actor={"user_id": actor.id.hex, "authentication_method": "oidc_session"},
            run={
                **{key: scope[key] for key in ("project_id", "repository_id", "component_id")},
                "execution_actor": {
                    "user_id": "7" * 32,
                    "authentication_method": "scm_mapping" if run_origin == "scm" else "oidc_session",
                },
            },
        ),
    )
    before = copy.deepcopy(witness)
    with prepared.activate():
        prepared.validate_witness(witness, {"digest": value["claim_digest"]})
        assert witness == before and not actor.api_roles and actor.api_audience is None
        with pytest.raises(AccessDenied, match="not checked"):
            prepared.fence()
        witness["payload"]["actor"]["user_id"] = "3" * 32
        with pytest.raises(AccessDenied, match="original claim or native operator"):
            prepared.validate_witness(witness)
        object.__setattr__(prepared.seal.scope, "component_id", "other")
        with pytest.raises(AccessDenied, match="seal"):
            prepared._view()


@pytest.fixture
async def continuation_case(consumed_terminal, monkeypatch):
    """Real resume/continuation/core; persistence and verified provider observations are controlled."""
    from test_bridge_inflight_cancel import Rows
    from test_consumed_terminal_reconciliation_host import stopped

    from firefly_forge import scm_conversations, scm_polling
    from firefly_forge.access_persistence import ForgeMembershipRepository
    from firefly_forge.identity_persistence import ForgeIdentityUserRepository
    from firefly_forge.scm_consumed_continuations import SCMConsumedContinuations, reference
    from firefly_forge.scm_natural_effects import authorize_projection
    from firefly_forge.scm_natural_tasks import SCMNaturalTasks, natural_policy_digest
    from firefly_forge.scm_poll_resume import SCMPollResumeAuthority

    env = consumed_terminal
    completions, request = await stopped(env)
    # The historical epoch fixture mocks authorization during setup; the new consumer uses the real checks.
    monkeypatch.setattr(ProjectAccess, "authorize_actor", NATIVE_AUTHORIZE_ACTOR)
    monkeypatch.setattr(ProjectAccess, "authorize_execution", NATIVE_AUTHORIZE_EXECUTION)
    run = await env.store.get_run(env.run_id)
    env.store.run.repository_id = UUID(run["repository_id"])
    env.store.repository_records = Rows(
        [SimpleNamespace(id=UUID(run["repository_id"]), data={"id": run["repository_id"]})]
    )
    rows = {}
    member = SimpleNamespace(project_id=UUID(run["project_id"]), user_id=env.user.id, active=True, role="developer")
    settings = SimpleNamespace(data={"enabled": True, "trusted_actors": ["7"], "channel_commands": ["respond"]})
    event = SimpleNamespace(data={"retained": "immutable resume grant"})
    binding = {
        "digest": "2" * 64,
        "binding_key": "1" * 64,
        "request": {"native_user_id": env.user.id.hex, "allowed_actions": ["respond", "status"]},
    }
    grant = dict(
        digest="3" * 64,
        native_user_id=env.user.id.hex,
        event_id=uuid4().hex,
        request=dict(
            version=3,
            transport="github_rest_poll_v1",
            request_id=uuid4().hex,
            expected_previous_digest=None,
            expected_project_binding_digest="4" * 64,
            expected_binding_request_id=uuid4().hex,
            expected_binding_digest=binding["digest"],
            component_ids=[run["component_id"]],
            allowed_actions=["resume"],
            active=True,
            acknowledge_same_run_resume=True,
        ),
    )
    ref = dict(
        version=1,
        transport="github_rest_poll_v1",
        provider="github",
        api_origin="https://api.github.com",
        web_origin="https://github.com",
        installation_id="34",
        remote_repository_id="42",
        external_actor_id="7",
        connection_revision=uuid4().hex,
        connection_scope_digest="5" * 64,
        resource_id="101",
        issue_number=7,
        resource_updated_at="2026-11-01T10:00:00Z",
        payload_digest="6" * 64,
        response_sha256="7" * 64,
        observed_at="2026-11-01T10:00:00Z",
    )
    binding["verified_reference"] = copy.deepcopy(ref)
    scope = dict(
        version=1,
        action="status",
        transport="github_rest_poll_v1",
        project_id=run["project_id"],
        repository_id=run["repository_id"],
        native_user_id=env.user.id.hex,
        component_ids=[run["component_id"]],
        binding_key=binding["binding_key"],
        binding_digest=binding["digest"],
        binding_request_id=grant["request"]["expected_binding_request_id"],
        verified_reference_digest=digest(ref),
        project_binding_digest="4" * 64,
    )
    parent_id = uuid4().hex
    context_scope = {key: scope[key] for key in ("project_id", "repository_id", "native_user_id", "binding_digest")}
    context_scope.update(actor="7", issue={"kind": "issue", "number": 7})
    choice = {**context_scope, "run_id": env.run_id, "run_status": "paused", **reference(run)}
    decision = NaturalDecision(intent="resume", message="Continue local postprocessing.")
    parent = dict(
        id=parent_id,
        repository_id=run["repository_id"],
        provider="github",
        remote_id="42",
        actor="7",
        digest=ref["payload_digest"],
        issue=context_scope["issue"],
        verification=ref,
        source_snapshot={"body": "@forge continue"},
        command={"adapter": "addressed_status_v1", "action": "status"},
        status="processed",
        action_status="completed",
        feedback_status="pending",
        natural_turn=dict(
            state="consumed_continue",
            classifier_outcome="confirmed_valid",
            human_message="continue",
            policy_digest=natural_policy_digest(),
            source_digest=ref["payload_digest"],
            decision=decision.model_dump(mode="json"),
            reply=decision.message,
            context={"scope": context_scope, "candidates": [choice], "previous": None},
            resolution=resolve_task(context_scope, [choice], decision, answer="continue").model_dump(mode="json"),
        ),
    )
    rows[parent_id] = SimpleNamespace(
        id=UUID(parent_id), data=parent, status="processed", provider="github", repository_id=UUID(run["repository_id"])
    )

    class Inbox:
        async def find_by_id(self, key, **kwargs):
            return rows.get(key.hex)

        async def save(self, row):
            if getattr(row, "source_key", None):
                assert not any(
                    existing.id != row.id and getattr(existing, "source_key", None) == row.source_key
                    for existing in rows.values()
                )
            rows[row.id.hex] = row
            return row

        async def find_source(self, repository, provider, source_key):
            return next(
                (
                    row
                    for row in rows.values()
                    if row.repository_id == repository
                    and row.provider == provider
                    and getattr(row, "source_key", None) == source_key
                ),
                None,
            )

    async def receipt(key):
        return copy.deepcopy(rows[key].data)

    async def write(row, **changes):
        row.data = {**row.data, **copy.deepcopy(changes)}
        row.status = row.data["status"]

    async def identity(self, key, **kwargs):
        return env.user if key == env.user.id else None

    async def memberships(*args):
        return [member]

    async def project(*args, **kwargs):
        return SimpleNamespace(id=UUID(run["project_id"]))

    async def repo_lock(repository):
        assert repository == run["repository_id"]

    async def observation(project, repository, key):
        assert project == run["project_id"] and repository == run["repository_id"]
        value = await receipt(key)
        if value.get("natural_parent"):
            observed_run = await env.store.get_run(env.run_id)
            assert reference(observed_run, known_settlement=True) is not None, (
                observed_run["status"],
                observed_run.get("consumed_terminal_recovery"),
                observed_run.get("error"),
            )
            await authorize_projection(polling, value)
        return value

    async def verified_scope(delivery, *, remote=True):
        from firefly_forge.project_access import AccessDenied

        if (
            delivery != rows[delivery["id"]].data
            or delivery["actor"] != "7"
            or delivery["repository_id"] != run["repository_id"]
        ):
            raise AccessDenied("The current verified issue, actor or repository changed")
        return {
            **scope,
            "verified_receipt_id": delivery["id"],
            "observation_digest": digest(delivery["source_snapshot"]),
        }

    async def head(*args, **kwargs):
        return copy.deepcopy(binding)

    async def state(*args):
        return settings, {}

    async def find_settings(*args):
        return settings

    async def find_event(*args):
        return event

    async def retained_grant(*args, **kwargs):
        return copy.deepcopy(grant)

    async def verified_binding(*args):
        return copy.deepcopy(binding)

    async def original_run(key):
        return await env.store.get_run(key)

    async def preflight(context):
        assert context["binding"] == binding

    async def inbox_size(*args):
        return len(rows), 0

    monkeypatch.setattr(ForgeIdentityUserRepository, "find_by_id", identity)
    monkeypatch.setattr(ForgeMembershipRepository, "for_user", memberships)
    monkeypatch.setattr(scm_conversations.ForgeProjectRepository, "find_by_id", project)
    monkeypatch.setattr(scm_polling, "inbox_size", inbox_size)
    polling = SimpleNamespace(
        store=env.store,
        users=ForgeIdentityUserRepository(),
        memberships=SimpleNamespace(for_user=memberships),
        _observation=observation,
        _head=head,
        _state=state,
        conversation_scope=verified_scope,
    )
    collaboration = SimpleNamespace(
        store=env.store, polling=polling, inbox=Inbox(), receipt=receipt, _write_row=write, _repo_lock=repo_lock
    )
    polling.inbox = collaboration.inbox
    resume = SCMPollResumeAuthority(
        env.store,
        SimpleNamespace(),
        env.journal,
        SimpleNamespace(find_for_repository=find_settings),
        collaboration.inbox,
        SimpleNamespace(find_by_id=find_event),
    )
    resume.polling = polling
    resume._grant, resume._binding, resume._run_data, resume._preflight = (
        retained_grant,
        verified_binding,
        original_run,
        preflight,
    )
    collaboration.poll_resume_authority = resume
    env.store.scm_resume_authorities = {resume.actor_transport: resume}
    manager = SCMConsumedContinuations(collaboration, completions)
    real_context = SCMNaturalTasks._context

    async def context(self, delivery, scopes):
        if self.collaboration is collaboration:
            current = await env.store.get_run(env.run_id)
            found = reference(current)
            return {"scope": context_scope, "candidates": [{**choice, **found}] if found else [], "previous": None}
        return await real_context(self, delivery, scopes)

    monkeypatch.setattr(SCMNaturalTasks, "_context", context)
    return SimpleNamespace(
        env=env,
        api=completions,
        request=request,
        manager=manager,
        rows=rows,
        parent_id=parent_id,
        grant=grant,
        member=member,
        settings=settings,
        scope=scope,
        resume=resume,
    )


async def test_delegated_api_origin_claim_settles_without_worker_replay(continuation_case, monkeypatch):
    import sys

    c = continuation_case
    before = copy.deepcopy(await c.env.store.get_run(c.env.run_id))
    posts = len(c.env.posts)

    async def forbidden(*args, **kwargs):
        raise AssertionError("SCM continuation cannot dispatch models, tools, QA or verification")

    monkeypatch.setattr(c.env.provider, "execute", forbidden)
    monkeypatch.setattr(c.env.engine, "_project_command", forbidden)
    monkeypatch.setattr(c.env.engine, "_independent_review", forbidden)
    retain = c.manager._retain_state

    async def retain_success(receipt, state, **kwargs):
        if state == "consumed_held":
            raise AssertionError("The actual delegated consumer refused continuation") from sys.exception()
        return await retain(receipt, state, **kwargs)

    monkeypatch.setattr(c.manager, "_retain_state", retain_success)
    result = await c.manager.apply(c.parent_id)
    assert result["natural_turn"]["state"] == "consumed_accepted"
    after = await c.env.store.get_run(c.env.run_id)
    assert after["consumed_terminal_recovery"]["state"] == "accepted"
    assert after["status"] == "paused" and after["attempt"] == before["attempt"]
    assert after["verification_handoff"] == before["verification_handoff"]
    assert len(c.env.posts) == posts
    assert (await c.manager.apply(c.parent_id))["natural_turn"]["state"] == "consumed_accepted"
    monkeypatch.setattr(c.manager, "_retain_state", retain)
    duplicate = copy.deepcopy(c.rows[c.parent_id].data)
    duplicate_id = uuid4().hex
    duplicate.update(id=duplicate_id, natural_effects={})
    duplicate["natural_turn"]["state"] = "consumed_continue"
    duplicate["digest"] = duplicate["verification"]["payload_digest"] = "e" * 64
    duplicate["verification"]["resource_id"] = "102"
    c.rows[duplicate_id] = SimpleNamespace(
        id=UUID(duplicate_id),
        data=duplicate,
        status="processed",
        provider="github",
        repository_id=UUID(duplicate["repository_id"]),
    )
    assert (await c.manager.apply(duplicate_id))["natural_turn"]["state"] == "consumed_held"
    c.grant["request"]["active"] = False
    assert (await c.manager.apply(c.parent_id))["natural_turn"]["state"] == "consumed_held"
    assert (await c.env.store.get_run(c.env.run_id))["consumed_terminal_recovery"]["state"] == "accepted"
    assert (
        len([e for e in await c.env.journal.entries(c.env.run_id) if e["kind"] == "consumed_terminal_reconciliation"])
        == 1
    )


async def test_current_scope_refusals_never_accept_the_original_claim(continuation_case):
    c = continuation_case
    original = copy.deepcopy(c.rows[c.parent_id].data)
    before = [copy.deepcopy(row.data) for row in c.env.journal.records.rows]
    posts = len(c.env.posts)
    for change in ("actor", "repository", "grant", "permission", "enabled", "trusted", "component"):
        for key in list(c.rows):
            if key != c.parent_id:
                del c.rows[key]
        row = c.rows[c.parent_id]
        row.data, row.status = copy.deepcopy(original), "processed"
        grant, settings, role, enabled = (
            copy.deepcopy(c.grant),
            copy.deepcopy(c.settings.data),
            c.member.role,
            c.env.user.enabled,
        )
        try:
            if change == "actor":
                row.data["actor"] = "8"
            elif change == "repository":
                row.data["repository_id"] = uuid4().hex
            elif change == "grant":
                c.grant["request"]["active"] = False
            elif change == "permission":
                c.member.role = "viewer"
            elif change == "enabled":
                c.env.user.enabled = False
            elif change == "trusted":
                c.settings.data["trusted_actors"] = []
            else:
                c.grant["request"]["component_ids"] = ["other-component"]
            result = await c.manager.apply(c.parent_id)
            assert result["natural_turn"]["state"] == "consumed_held", change
            assert [row.data for row in c.env.journal.records.rows] == before, change
            assert (await c.env.store.get_run(c.env.run_id))["consumed_terminal_recovery"]["state"] == "claimed"
        finally:
            c.grant.clear()
            c.grant.update(grant)
            c.settings.data, c.member.role, c.env.user.enabled = settings, role, enabled
    assert len(c.env.posts) == posts


async def test_lost_settlement_response_reads_same_intent_without_reconciliation_replay(continuation_case, monkeypatch):
    import firefly_forge.consumed_terminal_reconciliations as native

    c = continuation_case
    original, calls = native.reconcile, []

    async def lost_response(*args, **kwargs):
        calls.append(True)
        await original(*args, **kwargs)
        raise OSError("Controlled lost local settlement response")

    monkeypatch.setattr(native, "reconcile", lost_response)
    held = await c.manager.apply(c.parent_id)
    assert held["natural_turn"]["state"] == "consumed_held"
    assert (await c.env.store.get_run(c.env.run_id))["consumed_terminal_recovery"]["state"] == "accepted"
    result = await c.manager.apply(c.parent_id)
    assert result["natural_turn"]["state"] == "consumed_accepted"
    assert calls == [True]
    entries = await c.env.journal.entries(c.env.run_id)
    assert sum(e["kind"] == "consumed_terminal_reconciliation" for e in entries) == 1
    assert sum(e["kind"] == "consumed_terminal_claim" for e in entries) == 1


async def test_uncertain_intent_without_native_receipt_is_never_dispatched_again(continuation_case, monkeypatch):
    import firefly_forge.consumed_terminal_reconciliations as native

    c, calls = continuation_case, []

    async def unknown(*args, **kwargs):
        calls.append(True)
        raise OSError("Controlled uncertainty before a known native receipt")

    monkeypatch.setattr(native, "reconcile", unknown)
    for _ in range(2):
        result = await c.manager.apply(c.parent_id)
        assert result["natural_turn"]["state"] == "consumed_held"
    duplicate = copy.deepcopy(c.rows[c.parent_id].data)
    duplicate_id = uuid4().hex
    duplicate.update(id=duplicate_id, natural_effects={})
    duplicate["natural_turn"]["state"] = "consumed_continue"
    duplicate["digest"] = duplicate["verification"]["payload_digest"] = "e" * 64
    duplicate["verification"]["resource_id"] = "102"
    c.rows[duplicate_id] = SimpleNamespace(
        id=UUID(duplicate_id),
        data=duplicate,
        status="processed",
        provider="github",
        repository_id=UUID(duplicate["repository_id"]),
    )
    assert (await c.manager.apply(duplicate_id))["natural_turn"]["state"] == "consumed_held"
    assert calls == [True]
    assert (await c.env.store.get_run(c.env.run_id))["consumed_terminal_recovery"]["state"] == "claimed"
    assert not any(e["kind"] == "consumed_terminal_reconciliation" for e in await c.env.journal.entries(c.env.run_id))


async def test_permission_row_change_at_native_writer_exit_refuses_commit(continuation_case, monkeypatch):
    from contextlib import asynccontextmanager

    from firefly_forge.project_access import AccessDenied

    c = continuation_case
    writer = c.env.store.source_budget_admission_unit
    mutations = []

    @asynccontextmanager
    async def revoke_at_writer_exit():
        async with writer():
            yield
            c.member.role = "viewer"
            mutations.append(True)

    monkeypatch.setattr(c.env.store, "source_budget_admission_unit", revoke_at_writer_exit)
    original = c.manager._retain_state

    async def held(receipt, state, **kwargs):
        if state == "consumed_held":
            import sys

            assert isinstance(sys.exception(), AccessDenied)
            assert "before commit" in str(sys.exception())
        return await original(receipt, state, **kwargs)

    monkeypatch.setattr(c.manager, "_retain_state", held)
    result = await c.manager.apply(c.parent_id)
    assert result["natural_turn"]["state"] == "consumed_held"
    assert mutations == [True]
    assert not any(e["kind"] == "consumed_terminal_reconciliation" for e in await c.env.journal.entries(c.env.run_id))
