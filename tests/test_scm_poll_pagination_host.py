# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Canonical GitHub pagination remains bound to the verified repository and query."""

import copy

import httpx
import pytest
from test_scm_polling import poll_case  # noqa: F401

from firefly_forge.scm_polling import SCMPolling


def response(path, target, query="sort=created&direction=asc&per_page=50&page=1"):
    return httpx.Response(
        200,
        headers={"Link": f'<{target}>; rel="next"'},
        request=httpx.Request("GET", "https://api.github.com" + path + "?" + query),
    )


@pytest.mark.parametrize("resource", ["issues", "issues/comments"])
@pytest.mark.parametrize("prefix", ["", "/api/v3"])
def test_canonical_next_page_matches_only_verified_repository(resource, prefix):
    path = prefix + "/repos/team/product/" + resource
    query = "sort=created&direction=asc&per_page=50&page=2"
    target = "https://api.github.com" + prefix + "/repositories/42/" + resource + "?" + query
    assert SCMPolling._next_page(response(path, target), 1, "42") == 2


@pytest.mark.parametrize(
    "target",
    [
        "https://api.github.com/repositories/43/issues/comments?sort=created&direction=asc&per_page=50&page=2",
        "https://foreign.invalid/repositories/42/issues/comments?sort=created&direction=asc&per_page=50&page=2",
        "http://api.github.com/repositories/42/issues/comments?sort=created&direction=asc&per_page=50&page=2",
        "https://user@api.github.com/repositories/42/issues/comments?sort=created&direction=asc&per_page=50&page=2",
        "https://api.github.com/repositories/42/issues?sort=created&direction=asc&per_page=50&page=2",
        "https://api.github.com/repositories/042/issues/comments?sort=created&direction=asc&per_page=50&page=2",
        "https://api.github.com/repositories/42/issues/comments?sort=created&direction=desc&per_page=50&page=2",
        "https://api.github.com/repositories/42/issues/comments?sort=created&direction=asc&per_page=50&page=3",
        "https://api.github.com/repositories/42/issues/comments?sort=created&direction=asc&per_page=50&page=2&page=2",
        "https://api.github.com/repositories/42/issues/comments?sort=created&direction=asc&per_page=50&page=2&extra=",
        "https://api.github.com/repositories/42/issues/comments?sort=created&direction=asc&per_page=50&page=2#fragment",
        "https://api.github.com/api/v3/repositories/42/issues/comments?sort=created&direction=asc&per_page=50&page=2",
    ],
)
def test_canonical_next_page_refuses_scope_or_query_changes(target):
    with pytest.raises(ValueError, match="pagination"):
        SCMPolling._next_page(response("/repos/team/product/issues/comments", target), 1, "42")


def test_original_path_duplicate_query_and_page_cap_remain_exact():
    path = "/repos/team/product/issues/comments"
    original = "sort=created&sort=created&direction=asc&per_page=50&page=1"
    target = "https://api.github.com" + path + "?" + original.replace("page=1", "page=2")
    assert SCMPolling._next_page(response(path, target, original), 1, "42") == 2
    with pytest.raises(ValueError, match="pagination"):
        SCMPolling._next_page(response(path, target, original.replace("sort=created&", "", 1)), 1, "42")
    with pytest.raises(ValueError, match="pagination"):
        SCMPolling._next_page(response(path, target.replace("page=2", "page=21")), 20, "42")


async def test_polling_commits_second_page_without_rebaselining(poll_case, monkeypatch):  # noqa: F811
    async with poll_case() as c:
        await c.ready()
        before = copy.deepcopy(c.db["settings"][c.repository].data["_github_poll_v1"])
        c.state["clock"] += 3600
        original_read = c.scm.github_poll_read
        pages = []

        async def read(repository, resource, *, page=1, value=""):
            if resource == "comments":
                pages.append(page)
                c.state["comments"] = [c.comment(n) for n in range(1, 51)] if page == 1 else [c.comment(51)]
                c.state["comment_headers"] = (
                    {
                        "Link": "<https://api.github.com/repositories/42/issues/comments"
                        '?sort=created&direction=asc&per_page=50&page=2>; rel="next"'
                    }
                    if page == 1
                    else {}
                )
            return await original_read(repository, resource, page=page, value=value)

        monkeypatch.setattr(c.scm, "github_poll_read", read)
        for _ in range(3):
            await c.service.step(c.repository)
        after = c.db["settings"][c.repository].data["_github_poll_v1"]
        assert pages == [1, 2]
        assert after["cursor"]["ready"] is True
        assert after["cursor"]["sequence"] == before["cursor"]["sequence"] + 1
        assert after["configuration"] == before["configuration"] and after["heads"] == before["heads"]
        row = next(r for r in c.db["inbox"].values() if r.source_key == "comment:51")
        assert row.data["reason"] == "not_an_enabled_poll_command"
        assert not c.db["journal"] and not c.state["posts"]
