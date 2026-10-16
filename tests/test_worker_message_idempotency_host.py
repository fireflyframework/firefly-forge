# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Actual Journal behavior at its ORM key bound; no PostgreSQL or provider I/O."""

from types import SimpleNamespace

import pytest
from test_worker_reply_authority_host import setup  # noqa: F401

from firefly_forge.agent_roles import WorkerFeedback, WorkerRequest, WorkerResponse
from firefly_forge.journal import Journal
from firefly_forge.persistence import ForgeJournalEntity
from firefly_forge.project_access import CURRENT_PRINCIPAL


class BoundedRows:
    def __init__(self, rows=()):
        self.rows = list(rows)

    async def find_for_run(self, run_id):
        return self.rows

    async def save(self, row):
        if isinstance(row, ForgeJournalEntity):
            limit = ForgeJournalEntity.__table__.c.idempotency_key.type.length
            assert limit == 64
            assert len(row.idempotency_key) <= limit, "Journal key exceeds the actual ORM column"
        if row not in self.rows:
            self.rows.append(row)


class CurrentRunJournal(Journal):
    async def _run(self, run_id):
        assert run_id == self.store.run.id.hex
        return self.store.run


@pytest.fixture
def message_journal(setup):  # noqa: F811
    store, _, actor = setup
    task = SimpleNamespace(task_id="verify", status="pending", data={})
    journal = CurrentRunJournal(store, tasks=BoundedRows([task]), records=BoundedRows())
    return store, journal, actor


def feedback(identifier):
    return WorkerFeedback(
        id=identifier,
        role_id="qa",
        task_id="verify",
        attempt=1,
        disposition="clarification",
        summary="Keep the fictional result?",
        request=WorkerRequest(
            id="question-1", kind="verification", question="Keep the result?", context="Offline storage control"
        ),
    )


@pytest.mark.parametrize(
    "identifier,expected",
    [
        ("f" * 48, "worker-feedback:" + "f" * 48),
        ("f" * 49, "6c726ccbd888dae690114295731bd521be60b74c5bdf17dce1142d0508328593"),
        ("live-poll-feedback-" + "a" * 32, "c858fb8af9e01f0469c250951623b9fa2061061d99ef5b77b71fff09141eac99"),
        ("f" * 80, "f7ded868c08f25454cbd49abcc8024050cfc3d5c115b25c351b52334ac60533e"),
    ],
    ids=["legacy-boundary48", "digest-boundary49", "live-id51", "maximum80"],
)
async def test_feedback_ids_fit_orm_and_replay_without_payload_rewriting(message_journal, identifier, expected):
    store, journal, _ = message_journal
    del store.run.data["worker_request"]
    value = feedback(identifier)
    first = await journal.worker_feedback(store.run.id.hex, value)
    assert first["idempotency_key"] == expected
    assert first["payload"]["id"] == identifier
    assert await journal.worker_feedback(store.run.id.hex, value) == first
    with pytest.raises(ValueError, match="different content"):
        await journal.worker_feedback(store.run.id.hex, value.model_copy(update={"summary": "Changed decision"}))
    assert len(journal.records.rows) == 1
    assert store.run.status == "paused" and store.run.data["worker_request"]["status"] == "pending"
    assert Journal.verify_chain([row.data for row in journal.records.rows])


@pytest.mark.parametrize(
    "identifier,expected",
    [
        ("f" * 48, "worker-response:" + "f" * 48),
        ("f" * 49, "78b11a122e7b19002ad43ec48e4dc2ba3f95e02a60d179e999b1e4a2c8f0943e"),
        ("live-poll-feedback-" + "a" * 32, "6db5559af5a8f9a93abe5af13ec83ebce422662e02acea2f4f9cfe84ede9e27d"),
        ("f" * 80, "e99c469aedeaa9f0c9b7f472bfa8fd83d0068f61e97500197e25a0ae165baf85"),
    ],
    ids=["legacy-boundary48", "digest-boundary49", "live-id51", "maximum80"],
)
async def test_response_ids_fit_orm_and_keep_semantic_duplicate_refusal(message_journal, identifier, expected):
    store, journal, actor = message_journal
    value = WorkerResponse(id=identifier, actor="Fictional operator", answer="Keep the result")
    token = CURRENT_PRINCIPAL.set(actor)
    try:
        first = await journal.worker_response(store.run.id.hex, "question-1", value)
        assert first["idempotency_key"] == expected
        assert first["payload"]["id"] == identifier
        assert await journal.worker_response(store.run.id.hex, "question-1", value) == first
        with pytest.raises(ValueError, match="different content"):
            await journal.worker_response(
                store.run.id.hex, "question-1", value.model_copy(update={"answer": "A different decision"})
            )
        assert len(journal.records.rows) == 1
        assert store.run.status == "paused" and store.run.data["worker_request"]["status"] == "answered"
        assert Journal.verify_chain([row.data for row in journal.records.rows])
    finally:
        CURRENT_PRINCIPAL.reset(token)


async def test_same_long_id_is_namespaced_between_feedback_and_response(message_journal):
    store, journal, actor = message_journal
    del store.run.data["worker_request"]
    first = await journal.worker_feedback(store.run.id.hex, feedback("f" * 80))
    token = CURRENT_PRINCIPAL.set(actor)
    try:
        second = await journal.worker_response(
            store.run.id.hex, "question-1", WorkerResponse(id="f" * 80, actor="Fictional operator", answer="Keep it")
        )
    finally:
        CURRENT_PRINCIPAL.reset(token)
    assert first["idempotency_key"] != second["idempotency_key"]
    assert first["payload"]["id"] == second["payload"]["id"] == "f" * 80
    assert Journal.verify_chain([row.data for row in journal.records.rows])
