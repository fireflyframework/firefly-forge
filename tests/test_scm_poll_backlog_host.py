# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Drain pages fairly; metadata wakeups preserve provider cadence and backoff."""

import asyncio
import copy
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from test_scm_polling import poll_case  # noqa: F401

from firefly_forge.project_access import AccessDenied


class PollRest(BaseException):
    pass


def stop_at_rest(monkeypatch, *, rests=1):
    original = asyncio.sleep
    waits = []

    async def sleep(seconds):
        nonlocal rests
        waits.append(seconds)
        if seconds:
            rests -= 1
            if rests == 0:
                raise PollRest
        await original(0)

    monkeypatch.setattr(asyncio, "sleep", sleep)
    return waits


async def test_real_worker_reads_the_next_comment_page_before_its_normal_rest(poll_case, monkeypatch):  # noqa: F811
    async with poll_case() as c:
        await c.ready()
        c.state["clock"] += 3600
        original = c.scm.github_poll_read
        pages = []

        async def read(repository, resource, *, page=1, value=""):
            pages.append((resource, page))
            if resource == "comments":
                c.state["comments"] = [c.comment(n) for n in range(1, 51)] if page == 1 else [c.comment(51)]
                c.state["comment_headers"] = (
                    {
                        "Link": "<https://api.github.com/repositories/42/issues/comments"
                        '?sort=created&direction=asc&per_page=50&page=2>; rel="next"'
                    }
                    if page == 1
                    else {}
                )
            return await original(repository, resource, page=page, value=value)

        monkeypatch.setattr(c.scm, "github_poll_read", read)
        waits = stop_at_rest(monkeypatch)
        with pytest.raises(PollRest):
            await c.service._poll()
        assert pages == [("issues", 1), ("comments", 1), ("comments", 2)]
        assert waits[-1] == 1 and all(wait == 0 for wait in waits[:-1])
        cursor = c.db["settings"][c.repository].data["_github_poll_v1"]["cursor"]
        assert cursor["ready"] is True and cursor["next_due_at"] == c.state["clock"] + 60
        assert any(row.source_key == "comment:51" for row in c.db["inbox"].values())
        assert not c.state["posts"] and not c.db["journal"]


async def test_backlog_rounds_give_each_repository_one_turn_and_stop_retrying_failed_peers(
    poll_case,  # noqa: F811
    monkeypatch,
):
    async with poll_case() as c:
        await c.configure()
        second = uuid4().hex
        c.db["settings"][second] = SimpleNamespace(
            id=UUID(second),
            repository_id=UUID(second),
            data=copy.deepcopy(c.db["settings"][c.repository].data),
        )
        c.db["repositories"][second] = SimpleNamespace(id=UUID(second), data={})
        turns = []
        counts = {c.repository: 0, second: 0}

        async def step(repository):
            turns.append(repository)
            counts[repository] += 1
            if repository == second:
                raise AccessDenied("Controlled peer access loss")
            return counts[repository] < 3

        monkeypatch.setattr(c.service, "step", step)
        waits = stop_at_rest(monkeypatch)
        with pytest.raises(PollRest):
            await c.service._poll()
        assert turns == [c.repository, second, c.repository, c.repository]
        assert waits == [0, 0, 1]
        assert c.db["settings"][second].data["_github_poll_v1"]["cursor"]["last_error"] == "poll_unavailable"
        assert not c.state["posts"] and not c.db["journal"]


async def test_two_available_backlogs_are_interleaved_until_each_finishes(poll_case, monkeypatch):  # noqa: F811
    async with poll_case() as c:
        await c.configure()
        second = uuid4().hex
        c.db["settings"][second] = SimpleNamespace(id=UUID(second), repository_id=UUID(second), data={})
        turns = []
        counts = {c.repository: 0, second: 0}

        async def step(repository):
            turns.append(repository)
            counts[repository] += 1
            return counts[repository] < (3 if repository == c.repository else 2)

        monkeypatch.setattr(c.service, "step", step)
        waits = stop_at_rest(monkeypatch)
        with pytest.raises(PollRest):
            await c.service._poll()
        assert turns == [c.repository, second, c.repository, second, c.repository]
        assert waits == [0, 0, 1]


async def test_provider_backoff_preserves_cursor_and_has_no_immediate_worker_retry(poll_case, monkeypatch):  # noqa: F811
    async with poll_case() as c:
        await c.configure()
        before = copy.deepcopy(c.db["settings"][c.repository].data["_github_poll_v1"]["cursor"])
        configured_requests = len(c.state["requests"])
        c.state.update(read_status=429, read_headers={"Retry-After": "120"})
        waits = stop_at_rest(monkeypatch, rests=3)
        with pytest.raises(PollRest):
            await c.service._poll()
        cursor = c.db["settings"][c.repository].data["_github_poll_v1"]["cursor"]
        assert cursor["page"] == before["page"] and cursor["phase"] == before["phase"]
        assert cursor["retry_at"] == c.state["clock"] + 120 and waits == [1, 1, 1]
        assert c.state["requests"][configured_requests:].count(("GET", "/repos/team/product/issues")) == 1
        requests = len(c.state["requests"])
        assert await c.service.step(c.repository) is False
        assert len(c.state["requests"]) == requests
        assert not c.state["posts"] and not c.db["inbox"] and not c.db["journal"]


async def test_finished_scan_waits_for_the_configured_interval_without_provider_reads(poll_case, monkeypatch):  # noqa: F811
    async with poll_case() as c:
        await c.ready()
        before = copy.deepcopy(c.db["settings"][c.repository].data["_github_poll_v1"])
        requests = len(c.state["requests"])
        waits = stop_at_rest(monkeypatch, rests=3)
        with pytest.raises(PollRest):
            await c.service._poll()
        assert waits == [1, 1, 1] and len(c.state["requests"]) == requests
        assert c.db["settings"][c.repository].data["_github_poll_v1"] == before


async def test_backlog_yield_rechecks_ownership_before_another_provider_read(poll_case, monkeypatch):  # noqa: F811
    async with poll_case() as c:
        await c.configure()
        original = asyncio.sleep
        waits = []

        async def lose_ownership(seconds):
            waits.append(seconds)
            c.state["ownership_lost"] = True
            await original(0)

        monkeypatch.setattr(asyncio, "sleep", lose_ownership)
        with pytest.raises(RuntimeError, match="ownership loss"):
            await c.service._poll()
        assert waits == [0]
        cursor = c.db["settings"][c.repository].data["_github_poll_v1"]["cursor"]
        assert (cursor["phase"], cursor["page"]) == ("comments", 1)
        assert not c.db["inbox"] and not c.db["journal"] and not c.state["posts"]


async def test_backlog_yield_allows_cancellation_without_reading_the_next_page(poll_case, monkeypatch):  # noqa: F811
    async with poll_case() as c:
        await c.configure()
        waits = []

        async def cancel(seconds):
            waits.append(seconds)
            raise asyncio.CancelledError

        monkeypatch.setattr(asyncio, "sleep", cancel)
        with pytest.raises(asyncio.CancelledError):
            await c.service._poll()
        assert waits == [0]
        cursor = c.db["settings"][c.repository].data["_github_poll_v1"]["cursor"]
        assert (cursor["phase"], cursor["page"]) == ("comments", 1)
        assert not c.db["inbox"] and not c.db["journal"] and not c.state["posts"]
