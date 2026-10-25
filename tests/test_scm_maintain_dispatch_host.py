# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Provider contention cannot turn fresh maintain consent into stale write authority."""

import asyncio
import copy

import pytest
from test_scm_maintain_ticket_host import poll_case as poll_case
from test_scm_maintain_ticket_host import prepared_binding
from test_scm_provider_contention_host import still_waiting

from firefly_forge.scm_maintain_tickets import MaintainNotificationNotDispatched
from firefly_forge.scm_provider_backoff import ProviderBackoff


def scope_lock(case):
    values = {**case.scm._definition("github")["defaults"], **case.scm._record("github")["values"]}
    return ProviderBackoff(
        case.scm.private.directory.parent,
        [
            "github",
            case.scm._api("github"),
            values.get("app_id", ""),
            values.get("installation_id", ""),
            values.get("workspace", ""),
        ],
    )


@pytest.mark.parametrize("change", ["revoke", "head", "expire"])
async def test_provider_wait_rechecks_current_native_consent_before_any_http(poll_case, monkeypatch, change):
    async with poll_case() as case:
        service, request = await prepared_binding(case, monkeypatch)
        kept = await service.configure(case.project, case.repository, request)
        anchor = await case.collaboration.receipt(kept["outbox_id"])
        lock = scope_lock(case)
        await lock.lock.acquire()
        previous = len(case.state["requests"])

        async def local_admission():
            assert case.state["depth"] == 0
            await service.authorize_feedback(anchor)
            assert case.state["depth"] == 0

        task = asyncio.create_task(
            case.scm._request(
                "POST",
                "https://api.github.com/repos/team/product/issues/3/comments",
                json={"body": "not dispatched"},
                before_dispatch=local_admission,
            )
        )
        try:
            await still_waiting(task)
            if change == "revoke":
                case.db["users"][case.user_id].enabled = False
            elif change == "head":
                state = case.db["settings"][case.repository].data["_github_poll_v1"]
                state["heads"][kept["polling_scope"]["binding_key"]]["digest"] = "f" * 64
            else:
                import firefly_forge.scm_maintain_tickets as module

                monkeypatch.setattr(module, "clock_ms", lambda: kept["expires_at_ms"])
        finally:
            await lock.lock.release()
        with pytest.raises(MaintainNotificationNotDispatched):
            await task
        assert len(case.state["requests"]) == previous


async def test_predispatch_refusal_is_held_and_unknown_post_is_never_replayed(poll_case, monkeypatch):
    async with poll_case() as case:
        service, request = await prepared_binding(case, monkeypatch)
        kept = await service.configure(case.project, case.repository, request)
        original = case.scm._request

        async def revoke_before_wait(method, url, **kwargs):
            if method == "POST" and kwargs.get("before_dispatch"):
                case.db["users"][case.user_id].enabled = False
            return await original(method, url, **kwargs)

        monkeypatch.setattr(case.scm, "_request", revoke_before_wait)
        before = len(case.state["posts"])
        await case.collaboration.feedback(kept["outbox_id"])
        refused = await case.collaboration.receipt(kept["outbox_id"])
        assert refused["feedback_status"] == "held" and refused["reason"] == "native_maintain_authority_changed"
        assert len(case.state["posts"]) == before

    async with poll_case() as case:
        service, request = await prepared_binding(case, monkeypatch)
        kept = await service.configure(case.project, case.repository, request)
        case.state["lost_reply"] = True
        before = len(case.state["posts"])
        await case.collaboration.feedback(kept["outbox_id"])
        unknown = copy.deepcopy(await case.collaboration.receipt(kept["outbox_id"]))
        assert unknown["feedback_status"] == "needs_reconciliation"
        await case.collaboration.feedback(kept["outbox_id"])
        assert await case.collaboration.receipt(kept["outbox_id"]) == unknown
        assert len(case.state["posts"]) == before + 1


async def test_cancelled_waiter_does_not_unlock_another_provider_owner(poll_case, monkeypatch):
    async with poll_case() as case:
        lock = scope_lock(case)
        await lock.lock.acquire()
        calls = []

        async def refuse():
            calls.append(True)
            raise MaintainNotificationNotDispatched("Controlled local refusal")

        task = asyncio.create_task(
            case.scm._request(
                "POST", "https://api.github.com/repos/team/product/issues/3/comments", before_dispatch=refuse
            )
        )
        try:
            await still_waiting(task)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            other = asyncio.create_task(
                case.scm._request(
                    "POST", "https://api.github.com/repos/team/product/issues/3/comments", before_dispatch=refuse
                )
            )
            await still_waiting(other)
            assert not calls
        finally:
            await lock.lock.release()
        with pytest.raises(MaintainNotificationNotDispatched):
            await other
        assert calls == [True] and not case.state["posts"]


async def test_connection_configuration_is_rechecked_after_awaited_local_admission(poll_case):
    async with poll_case() as case:
        entered, release = asyncio.Event(), asyncio.Event()
        before = len(case.state["requests"])

        async def admission():
            assert case.state["depth"] == 0
            entered.set()
            await release.wait()

        task = asyncio.create_task(
            case.scm._request(
                "POST", "https://api.github.com/repos/team/product/issues/3/comments", before_dispatch=admission
            )
        )
        await entered.wait()
        data = case.scm.private._read(case.scm.path)
        data["github"]["revision"] = "changed-during-local-admission"
        case.scm.private._write(case.scm.path, data)
        release.set()
        with pytest.raises(ValueError, match="settings changed during provider admission"):
            await task
        assert len(case.state["requests"]) == before


async def test_connection_change_during_local_check_is_a_definitive_hold_not_an_unknown_post(poll_case, monkeypatch):
    async with poll_case() as case:
        service, request = await prepared_binding(case, monkeypatch)
        kept = await service.configure(case.project, case.repository, request)
        original = case.scm._request

        async def change_after_admission(method, url, **kwargs):
            callback = kwargs.get("before_dispatch")
            if method == "POST" and callback:

                async def admit():
                    await callback()
                    data = case.scm.private._read(case.scm.path)
                    data["github"]["revision"] = "changed-after-local-check"
                    case.scm.private._write(case.scm.path, data)

                kwargs["before_dispatch"] = admit
            return await original(method, url, **kwargs)

        monkeypatch.setattr(case.scm, "_request", change_after_admission)
        before = len(case.state["posts"])
        await case.collaboration.feedback(kept["outbox_id"])
        refused = await case.collaboration.receipt(kept["outbox_id"])
        assert refused["feedback_status"] == "held" and refused["reason"] == "native_maintain_authority_changed"
        assert len(case.state["posts"]) == before


async def test_changed_communication_route_at_dispatch_is_held_without_http(poll_case, monkeypatch):
    async with poll_case() as case:
        service, request = await prepared_binding(case, monkeypatch)
        kept = await service.configure(case.project, case.repository, request)
        original_request = case.scm._request
        original_assert = case.collaboration.communication_writer.assert_current
        at_dispatch = False

        def current(value):
            if at_dispatch:
                raise ValueError("Controlled communication route changed")
            return original_assert(value)

        async def request(method, url, **kwargs):
            nonlocal at_dispatch
            callback = kwargs.get("before_dispatch")
            if method == "POST" and callback:

                async def admit():
                    nonlocal at_dispatch
                    at_dispatch = True
                    await callback()

                kwargs["before_dispatch"] = admit
            return await original_request(method, url, **kwargs)

        monkeypatch.setattr(case.scm, "_request", request)
        monkeypatch.setattr(case.collaboration.communication_writer, "assert_current", current)
        before = len(case.state["posts"])
        await case.collaboration.feedback(kept["outbox_id"])
        refused = await case.collaboration.receipt(kept["outbox_id"])
        assert refused["feedback_status"] == "held" and refused["reason"] == "native_maintain_authority_changed"
        assert len(case.state["posts"]) == before


async def test_changed_child_facts_while_waiting_are_not_posted_as_the_old_draft(poll_case, monkeypatch):
    async with poll_case() as case:
        service, request = await prepared_binding(case, monkeypatch)
        kept = await service.configure(case.project, case.repository, request)
        original = case.scm._request

        async def changed(method, url, **kwargs):
            callback = kwargs.get("before_dispatch")
            if method == "POST" and callback:

                async def admit():
                    case.db["runs"][request.run_id].data["status"] = "failed"
                    await callback()

                kwargs["before_dispatch"] = admit
            return await original(method, url, **kwargs)

        monkeypatch.setattr(case.scm, "_request", changed)
        before = len(case.state["posts"])
        await case.collaboration.feedback(kept["outbox_id"])
        refused = await case.collaboration.receipt(kept["outbox_id"])
        assert refused["feedback_status"] == "held" and refused["reason"] == "native_maintain_authority_changed"
        assert len(case.state["posts"]) == before
