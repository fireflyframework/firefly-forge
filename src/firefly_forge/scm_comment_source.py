# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Exact GitHub comment snapshots shared by verified ingress and fresh API reads."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

Data = dict[str, Any]


def github_comment_source(comment: Data, repository: Data, api: str) -> Data:
    if not isinstance(comment, dict) or not isinstance(comment.get("user"), dict):
        raise ValueError("The polled comment author is unavailable")
    user = comment["user"]
    number, actor, body = comment.get("id"), user.get("id"), comment.get("body")
    parsed = urlsplit(str(comment.get("issue_url", "")))
    expected = urlsplit(api).path + "/repos/" + repository["name"] + "/issues/"
    if (
        type(number) is not int
        or number <= 0
        or type(actor) is not int
        or actor <= 0
        or not isinstance(body, str)
        or len(body.encode()) > 100000
        or not parsed.path.startswith(expected)
        or not parsed.path.removeprefix(expected).isdecimal()
        or parsed.query
        or parsed.fragment
        or parsed.username
        or comment.get("issue_url")
        != api + "/repos/" + repository["name"] + "/issues/" + parsed.path.removeprefix(expected)
    ):
        raise ValueError("The polled comment identity or body is invalid")
    for name in ("created_at", "updated_at"):
        value = comment.get(name)
        if (
            not isinstance(value, str)
            or len(value) > 50
            or datetime.fromisoformat(value.replace("Z", "+00:00")).tzinfo is None
        ):
            raise ValueError("The provider comment timestamp is invalid")
    return dict(
        comment_id=str(number),
        author_id=str(actor),
        author_login=str(user.get("login", "")),
        author_type=user.get("type"),
        body=body,
        issue_number=int(parsed.path.removeprefix(expected)),
        created_at=str(comment.get("created_at", "")),
        updated_at=str(comment.get("updated_at", "")),
    )
