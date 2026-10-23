# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Definite local nondispatch is distinct from uncertain provider effects."""

import asyncio
import copy

import httpx
import pytest
from test_native_question_publication_host import pending
from test_scm_delegated_feedback import RECEIPT, received
from test_scm_delegated_feedback import delegated_feedback as delegated_feedback
from test_scm_polling import poll_case as poll_case
from test_scm_provider_backoff_host import connection

from firefly_forge.native_question_publications import NativeQuestionPublications
from firefly_forge.scm_provider_backoff import ProviderBackoff, ProviderRequestDeferred


async def test_cancelled_http_releases_scope_for_a_new_owner(tmp_path):
    entered = asyncio.Event()
    calls = []

    async def handler(request):
        calls.append(request)
        if len(calls) == 1:
            entered.set()
            await asyncio.Event().wait()
        return httpx.Response(200)

    scm = connection(tmp_path, handler)
    first = asyncio.create_task(scm._request("GET", "https://api.github.com/installation/repositories"))
    await entered.wait()
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert (await scm._request("GET", "https://api.github.com/installation/repositories")).status_code == 200
    assert len(calls) == 2
    await scm._client.aclose()


async def test_cancellation_at_acquired_lock_handoff_does_not_strand_scope(tmp_path, monkeypatch):
    guard = ProviderBackoff(tmp_path, ["github", "public"])
    acquire = guard.lock.acquire

    async def cancelled_after_acquire():
        await acquire()
        raise asyncio.CancelledError()

    monkeypatch.setattr(guard.lock, "acquire", cancelled_after_acquire)
    with pytest.raises(asyncio.CancelledError):
        async with guard:
            pytest.fail("Cancelled acquisition must not dispatch")
    async with ProviderBackoff(tmp_path, ["github", "public"]):
        pass


async def test_local_feedback_defer_keeps_exact_draft_without_new_generation(delegated_feedback, monkeypatch):
    service, row, run, sent, state = delegated_feedback
    received(row, "status")
    remote = service._remote_write
    deferred = []

    async def hold(*args, **kwargs):
        deferred.append(1)
        raise ProviderRequestDeferred("Provider cooldown", retry_at=100.0)

    monkeypatch.setattr(service, "_remote_write", hold)
    await service.process(RECEIPT)
    retained = copy.deepcopy(row.data["feedback_intent"])
    assert row.data["feedback_status"] == "pending" and not sent
    assert row.data["feedback_deferred"]["intent_sha256"]
    monkeypatch.setattr(service, "_remote_write", remote)

    async def forbidden(*args, **kwargs):
        pytest.fail("A retained definite-nondispatch draft must not call a writer again")

    monkeypatch.setattr(service.communication_writer, "write", forbidden)
    await service.feedback(RECEIPT)
    assert row.data["feedback_status"] == "sent" and len(sent) == 1
    assert row.data["feedback_intent"] == retained
    assert not row.data.get("feedback_deferred")


@pytest.mark.parametrize("point", ["post", "after_draft_read", "token"])
async def test_native_question_defer_reuses_immutable_intent_and_current_authority(poll_case, monkeypatch, point):
    async with poll_case() as c:
        await pending(c, monkeypatch)
        producer = NativeQuestionPublications(c.collaboration)
        record = await producer.produce(c.run_id)
        request = c.scm._request
        authentication = c.scm.issue_authentication
        writer = c.collaboration.communication_writer.write
        generated = []

        async def draft(*args, **kwargs):
            result = await writer(*args, **kwargs)
            generated.append(1)
            return result

        monkeypatch.setattr(c.collaboration.communication_writer, "write", draft)

        async def hold(method, url, **kwargs):
            if point == "post" and method == "POST" and url.endswith("/issues/3/comments"):
                raise ProviderRequestDeferred("Provider cooldown", retry_at=100.0)
            if point == "after_draft_read" and generated and method == "GET":
                raise ProviderRequestDeferred("Provider cooldown", retry_at=100.0)
            return await request(method, url, **kwargs)

        monkeypatch.setattr(c.scm, "_request", hold)
        if point == "token":

            async def hold_token(*args, **kwargs):
                raise ProviderRequestDeferred("Provider cooldown", retry_at=100.0)

            monkeypatch.setattr(c.scm, "issue_authentication", hold_token)
        await producer.publish(record["id"])
        deferred = await c.collaboration.receipt(record["id"])
        assert deferred["feedback_status"] == "pending" and not c.state["posts"]
        retained = copy.deepcopy(deferred["feedback_intent"])
        assert generated == [1]
        monkeypatch.setattr(c.scm, "_request", request)
        monkeypatch.setattr(c.scm, "issue_authentication", authentication)

        async def forbidden(*args, **kwargs):
            pytest.fail("A retained native question must not regenerate its AI draft")

        monkeypatch.setattr(c.collaboration.communication_writer, "write", forbidden)
        await producer.publish(record["id"])
        sent = await c.collaboration.receipt(record["id"])
        assert sent["feedback_status"] == "sent" and len(c.state["posts"]) == 1
        assert sent["feedback_intent"] == retained


@pytest.mark.parametrize("change", ["facts", "route", "authority", "evidence"])
async def test_stale_deferred_feedback_is_held_without_replacement_draft(delegated_feedback, monkeypatch, change):
    service, row, run, sent, state = delegated_feedback
    received(row, "status")

    async def hold(*args, **kwargs):
        raise ProviderRequestDeferred("Provider cooldown", retry_at=100.0)

    monkeypatch.setattr(service, "_remote_write", hold)
    await service.process(RECEIPT)
    retained = copy.deepcopy(row.data["feedback_intent"])
    if change == "facts":
        run.update(status="running", active_task="implement", worker_request={})
    elif change == "route":

        def stale_route(*args):
            raise ValueError("The configured communication route changed")

        monkeypatch.setattr(service.communication_writer, "assert_current", stale_route)
    elif change == "authority":
        state["deny"] = True
    else:
        row.data["feedback_deferred"]["intent_sha256"] = "f" * 64

    async def forbidden(*args, **kwargs):
        pytest.fail("Stale intent must not dispatch or replace its draft")

    monkeypatch.setattr(service, "_remote_write", forbidden)
    monkeypatch.setattr(service.communication_writer, "write", forbidden)
    await service.feedback(RECEIPT)
    assert row.data["feedback_status"] == "held" and not sent
    assert row.data["feedback_intent"] == retained


async def test_actual_dispatched_write_refusal_remains_unknown_not_deferred(delegated_feedback, monkeypatch):
    service, row, run, sent, state = delegated_feedback
    received(row, "status")
    calls = []

    async def dispatched(*args, **kwargs):
        calls.append(1)
        raise ValueError("Provider rejected the application request (HTTP 403)")

    monkeypatch.setattr(service, "_remote_write", dispatched)
    await service.process(RECEIPT)
    assert row.data["feedback_status"] == "needs_reconciliation"
    assert not row.data.get("feedback_deferred")
    await service.feedback(RECEIPT)
    await service.sync_feedback()
    assert calls == [1] and not sent


async def test_future_local_hold_performs_no_authority_model_or_transport_work(delegated_feedback, monkeypatch):
    service, row, run, sent, state = delegated_feedback
    received(row, "status")

    async def hold(*args, **kwargs):
        raise ProviderRequestDeferred("Provider cooldown", retry_at=9999999999.0)

    monkeypatch.setattr(service, "_remote_write", hold)
    await service.process(RECEIPT)

    async def forbidden(*args, **kwargs):
        pytest.fail("A future local hold must wait without authentication, generation or HTTP")

    monkeypatch.setattr(service, "_feedback_authority", forbidden)
    monkeypatch.setattr(service.communication_writer, "write", forbidden)
    monkeypatch.setattr(service, "_remote_write", forbidden)
    await service.feedback(RECEIPT)
    assert row.data["feedback_status"] == "pending" and not sent


async def test_renewed_local_hold_during_revalidation_preserves_pending_intent(delegated_feedback, monkeypatch):
    service, row, run, sent, state = delegated_feedback
    received(row, "status")

    async def hold(*args, **kwargs):
        raise ProviderRequestDeferred("Provider cooldown", retry_at=100.0)

    monkeypatch.setattr(service, "_remote_write", hold)
    await service.process(RECEIPT)
    intent = copy.deepcopy(row.data["feedback_intent"])

    async def renewed_hold(*args, **kwargs):
        raise ProviderRequestDeferred("Renewed provider cooldown", retry_at=9999999999.0)

    monkeypatch.setattr(service, "_feedback_authority", renewed_hold)
    await service.feedback(RECEIPT)
    assert row.data["feedback_status"] == "pending" and not sent
    assert row.data["feedback_intent"] == intent
    assert row.data["feedback_deferred"]["retry_at"] == 9999999999.0


@pytest.mark.parametrize("change", ["route", "authority"])
async def test_deferred_final_prewrite_drift_holds_without_http(delegated_feedback, monkeypatch, change):
    service, row, run, sent, state = delegated_feedback
    received(row, "status")
    remote = service._remote_write

    async def hold(*args, **kwargs):
        raise ProviderRequestDeferred("Provider cooldown", retry_at=100.0)

    monkeypatch.setattr(service, "_remote_write", hold)
    await service.process(RECEIPT)
    retained = copy.deepcopy(row.data["feedback_intent"])
    current = service.communication_writer.assert_current
    calls = []

    def drift(*args):
        calls.append(1)
        current(*args)
        if change == "authority" and len(calls) == 1:
            state["deny"] = True
        if change == "route" and len(calls) == 2:
            raise ValueError("The route changed immediately before POST")

    monkeypatch.setattr(service.communication_writer, "assert_current", drift)
    monkeypatch.setattr(service, "_remote_write", remote)
    await service.feedback(RECEIPT)
    assert row.data["feedback_status"] == "held" and not sent
    assert row.data["feedback_intent"] == retained
