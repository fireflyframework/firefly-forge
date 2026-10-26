# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""A new test run has its own creation receipt and read-only issue lineage."""

import copy
from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import UUID

import pytest
from test_checkpoint_test_source_host import PARENT, PROJECT, REFERENCE, REPO, REVISION, TREE

from firefly_forge.git_deliveries import delivery_digest
from firefly_forge.scm_conversations import SCMConversations

CHILD, CREATION, NATIVE, PARENT_RECEIPT = "9" * 32, "7" * 32, "e" * 32, "f" * 32


@pytest.fixture
def lineage(monkeypatch):
    state = {"allowed": True, "read_allowed": True, "queries": 0}
    scope = dict(project_id=PROJECT, repository_id=REPO, native_user_id=NATIVE, component_ids=["api"])
    delivery = dict(
        id="8" * 32, repository_id=REPO, provider="github", actor="17", issue={"kind": "issue", "number": 4}
    )
    previous = {
        **delivery,
        "id": PARENT_RECEIPT,
        "run_id": PARENT,
        "status": "processed",
        "action_status": "completed",
        "command": {},
        "verification": {"transport": "github_rest_poll_v1"},
    }
    parent = dict(id=PARENT, repository_id=REPO, project_id=PROJECT, component_id="api", status="failed")
    snapshot = dict(
        version=1,
        reference=REFERENCE,
        repository_id=REPO,
        project_id=PROJECT,
        component_id="api",
        revision=REVISION,
        source_tree=TREE,
        branch="forge/" + PARENT[:12],
        remote_receipt_digest="4" * 64,
        journal_head="5" * 64,
        parent_scope_digest="6" * 64,
    )
    snapshot["digest"] = delivery_digest(snapshot)
    child = {
        **parent,
        "id": CHILD,
        "status": "queued",
        "operation": "test",
        "publish": False,
        "source_checkpoint": REFERENCE,
        "source_checkpoint_snapshot": copy.deepcopy(snapshot),
    }
    receipt = dict(
        version=1,
        request_id=CREATION,
        run_id=CHILD,
        repository_id=REPO,
        project_id=PROJECT,
        component_id="api",
        original_actor={"user_id": NATIVE},
        source_checkpoint=copy.deepcopy(snapshot),
        digest="6" * 64,
    )

    @asynccontextmanager
    async def unit(**kwargs):
        yield

    async def context(_):
        return [SimpleNamespace(data=copy.deepcopy(previous), sequence=1)]

    async def current(run_id):
        return copy.deepcopy(parent if run_id == PARENT else child)

    async def authorize(*args):
        if not state["allowed"]:
            raise ValueError("Current native read authority revoked")

    async def children(parents, repository):
        assert len(parents) <= 200 and repository == UUID(REPO)
        state["queries"] += 1
        return [UUID(CREATION)] if UUID(PARENT) in parents else []

    async def load(self, request_id):
        assert request_id == CREATION
        return copy.deepcopy(receipt)

    from firefly_forge.project_access import AccessDenied, ProjectAccess
    from firefly_forge.run_creation_requests import RunCreationRequests

    async def read(*args):
        if not state["read_allowed"]:
            raise AccessDenied("The current project reader is no longer authorized")

    monkeypatch.setattr(ProjectAccess, "authorize_actor", read)

    monkeypatch.setattr(RunCreationRequests, "_load", load)
    store = SimpleNamespace(
        _unit=unit, get_run=current, run_creation_records=SimpleNamespace(checkpoint_children=children)
    )
    collaboration = SimpleNamespace(
        store=store, inbox=SimpleNamespace(ticket_context=context), polling=SimpleNamespace(ticket_reference=authorize)
    )
    return SimpleNamespace(
        state=state,
        scope=scope,
        delivery=delivery,
        parent=parent,
        child=child,
        receipt=receipt,
        service=SCMConversations(collaboration),
    )


async def test_authenticated_ticket_exposes_child_with_its_own_native_creation_identity(lineage):
    c = lineage
    refs, runs = await c.service.references(c.delivery, [c.scope])
    assert set(runs) == {PARENT, CHILD}
    native = next(ref for ref in refs if ref["run_id"] == CHILD)
    assert native["receipt_id"] == CREATION and native["parent_receipt_id"] == PARENT_RECEIPT
    assert native["kind"] == "native_checkpoint_test"
    assert native["receipt_id"] != PARENT_RECEIPT


@pytest.mark.parametrize(
    "mutation", ["no_parent_authority", "denied_read", "foreign_repo", "foreign_component", "changed_snapshot"]
)
async def test_child_trace_never_bypasses_parent_authority_or_native_scope(lineage, mutation):
    c = lineage
    if mutation == "no_parent_authority":
        c.state["allowed"] = False
    elif mutation == "denied_read":
        c.state["read_allowed"] = False
    elif mutation == "foreign_repo":
        c.child["repository_id"] = "0" * 32
    elif mutation == "foreign_component":
        c.child["component_id"] = "other"
    else:
        c.child["source_checkpoint_snapshot"]["revision"] = "0" * 40
    refs, runs = await c.service.references(c.delivery, [c.scope])
    assert CHILD not in runs and not any(ref["run_id"] == CHILD for ref in refs)
    if mutation == "no_parent_authority":
        assert c.state["queries"] == 0


async def test_current_project_teammate_can_read_child_without_becoming_its_creator(lineage):
    c = lineage
    c.scope["native_user_id"] = "0" * 32
    refs, runs = await c.service.references(c.delivery, [c.scope])
    assert CHILD in runs
    native = next(ref for ref in refs if ref["run_id"] == CHILD)
    assert native["creator_user_id"] == NATIVE
    assert native["creator_user_id"] != c.scope["native_user_id"]


@pytest.mark.parametrize("intent", ["answer", "resume", "checkpoint"])
def test_read_only_child_trace_cannot_supply_question_or_checkpoint_authority(intent):
    from firefly_forge.scm_natural_models import NaturalDecision, resolve_task

    scope = dict(
        repository_id=REPO,
        project_id=PROJECT,
        issue={"kind": "issue", "number": 4},
        actor="17",
        binding_digest="a" * 64,
        native_user_id=NATIVE,
    )
    # Even incorrectly projected question fields must not turn read lineage into a grant.
    candidate = {
        **scope,
        "run_id": CHILD,
        "run_status": "paused",
        "origin": "native_checkpoint_test",
        "read_only": True,
        "question_id": "not-a-child-grant",
        "question_digest": "b" * 64,
        "question_status": "pending" if intent == "answer" else "answered",
        "consumed": False,
        "response_digest": "c" * 64,
    }
    decision = NaturalDecision(intent=intent, message="Read-only native trace")
    result = resolve_task(scope, [candidate], decision, answer="Please continue")
    assert result.state == "waiting" and result.run_id is None
    status = resolve_task(scope, [candidate], NaturalDecision(intent="status", message="Read status"), answer="Status?")
    assert status.state == "status" and status.run_id == CHILD


@pytest.mark.parametrize("operation", ["test", "maintain"])
async def test_natural_teammate_child_projection_never_inherits_the_parent_question(operation):
    from test_scm_natural_worker_context_host import fixture

    natural, delivery, scopes, rows, runs = await fixture()
    delivery["actor"] = "another-current-project-reader"
    child = copy.deepcopy(runs["1" * 32])
    child.update(
        id=CHILD,
        status="paused",
        operation=operation,
        source_checkpoint_snapshot={"reference": REFERENCE, "revision": REVISION, "operation": operation},
    )
    runs[CHILD] = child

    async def references(*args, **kwargs):
        return (
            [
                dict(
                    kind="native_checkpoint_maintain" if operation == "maintain" else "native_checkpoint_test",
                    receipt_id=CREATION,
                    parent_receipt_id=rows[0].data["id"],
                    run_id=CHILD,
                )
            ],
            {CHILD: copy.deepcopy(child)},
        )

    natural.conversations.references = references
    result = await natural._context(delivery, scopes)
    assert len(result["candidates"]) == 1
    candidate = result["candidates"][0]
    assert candidate["run_id"] == CHILD
    assert candidate["origin"] == (
        "native_checkpoint_maintain" if operation == "maintain" else "native_checkpoint_test"
    )
    assert candidate["read_only"] is True and candidate["operation"] == operation
    assert candidate["source_parent_run_id"] == PARENT and candidate["source_revision"] == REVISION
    assert candidate["question_id"] is None and candidate.get("publication") is None
