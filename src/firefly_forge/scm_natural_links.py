# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Internal delivery hrefs from already verified native status context, never model authority."""

from __future__ import annotations

import re
from typing import Any, Literal
from urllib.parse import urlsplit, urlunsplit

from pydantic import Field, model_validator

from firefly_forge.collaboration_feedback import _link
from firefly_forge.scm_authority_models import Digest, NativeID, StrictContract
from firefly_forge.scm_natural_models import SCOPE_FIELDS

Data = dict[str, Any]


class NativeDeliveryLinkV1(StrictContract):
    version: Literal[1] = 1
    kind: Literal["pull_request", "commit"]
    href: str = Field(min_length=1, max_length=600)
    run_id: NativeID
    repository_id: NativeID
    project_id: NativeID
    checkpoint_revision: str = Field(pattern=r"^(?:[a-f0-9]{40}|[a-f0-9]{64})$")
    checkpoint_digest: Digest

    @model_validator(mode="after")
    def canonical_delivery_path(self) -> NativeDeliveryLinkV1:
        parsed = urlsplit(self.href)
        suffix = r"pull/[1-9][0-9]{0,19}" if self.kind == "pull_request" else "commit/" + self.checkpoint_revision
        if (
            not self.href.isascii()
            or parsed.scheme != "https"
            or not parsed.netloc
            or parsed.username
            or parsed.password
            or "?" in self.href
            or "#" in self.href
            or not re.fullmatch(r"/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/" + suffix, parsed.path)
            or re.search(r"[\s<>\\\[\](){}]", self.href)
        ):
            raise ValueError("A native delivery href must have its exact canonical HTTPS SCM path")
        return self


def delivery_links(data: Data, repository: Data | None, host: str) -> tuple[NativeDeliveryLinkV1, ...]:
    # This producer consumes server-owned context after native feedback authority checks.
    # DTO construction alone does not verify a journal or confer a project read grant.
    if (
        repository is None
        or data.get("provider") != "github"
        or (repository.get("connection_provider") or repository.get("provider")) != "github"
        or urlsplit(host).scheme != "https"
    ):
        return ()
    turn = data.get("natural_turn") or {}
    context = turn.get("context") or {}
    scope = context.get("scope") or {}
    candidates = context.get("candidates") or []
    if (
        (turn.get("decision") or {}).get("intent") != "status"
        or set(scope) != set(SCOPE_FIELDS)
        or any(scope.get(key) != data.get(key) for key in ("repository_id", "actor", "issue"))
        or scope.get("repository_id") != repository.get("id")
        or not isinstance(candidates, list)
        or len(candidates) > 200
    ):
        return ()
    links: dict[str, NativeDeliveryLinkV1] = {}
    for row in candidates:
        if not isinstance(row, dict) or any(row.get(key) != scope.get(key) for key in SCOPE_FIELDS):
            continue
        delivery = row.get("delivery") or {}
        checkpoint, remote = delivery.get("checkpoint") or {}, delivery.get("remote") or {}
        if (
            delivery.get("evidence") != "verified_native_journal"
            or delivery.get("product_acceptance") != "not_established_by_checkpoint"
            or remote.get("status") not in {"draft", "ready"}
        ):
            continue
        review = _link(remote.get("pr_url"), host, repository)
        if not review:
            continue
        parsed = urlsplit(review)
        if not re.fullmatch(r"/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/pull/[1-9][0-9]{0,19}", parsed.path):
            continue
        try:
            shared = {
                "run_id": row.get("run_id"),
                "repository_id": scope.get("repository_id"),
                "project_id": scope.get("project_id"),
                "checkpoint_revision": checkpoint.get("revision"),
                "checkpoint_digest": checkpoint.get("checkpoint_digest"),
            }
            pull = NativeDeliveryLinkV1.model_validate({"kind": "pull_request", "href": review, **shared})
            commit = NativeDeliveryLinkV1.model_validate(
                {
                    "kind": "commit",
                    "href": urlunsplit(
                        (
                            "https",
                            parsed.netloc,
                            parsed.path.rsplit("/pull/", 1)[0] + "/commit/" + pull.checkpoint_revision,
                            "",
                            "",
                        )
                    ),
                    **shared,
                }
            )
        except (TypeError, ValueError):
            continue
        for link in (pull, commit):
            links.setdefault(link.href, link)
    return tuple(links.values())
