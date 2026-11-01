# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Server consent phases consume typed SCM grants; native SCM records are a separate proof."""

import copy
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pyfly.data.transaction import current_unit_of_work

from firefly_forge.execution_requests import digest as request_digest
from firefly_forge.identity import Principal
from firefly_forge.journal import digest
from firefly_forge.linked_verification_models import LinkedVerificationInputV1
from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied, ProjectAccess
from firefly_forge.scm_linked_verification_models import (
    SCMLinkedVerificationAdmissionV1,
    SCMLinkedVerificationScopeV1,
)

pytest_plugins = ["test_linked_verification_consumer_host"]


@pytest.fixture(autouse=True)
def component_exists_before_fictional_history_is_frozen(monkeypatch):
    import test_original_selection_readmission_host as fixture

    original = fixture.historical_case

    def component_case():
        value = original()
        value["run"]["component_id"] = "inventory-api"
        return value

    monkeypatch.setattr(fixture, "historical_case", component_case)


@pytest.fixture
async def scm_case(linked_case, monkeypatch):
    c = linked_case
    run = await c.env.store.get_run(c.env.run_id)
    scope = SCMLinkedVerificationScopeV1(
        version=1,
        transport="github_rest_poll_linked_verification_v1",
        proposal_id=uuid4().hex,
        proposal_digest="1" * 64,
        project_id=run["project_id"],
        repository_id=run["repository_id"],
        component_id=run["component_id"],
        run_id=run["id"],
        native_user_id=c.env.user.id.hex,
        binding_key="2" * 64,
        binding_digest="3" * 64,
        binding_request_id=uuid4().hex,
    )
    actor = Principal(
        c.env.user.id,
        c.env.user.issuer,
        c.env.user.subject,
        authentication_method="scm_mapping",
        scm_delegation=scope.model_dump(mode="json"),
    )
    state = {"confirmed": False, "grant_current": True, "calls": [], "grant": None}
    authorize = ProjectAccess.authorize_actor

    async def controlled_execute(self, saved, project_id, permission="release"):
        if saved and saved.get("authentication_method") == "scm_mapping":
            assert saved == ProjectAccess.actor_snapshot(actor)
            assert project_id == scope.project_id and permission == "execute"
            return False
        return await authorize(self, saved, project_id, permission)

    monkeypatch.setattr(ProjectAccess, "authorize_actor", controlled_execute)

    class Prepared:
        def __init__(self, seal):
            self.seal = SCMLinkedVerificationAdmissionV1.model_validate(copy.deepcopy(seal))
            self.owner = c.env.store.execution_ownership_token
            self.generation = c.env.store.configuration.write_generation
            self.reader = CURRENT_PRINCIPAL.get()
            self.unit = None

        def fence(self):
            if current_unit_of_work() is not None and current_unit_of_work() is not self.unit:
                raise ValueError("SCM grant locks belong to another native writer unit")
            if (
                not state["grant_current"]
                or c.env.store.execution_ownership_token is not self.owner
                or c.env.store.configuration.write_generation is not self.generation
                or CURRENT_PRINCIPAL.get() != self.reader
            ):
                raise AccessDenied("Controlled native SCM consent was revoked")

        async def current(self):
            self.unit = current_unit_of_work()
            self.fence()
            await c.env.store.assert_ownership()
            if state.get("mutate_view"):
                value = self.seal.model_dump(mode="json")
                value["confirmation_digest"] = "f" * 64
                value["digest"] = digest({k: v for k, v in value.items() if k != "digest"})
                self.seal = SCMLinkedVerificationAdmissionV1.model_validate(value)
            self.fence()

    class Authority:
        async def review(self, current_actor, run_id):
            state["calls"].append("review")
            assert current_actor == actor and run_id == run["id"]
            snapshot = ProjectAccess.actor_snapshot(current_actor)
            assert snapshot["global_admin"] is False and snapshot["api_audience"] is None

        async def admit(self, current_actor, run_id, candidate, request):
            await self.review(current_actor, run_id)
            state["calls"].append("admit")
            if not state["confirmed"]:
                raise AccessDenied("A confirmed same-owner reply is required")
            value = dict(
                version=1,
                scope=scope.model_dump(mode="json"),
                candidate_digest=candidate.digest,
                request_id=request.request_id,
                request_digest=request_digest(request.model_dump(mode="json")),
                **{
                    key: "4" * 64
                    for key in (
                        "review_digest",
                        "publication_digest",
                        "confirmation_digest",
                        "host_administrator_digest",
                        "grant_digest",
                    )
                },
            )
            value["digest"] = digest(value)
            state["grant"] = copy.deepcopy(value)
            return Prepared(value)

        async def worker(self, saved_actor, identity, operation_data):
            state["calls"].append("worker")
            assert saved_actor == ProjectAccess.actor_snapshot(actor)
            assert identity == {"issuer": actor.issuer, "subject": actor.subject}
            if operation_data.get("scm_authority") != state["grant"]:
                raise AccessDenied("Stored operation changed its exact native grant")
            return Prepared(state["grant"])

    c.env.store.scm_linked_verification_authority = Authority()
    return SimpleNamespace(case=c, scope=scope, actor=actor, state=state)


def consent(scope, candidate):
    return LinkedVerificationInputV1(
        version=1,
        request_id=scope.proposal_id,
        candidate_digest=candidate["digest"],
        acknowledge_all_fresh_checks=True,
        acknowledge_no_coding_or_repair=True,
        acknowledge_original_history_and_budget_preserved=True,
    )


async def test_review_has_no_execution_authority_until_exact_confirmation_then_worker_revalidates(
    scm_case, monkeypatch
):
    s, c = scm_case, scm_case.case
    token = CURRENT_PRINCIPAL.set(s.actor)
    try:
        candidate = await c.api.candidate(c.env.run_id)
        body = consent(s.scope, candidate)
        with pytest.raises(AccessDenied, match="confirmed"):
            await c.api.request(c.env.run_id, body)
        assert not c.calls
        assert not await c.env.requests.records.count_for_run(c.env.store.run.id, "linked_verify")
        s.state["confirmed"] = True
        queued = await c.api.request(c.env.run_id, body)
        assert queued["status"] == "queued" and not c.calls
        worker = CURRENT_PRINCIPAL.set(None)
        try:
            await c.api.execute(body.request_id)
        finally:
            CURRENT_PRINCIPAL.reset(worker)
        result = await c.api.get(c.env.run_id, body.request_id)
        assert result["status"] == "completed", result
        assert "worker" in s.state["calls"] and len(c.calls) == 1
        assert result["candidate"]["actor_digest"] == candidate["actor_digest"]
        posts, commands, reached = len(c.env.posts), len(c.calls), []

        async def no_independent_model(*args, **kwargs):
            pass

        async def git_gate(*args, **kwargs):
            reached.append(True)
            raise RuntimeError("Controlled SCM-authorized Factory Git gate reached")

        monkeypatch.setattr(c.env.engine, "_independent_review", no_independent_model)
        monkeypatch.setattr(c.env.engine, "_checkpoint_commit", git_gate)
        await c.env.journal.resume(c.env.run_id)
        worker = CURRENT_PRINCIPAL.set(None)
        try:
            await c.env.engine.execute_run(c.env.run_id)
        finally:
            CURRENT_PRINCIPAL.reset(worker)
        current = await c.env.store.get_run(c.env.run_id)
        assert reached == [True], current.get("error")
        assert s.state["calls"].count("worker") >= 2
        assert len(c.env.posts) == posts and len(c.calls) == commands
    finally:
        CURRENT_PRINCIPAL.reset(token)


async def test_revoked_confirmed_scm_grant_holds_before_first_effect(scm_case):
    s, c = scm_case, scm_case.case
    token = CURRENT_PRINCIPAL.set(s.actor)
    try:
        s.state["confirmed"] = True
        body = consent(s.scope, await c.api.candidate(c.env.run_id))
        await c.api.request(c.env.run_id, body)
        s.state["grant_current"] = False
        worker = CURRENT_PRINCIPAL.set(None)
        try:
            await c.api.execute(body.request_id)
        finally:
            CURRENT_PRINCIPAL.reset(worker)
        result = await c.api.get(c.env.run_id, body.request_id)
        assert result["status"] == "needs_reconciliation" and not c.calls
        assert result["progress"]["started"] == 0
    finally:
        CURRENT_PRINCIPAL.reset(token)


async def test_mutated_prepared_grant_view_cannot_be_blessed_after_final_await(scm_case):
    s, c = scm_case, scm_case.case
    token = CURRENT_PRINCIPAL.set(s.actor)
    try:
        s.state["confirmed"] = True
        body = consent(s.scope, await c.api.candidate(c.env.run_id))
        s.state["mutate_view"] = True
        with pytest.raises(ValueError, match="SCM.*grant"):
            await c.api.request(c.env.run_id, body)
        assert not c.calls
        assert not await c.env.requests.records.count_for_run(c.env.store.run.id, "linked_verify")
    finally:
        CURRENT_PRINCIPAL.reset(token)


async def test_prepared_actor_cannot_change_delegation_while_context_object_stays_equal(scm_case):
    from firefly_forge.checkpoint_mode_runtime import mode_execution_scope

    s, c = scm_case, scm_case.case
    token = CURRENT_PRINCIPAL.set(s.actor)
    try:
        async with mode_execution_scope(c.env.journal, c.env.run_id):
            prepared = await c.api._prepare(c.env.run_id, s.actor)
            try:
                s.actor.scm_delegation["binding_digest"] = "f" * 64
                with pytest.raises(AccessDenied, match="identity"):
                    prepared.fence()
            finally:
                prepared.close()
    finally:
        CURRENT_PRINCIPAL.reset(token)
