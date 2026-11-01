# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Signed delivery wakes authenticated observation; it never owns the comment action."""

import copy
from types import SimpleNamespace

import pytest
from test_scm_natural_checkpoints_host import enable_checkpoint
from test_scm_natural_intake_host import prepared
from test_scm_polling import poll_case as poll_case  # noqa: PLC0414
from test_scm_signed_natural_parity_host import signed

from firefly_forge.project_access import CURRENT_PRINCIPAL


def comment_receipts(c, number):
    return [row.data for row in c.db["inbox"].values() if row.source_key == f"comment:{number}"]


async def observe(c):
    for _ in range(3):
        await c.service.step(c.repository)


@pytest.mark.parametrize("installation", ["absent", {"id": 34}])
@pytest.mark.parametrize("order", ["webhook_first", "poll_first"])
async def test_same_comment_uses_existing_authorized_checkpoint_once(poll_case, monkeypatch, installation, order):
    async with poll_case() as c:
        natural, initial = await prepared(c, monkeypatch, intent="checkpoint")
        await natural.process(initial)
        await signed(c, number=88, body="@forge status before renewed consent")
        await enable_checkpoint(c, natural, initial)
        c.db["runs"][c.run_id].data.update(
            status="failed", git_delivery_policy={"enabled": True, "remote": "draft_pr"}, source_tree="b" * 40
        )
        calls = []

        async def candidate(run_id):
            return {"expected_head": "a" * 40, "expected_source_digest": "d" * 64}

        async def request(run_id, body):
            assert CURRENT_PRINCIPAL.get().authentication_method == "scm_mapping"
            calls.append(run_id)
            return {"request_digest": "c" * 64}

        c.store.execution_requests = SimpleNamespace(
            git_deliveries=SimpleNamespace(deliveries=SimpleNamespace(candidate=candidate), request=request)
        )
        body = "@forge save the current progress as a draft pull request"
        if order == "poll_first":
            c.state["comments"].append(c.comment(102, body))
            c.state["clock"] += 3600
            await observe(c)
        delivery, raw, headers = await signed(c, number=102, body=body, installation=installation)
        assert delivery["api_observation"]["state"] == "pending"
        assert delivery["command"] is None
        assert await c.collaboration.receive("github", c.repository, raw, headers) == delivery
        await c.collaboration.process(delivery["id"])
        await observe(c)
        rows = comment_receipts(c, 102)
        assert len(rows) == 1 and rows[0]["id"] != delivery["id"]
        assert rows[0]["verification"]["transport"] == "github_rest_poll_v1"
        before = c.state["natural_calls"]
        result = await c.collaboration.process(rows[0]["id"])
        assert result["natural_turn"]["state"] == "checkpoint_queued", result.get("reason")
        assert calls == [c.run_id] and c.state["natural_calls"] == before + 1
        await c.collaboration.process(rows[0]["id"])
        correlated = await c.collaboration.process(delivery["id"])
        assert correlated["api_observation"]["receipt_id"] == rows[0]["id"]
        assert correlated["api_observation"]["state"] == "observed"
        assert correlated["feedback_status"] == "not_requested" and not correlated.get("natural_turn")
        repeated, _, _ = await signed(c, number=102, body=body, installation=installation)
        assert repeated["id"] != delivery["id"]
        await c.collaboration.process(repeated["id"])
        assert len(comment_receipts(c, 102)) == 1
        assert calls == [c.run_id] and not c.db["journal"]


async def test_durable_pending_marker_preserves_provider_backoff_and_cursor(poll_case):
    async with poll_case() as c:
        await c.ready()
        await c.consent()
        delivery, _, _ = await signed(c)
        row = c.db["settings"][c.repository]
        cursor = row.data["_github_poll_v1"]["cursor"]
        cursor["retry_at"] = c.state["clock"] + 300
        cursor["next_due_at"] = c.state["clock"] + 600
        saved = copy.deepcopy(cursor)
        await c.collaboration.process(delivery["id"])
        scheduled = row.data["_github_poll_v1"]["cursor"]
        assert {k: v for k, v in scheduled.items() if k != "next_due_at"} == {
            k: v for k, v in saved.items() if k != "next_due_at"
        }
        assert scheduled["next_due_at"] <= c.state["clock"]
        before = len(c.state["requests"])
        assert not await c.service.step(c.repository)
        assert len(c.state["requests"]) == before
        fresh = type(c.service)(c.store, c.scm)
        fresh.collaboration = c.collaboration
        c.collaboration.polling = fresh
        c.state["clock"] += 301
        for _ in range(3):
            await fresh.step(c.repository)
        result = await c.collaboration.process(delivery["id"])
        assert result["api_observation"]["state"] == "observed"
        assert result["api_observation"]["receipt_id"] == comment_receipts(c, 90)[0]["id"]


async def test_historical_signed_owner_is_not_promoted_or_reclassified(poll_case):
    async with poll_case() as c:
        await c.ready()
        await c.consent()
        delivery, _, _ = await signed(c)
        old = c.db["inbox"][delivery["id"]]
        old.source_key = "comment:90"
        old.data.pop("api_observation")
        old.data.update(status="needs_reconciliation", natural_turn={"state": "unknown"})
        old.status = "needs_reconciliation"
        saved = copy.deepcopy(old.data)
        new, _, _ = await signed(c)
        result = await c.collaboration.process(new["id"])
        assert result["status"] == "denied"
        assert result["reason"] == "historical_comment_owner_requires_review"
        assert old.data == saved and not c.db["journal"] and not c.state["posts"]


async def test_unmentioned_reply_is_observed_without_fabricating_a_command(poll_case, monkeypatch):
    from test_scm_natural_intake_host import product_context

    async with poll_case() as c:
        natural, receipt = await prepared(c, monkeypatch)
        await product_context(c, natural, receipt)
        body = "Ventas revisa. Operaciones reasigna."
        delivery, _, _ = await signed(c, number=101, body=body)
        assert delivery["status"] == "received" and delivery["command"] is None
        await c.collaboration.process(delivery["id"])
        await observe(c)
        observed = comment_receipts(c, 101)[0]
        assert observed["command"]["source_kind"] == "retained_question_reply"
        result = await c.collaboration.process(observed["id"])
        assert result["natural_turn"]["resolution"]["state"] == "product_answer"
        assert result["natural_turn"]["human_message"] == body
        assert not c.db["journal"]


@pytest.mark.parametrize("change", ["key", "actor", "connection", "administrator", "configuration", "source"])
async def test_revoked_or_changed_wake_cannot_schedule_or_classify(poll_case, change):
    async with poll_case() as c:
        await c.ready()
        await c.consent()
        delivery, _, _ = await signed(c)
        if change == "key":
            c.store.configuration._write(c.collaboration.secrets_path, {})
        elif change == "actor":
            c.db["settings"][c.repository].data["trusted_actors"] = []
        elif change == "connection":
            c.db["repositories"][c.repository].data["remote_repository_id"] = "43"
        elif change == "administrator":
            c.db["users"][c.owner.id.hex].enabled = False
        elif change == "configuration":
            c.db["settings"][c.repository].data["enabled"] = False
        else:
            c.db["inbox"][delivery["id"]].data["source_snapshot"]["body"] += " tampered"
        before = copy.deepcopy(c.db["settings"][c.repository].data["_github_poll_v1"])
        requests = len(c.state["requests"])
        result = await c.collaboration.process(delivery["id"])
        assert result["status"] == "denied" and result["reason"] == "api_observation_scope_unavailable"
        assert c.db["settings"][c.repository].data["_github_poll_v1"] == before
        assert len(c.state["requests"]) == requests and not c.state["posts"] and not c.db["journal"]


async def test_pending_wake_retries_after_scan_race_without_resetting_cursor(poll_case):
    async with poll_case() as c:
        await c.ready()
        await c.consent()
        c.state["clock"] += 3600
        await c.service.step(c.repository)
        delivery, _, _ = await signed(c)
        await c.collaboration.process(delivery["id"])
        # A scan that missed the new provider comment may finish after the wake.
        c.state["comments"] = [item for item in c.state["comments"] if item["id"] != 90]
        await observe(c)
        assert not comment_receipts(c, 90)
        saved = copy.deepcopy(c.db["settings"][c.repository].data["_github_poll_v1"]["cursor"])
        assert saved["next_due_at"] > c.state["clock"]
        pending = await c.collaboration.process(delivery["id"])
        assert pending["status"] == "received" and pending["api_observation"]["state"] == "pending"
        cursor = c.db["settings"][c.repository].data["_github_poll_v1"]["cursor"]
        assert {k: v for k, v in cursor.items() if k != "next_due_at"} == {
            k: v for k, v in saved.items() if k != "next_due_at"
        }
        c.state["comments"].append(c.comment(90, delivery["source_snapshot"]["body"]))
        await observe(c)
        assert (await c.collaboration.process(delivery["id"]))["api_observation"]["state"] == "observed"


async def test_missing_comment_does_not_override_periodic_provider_interval_after_requested_scan(poll_case):
    async with poll_case() as c:
        await c.ready()
        await c.consent()
        delivery, _, _ = await signed(c)
        await c.collaboration.process(delivery["id"])
        c.state["comments"] = [item for item in c.state["comments"] if item["id"] != 90]
        await observe(c)
        cursor = copy.deepcopy(c.db["settings"][c.repository].data["_github_poll_v1"]["cursor"])
        before = len(c.state["requests"])
        for _ in range(3):
            assert (await c.collaboration.process(delivery["id"]))["status"] == "received"
            assert not await c.service.step(c.repository)
        assert c.db["settings"][c.repository].data["_github_poll_v1"]["cursor"] == cursor
        assert len(c.state["requests"]) == before


async def test_unknown_api_classifier_is_never_replayed_by_later_webhook(poll_case, monkeypatch):
    async with poll_case() as c:
        natural, initial = await prepared(c, monkeypatch)
        await natural.process(initial)
        delivery, _, _ = await signed(c)
        await c.collaboration.process(delivery["id"])
        await observe(c)
        operational = comment_receipts(c, 90)[0]
        calls = []

        async def unknown(*args, **kwargs):
            calls.append(True)
            raise RuntimeError("Controlled unknown provider outcome")

        monkeypatch.setattr("firefly_forge.scm_natural_tasks.classify", unknown)
        result = await c.collaboration.process(operational["id"])
        assert result["status"] == "needs_reconciliation"
        assert result["natural_turn"]["classifier_outcome"] == "unknown"
        repeated, _, _ = await signed(c)
        assert (await c.collaboration.process(repeated["id"]))["api_observation"]["state"] == "observed"
        assert await c.collaboration.process(operational["id"]) == result
        assert calls == [True]


async def test_bounded_delivery_id_has_bounded_separate_source_identity(poll_case):
    async with poll_case() as c:
        _, raw, headers = await signed(c)
        headers["x-github-delivery"] = "x" * 200
        result = await c.collaboration.receive("github", c.repository, raw, headers)
        row = c.db["inbox"][result["id"]]
        assert row.source_key.startswith("webhook:") and len(row.source_key) < 200
        assert row.delivery_id == "x" * 200 and result["api_observation"]["source_key"] == "comment:90"


async def test_worker_checks_persisted_schedule_each_second_without_provider_reads(poll_case, monkeypatch):
    import asyncio

    async with poll_case() as c:
        await c.ready()
        before = len(c.state["requests"])
        sleeps = []

        async def stop_after_sweep(delay):
            sleeps.append(delay)
            raise asyncio.CancelledError

        monkeypatch.setattr("firefly_forge.scm_polling.asyncio.sleep", stop_after_sweep)
        with pytest.raises(asyncio.CancelledError):
            await c.service._poll()
        assert sleeps == [1] and len(c.state["requests"]) == before


async def test_worker_pages_more_than_one_hundred_repositories_without_global_ceiling(poll_case, monkeypatch):
    import asyncio
    from uuid import UUID

    async with poll_case() as c:
        rows = [SimpleNamespace(repository_id=UUID(int=i)) for i in range(1, 206)]
        pages, visited, sleeps = [], [], []
        async with c.store._unit() as session:
            session_type = type(session)

        async def page(self, statement):
            params = statement.compile().params
            boundary = next((value for value in params.values() if isinstance(value, UUID)), UUID(int=0))
            limit = statement._limit_clause.value
            selected = [row for row in rows if row.repository_id > boundary][:limit]
            pages.append((boundary.int, limit, len(selected)))
            return selected

        async def step(repository):
            visited.append(repository)
            return False

        async def stop_after_sweep(delay):
            sleeps.append(delay)
            if delay:
                raise asyncio.CancelledError

        monkeypatch.setattr(session_type, "scalars", page)
        monkeypatch.setattr(c.service, "step", step)
        monkeypatch.setattr("firefly_forge.scm_polling.asyncio.sleep", stop_after_sweep)
        with pytest.raises(asyncio.CancelledError):
            await c.service._poll()
        assert visited == [row.repository_id.hex for row in rows]
        assert pages == [(0, 100, 100), (100, 100, 100), (200, 100, 5)]
        assert sleeps[-1] == 1 and not c.state["requests"]
