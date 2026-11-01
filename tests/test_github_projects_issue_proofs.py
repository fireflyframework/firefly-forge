# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Controlled canonical-context tests do not claim live provider or actor authority."""

import copy
from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import UUID

import pytest

from firefly_forge.github_projects_requests import GitHubProjectsRequests
from firefly_forge.github_projects_worker import BoardConflict, GitHubProjectsWorker
from firefly_forge.journal import digest
from firefly_forge.native_question_publications import INTENT, RESERVED, NativeQuestionPublications
from firefly_forge.project_access import AccessDenied


@pytest.mark.parametrize(
    "field", [None, "issue", "actor", "repository_id", "provider", "remote_id", "verification", "source_snapshot"]
)
async def test_native_question_copies_must_match_controlled_canonical_source(monkeypatch, field):
    native = {
        "version": 1,
        "kind": "native_worker_question_v1",
        "run_id": "1" * 32,
        "question_id": "question",
        "source_receipt_id": "2" * 32,
        "native_user_id": "3" * 32,
        **{
            key: "a" * 64
            for key in (
                "feedback_digest",
                "activation_digest",
                "source_authority_digest",
                "verified_reference_digest",
                "binding_digest",
                "native_identity_digest",
                "question_cut",
            )
        },
    }
    source = {
        "repository_id": "4" * 32,
        "provider": "github",
        "remote_id": "77",
        "actor": "7",
        "issue": {"kind": "issue", "number": 4},
        "verification": {"retained": "canonical"},
        "source_snapshot": {"original": True},
    }
    delivery = {
        **copy.deepcopy(source),
        "id": "5" * 32,
        "digest": digest(native),
        "native_question": native,
        "feedback_intent": {"request_id": "question"},
    }
    context = {
        "native": native,
        "source": source,
        "entries": [
            {"kind": RESERVED, "payload": {"receipt_id": delivery["id"], "native_question": native}},
            {"kind": INTENT, "payload": {"receipt_id": delivery["id"], **delivery["feedback_intent"]}},
        ],
    }

    async def canonical(self, run_id, *, pending, archived_binding):
        assert run_id == native["run_id"] and pending is False and archived_binding == native["binding_digest"]
        return context

    monkeypatch.setattr(NativeQuestionPublications, "_context", canonical)
    collaboration = SimpleNamespace(store=None, polling=None, deployments=None)
    tickets = object.__new__(GitHubProjectsRequests)
    tickets.collaboration = collaboration
    run = {"id": native["run_id"]}
    if field:
        delivery[field] = {"kind": "issue", "number": 999} if field == "issue" else "substituted"
        await NativeQuestionPublications(collaboration).authorize_reference(delivery, run)
        with pytest.raises(AccessDenied, match="canonical source"):
            await tickets.verify_issue(delivery, run)
    else:
        await tickets.verify_issue(delivery, run)

        class Session:
            async def get(self, entity, identifier):
                return SimpleNamespace(data=delivery)

        class Store:
            @asynccontextmanager
            async def _unit(self):
                yield Session()

        tickets.store = Store()
        run.update(
            repository_id=source["repository_id"],
            issue_context={
                "provider": "github",
                "receipt_id": delivery["id"],
                "actor": source["actor"],
                "kind": "issue",
                "number": 999,
            },
        )
        with pytest.raises(AccessDenied, match="verified native receipt"):
            await tickets.issue_references(run)


async def test_latest_projection_uses_same_verified_resolver_when_issue_context_is_absent():
    calls = []

    class Session:
        async def get(self, entity, identifier):
            return SimpleNamespace(repository_id=UUID("a" * 32), sequence=1)

        async def scalars(self, query):
            return [SimpleNamespace(id=UUID("2" * 32))]

    class Store:
        @asynccontextmanager
        async def _unit(self):
            yield Session()

        async def get_run(self, run_id):
            return {"id": run_id, "issue_context": None}

    async def verified(run):
        calls.append(run["id"])
        return [{"kind": "issue", "number": 4}]

    worker = object.__new__(GitHubProjectsWorker)
    worker.store = Store()
    worker.tickets = SimpleNamespace(issue_references=verified)
    with pytest.raises(BoardConflict, match="newer native unit"):
        await worker.current_issue_projection(
            {"run_id": "1" * 32, "request": {"project_id": "b" * 32}}, {"kind": "issue", "number": 4}
        )
    assert calls == ["2" * 32]


async def test_issue_reference_discovery_is_paged_beyond_two_hundred_retained_rows():
    calls = []
    rows = [SimpleNamespace(sequence=i, data={"id": str(i)}) for i in range(1, 202)]
    rows[-1].data.update(issue={"kind": "issue", "number": 4}, digest="a" * 64)

    class Session:
        async def scalars(self, query):
            limit = query._limit_clause.value
            start = len(calls) * limit
            calls.append(query)
            return rows[start : start + limit]

    @asynccontextmanager
    async def unit():
        yield Session()

    async def verify(delivery, run):
        if delivery["id"] != "201":
            raise ValueError("unrelated historical row")

    tickets = GitHubProjectsRequests.__new__(GitHubProjectsRequests)
    tickets.store = SimpleNamespace(_unit=unit)
    tickets.verify_issue = verify
    references = await tickets.issue_references({"id": "1" * 32, "repository_id": "2" * 32})
    assert references == [{"kind": "issue", "number": 4, "source_run_id": "1" * 32, "receipt_digest": "a" * 64}]
    assert len(calls) == 3 and all(query._limit_clause.value == 100 for query in calls)
