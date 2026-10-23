# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Prepared real PostgreSQL custody controls with recorded provider HTTP only."""

import asyncio
import copy
from uuid import UUID

import pytest
from scm_polling_native_fixtures import native_poll as native_poll
from test_native_question_publication_native import prepare

from firefly_forge.native_question_publications import DEFERRED, INTENT, RECEIPT, NativeQuestionPublications
from firefly_forge.scm_provider_backoff import ProviderRequestDeferred


@pytest.mark.parametrize("revoke", [False, True])
def test_native_definite_nondispatch_retains_one_intent_across_restart(native_poll, monkeypatch, revoke):
    case = native_poll
    env = prepare(case)

    async def run():
        try:
            record = await env.producer.produce(case.run["id"])
            request = case.scm._request

            async def hold(method, url, **kwargs):
                if method == "POST" and url.endswith("/issues/3/comments"):
                    raise ProviderRequestDeferred("Public local cooldown fixture", retry_at=100.0)
                return await request(method, url, **kwargs)

            monkeypatch.setattr(case.scm, "_request", hold)
            await env.producer.publish(record["id"])
            saved = await case.collaboration.receipt(record["id"])
            assert saved["feedback_status"] == "pending"
            assert saved["feedback_deferred"]["outcome"] == "not_dispatched"
            assert not case.state["posts"] and not env.witnesses
            intent = copy.deepcopy(saved["feedback_intent"])
            before = await case.journal.entries(case.run["id"])
            assert len([e for e in before if e["kind"] == INTENT]) == 1
            assert len([e for e in before if e["kind"] == DEFERRED]) == 1
            assert not [e for e in before if e["kind"] == RECEIPT]
            if revoke:
                async with case.store._unit():
                    user = await case.service.users.find_by_id(case.mapped.id)
                    user.enabled = False
                    await case.service.users.save(user)

            async def forbidden(*args, **kwargs):
                pytest.fail("Restarted deferred publication must retain its original model receipt")

            monkeypatch.setattr(case.collaboration.communication_writer, "write", forbidden)
            monkeypatch.setattr(case.scm, "_request", request)
            restarted = NativeQuestionPublications(case.collaboration)
            if revoke:
                await restarted.publish(record["id"])
            else:
                await asyncio.gather(restarted.publish(record["id"]), restarted.publish(record["id"]))
            result = await case.collaboration.receipt(record["id"])
            assert result["feedback_intent"] == intent
            assert result["feedback_status"] == ("held" if revoke else "sent")
            assert len(case.state["posts"]) == int(not revoke)
            assert len(env.witnesses) == int(not revoke)
            await restarted.publish(record["id"])
            assert len(case.state["posts"]) == int(not revoke)
            # Read the retained journal through the fixture-owned repository: the
            # intentionally disabled principal must not regain read authority.
            async with case.store._unit():
                rows = await case.journal.records.find_for_run(UUID(case.run["id"]))
            assert all(len(e.idempotency_key) <= 64 for e in rows)
            events = [e.data for e in rows]
            assert len([e for e in events if e["kind"] == INTENT]) == 1
            assert len([e for e in events if e["kind"] == DEFERRED]) == 1
            assert len([e for e in events if e["kind"] == RECEIPT]) == int(not revoke)
            assert await case.collaboration.receipt(env.original["id"]) == env.original
        finally:
            await env.client.aclose()

    case.http.portal.call(run)
