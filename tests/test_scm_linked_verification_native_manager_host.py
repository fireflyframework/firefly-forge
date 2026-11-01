# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Actual SCM records → native request/worker; verified observation/persistence/runner seams are fictional."""

import copy
import hashlib
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

from pyfly.data.transaction import current_unit_of_work

from firefly_forge.identity_persistence import ForgeIdentityUserRepository
from firefly_forge.journal import digest
from firefly_forge.project_access import CURRENT_PRINCIPAL, ProjectAccess
from firefly_forge.scm_linked_verification import SCMLinkedVerifications
from firefly_forge.scm_linked_verification_records import SCMVerificationRecordV1
from firefly_forge.scm_natural_models import NaturalDecision, resolve_task
from firefly_forge.scm_natural_questions import natural_producer, natural_questions
from firefly_forge.scm_natural_tasks import natural_policy_digest

pytest_plugins = ["test_scm_linked_verification_consumer_host"]


async def test_persisted_same_owner_confirmation_reaches_actual_native_worker_once(linked_case, monkeypatch):
    c, rows = linked_case, {}
    env = c.env
    run = await env.store.get_run(env.run_id)
    env.user.api_roles = ["forge-admin"]
    env.user.api_roles_audience = "owned-native-host-api"
    env.user.api_roles_issued_at = 1.0
    env.user.api_roles_expires_at = datetime.now(UTC) + timedelta(hours=1)
    before = copy.deepcopy(run["usage"])
    member = SimpleNamespace(project_id=UUID(run["project_id"]), active=True, role="developer")
    scope = dict(
        version=1,
        action="status",
        transport="github_rest_poll_v1",
        project_id=run["project_id"],
        repository_id=run["repository_id"],
        native_user_id=env.user.id.hex,
        component_ids=[run["component_id"]],
        binding_key="1" * 64,
        binding_digest="2" * 64,
        binding_request_id=uuid4().hex,
        verified_reference_digest="3" * 64,
        project_binding_digest="4" * 64,
    )

    class Inbox:
        async def find_by_id(self, key, **kwargs):
            return rows.get(key.hex)

        async def conversation_context(self, delivery):
            return list(rows.values())

    async def members(user):
        assert user == env.user.id
        return [member]

    async def verified_scope(delivery, *, remote=True):
        assert delivery == rows[delivery["id"]].data
        assert not delivery.get("source_conflict") and delivery["actor"] == "7"
        return {
            **scope,
            "verified_receipt_id": delivery["id"],
            "observation_digest": digest(delivery["source_snapshot"]),
        }

    async def head(project, repository, key):
        assert (project, repository, key) == (run["project_id"], run["repository_id"], scope["binding_key"])
        return {"request": {"allowed_actions": ["respond", "status"]}}

    async def receipt(key):
        return copy.deepcopy(rows[key].data)

    async def write(row, **changes):
        row.data = {**row.data, **copy.deepcopy(changes)}

    async def repo_lock(repository):
        assert repository == run["repository_id"] and current_unit_of_work() is not None

    async def project(self, key, **kwargs):
        assert key.hex == run["project_id"]
        return SimpleNamespace(id=key)

    from firefly_forge import scm_conversations
    from firefly_forge.access_persistence import ForgeMembershipRepository

    monkeypatch.setattr(scm_conversations.ForgeProjectRepository, "find_by_id", project)

    async def membership_rows(self, user):
        return await members(user)

    monkeypatch.setattr(ForgeMembershipRepository, "for_user", membership_rows)
    polling = SimpleNamespace(
        users=ForgeIdentityUserRepository(),
        memberships=SimpleNamespace(for_user=members),
        conversation_scope=verified_scope,
        _head=head,
    )
    collaboration = SimpleNamespace(
        store=env.store, polling=polling, inbox=Inbox(), receipt=receipt, _write_row=write, _repo_lock=repo_lock
    )
    authority = SCMLinkedVerifications(collaboration, c.api)
    env.store.scm_linked_verification_authority = collaboration.linked_verification_authority = authority
    proposal = uuid4().hex
    context_scope = {key: scope[key] for key in ("project_id", "repository_id", "native_user_id", "binding_digest")}
    context_scope.update(actor="7", issue={"kind": "issue", "number": 4})
    original = dict(
        id=proposal,
        digest="5" * 64,
        repository_id=run["repository_id"],
        actor="7",
        provider="github",
        issue=context_scope["issue"],
        verification={"transport": "github_rest_poll_v1"},
        source_snapshot={"body": "@forge, prepara las comprobaciones nuevas"},
        status="processed",
        action_status="completed",
        feedback_status="pending",
        natural_turn=dict(
            human_message="prepara las comprobaciones nuevas",
            source_digest="5" * 64,
            policy_digest=natural_policy_digest(),
            classifier_outcome="confirmed_valid",
            decision=NaturalDecision(
                intent="verify", message="Revisemos las comprobaciones.", language="es", verification_consent="request"
            ).model_dump(mode="json"),
            reply="Revisemos las comprobaciones.",
            context={"scope": context_scope, "previous": None},
            resolution=None,
        ),
    )
    rows[proposal] = SimpleNamespace(data=original)
    record = await authority.propose(copy.deepcopy(original), env.run_id)
    assert record.review is not None and env.requests.records.rows[-1].kind != "linked_verify"
    original = rows[proposal].data
    original.update(feedback_status="sent", feedback_remote={"id": 101})
    body = "Controlled AI-authored review\n\n" + record.review.question
    original["feedback_intent"] = dict(
        body=body,
        body_sha256=hashlib.sha256(body.encode()).hexdigest(),
        natural_turn_digest=digest(natural_producer(original)),
    )
    question = natural_questions(original)
    assert question is not None
    human = "sí, ejecuta esas comprobaciones"
    decision = NaturalDecision(
        intent="verify",
        message="Recibido.",
        language="es",
        verification_consent="approve",
        verification_affirmative_span=human,
    )
    context = dict(scope=context_scope, candidates=[question], previous=None)
    response_id = uuid4().hex
    response = dict(
        id=response_id,
        digest="6" * 64,
        repository_id=run["repository_id"],
        actor="7",
        provider="github",
        issue=context_scope["issue"],
        verification={"transport": "github_rest_poll_v1"},
        source_snapshot={"body": human},
        natural_turn=dict(
            human_message=human,
            decision=decision.model_dump(mode="json"),
            context=context,
            policy_digest=natural_policy_digest(),
            resolution=resolve_task(context_scope, [question], decision, answer=human).model_dump(mode="json"),
        ),
    )
    rows[response_id] = SimpleNamespace(data=response)
    # This fixture supplies the verified observation/read model, not a real SCM database.
    from firefly_forge.scm_natural_tasks import SCMNaturalTasks

    real_context = SCMNaturalTasks._context

    async def observed_context(self, delivery, scopes):
        if self.collaboration is collaboration:
            assert delivery["id"] == response_id
            return copy.deepcopy(context)
        return await real_context(self, delivery, scopes)

    monkeypatch.setattr(SCMNaturalTasks, "_context", observed_context)
    queued = await authority.confirm(copy.deepcopy(response), proposal)
    assert queued is not None and queued.status == "queued"
    record = SCMVerificationRecordV1.model_validate(rows[proposal].data["natural_verification"])
    actor = await authority.actor(record)
    assert ProjectAccess.actor_snapshot(actor)["global_admin"] is False and actor.api_audience is None
    assert len([r for r in env.requests.records.rows if r.kind == "linked_verify"]) == 1
    token = CURRENT_PRINCIPAL.set(None)
    try:
        await c.api.execute(proposal)
    finally:
        CURRENT_PRINCIPAL.reset(token)
    final = await authority.status(record)
    assert final is not None and final.status == "completed"
    assert final.progress.completed == record.review.candidate.effect_budget == len(c.calls)
    repeated = await authority.confirm(copy.deepcopy(response), proposal)
    assert repeated.status == "completed" and len(c.calls) == final.progress.completed
    assert (await env.store.get_run(env.run_id))["usage"] == before
