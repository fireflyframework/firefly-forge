# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Explicit provider draft lifecycles; no credentials or provider are inferred."""

from __future__ import annotations

import hashlib
import re
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from firefly_forge.scm import coordinates

Data = dict[str, Any]


def draft_capability(provider: str) -> bool:
    return provider in {"github", "gitlab"}


class DraftPullRequests:
    def __init__(
        self, client: httpx.AsyncClient, repository: Data, authentication: tuple[str, str], *, guard: Any = None
    ) -> None:
        self.client, self.repository = client, repository
        self.guard = guard
        self.provider = repository["provider"]
        if not draft_capability(self.provider):
            raise ValueError("This provider has no qualified draft update/ready adapter; use local checkpoints")
        if not authentication or not authentication[0]:
            raise ValueError("Intermediate draft delivery requires explicit scoped connection credentials")
        self.api = authentication[1].rstrip("/")
        if urlsplit(self.api).scheme != "https" or urlsplit(self.api).username or urlsplit(self.api).password:
            raise ValueError("The authorized provider API must use HTTPS without inline credentials")
        scheme, host, self.path = coordinates(repository["url"])
        self.base = f"{scheme}://{host}/{self.path}"
        self.headers = {"Authorization": "Bearer " + authentication[0], "Accept": "application/json"}
        self.endpoint = (
            f"{self.api}/repos/{self.path}/pulls"
            if self.provider == "github"
            else f"{self.api}/projects/{quote(self.path, safe='')}/merge_requests"
        )

    async def _request(self, method: str, url: str, **kwargs: Any) -> Any:
        if self.guard is not None:
            await self.guard()
        result = await self.client.request(method, url, headers=self.headers, timeout=30, **kwargs)
        if result.status_code not in {200, 201}:
            raise ValueError(f"Draft lifecycle response is unconfirmed (HTTP {result.status_code})")
        if len(result.content) > 1048576:
            raise ValueError("Draft lifecycle metadata exceeds its bounded envelope")
        return result.json()

    def _validate(self, result: Any, branch: str, revision: str, *, draft: bool = True) -> Data:
        if not isinstance(result, dict):
            raise ValueError("Draft reference metadata is not a typed object")
        number = result.get("number" if self.provider == "github" else "iid")
        if type(number) is not int or number <= 0:
            raise ValueError("Draft reference lacks a positive provider identity")
        url = f"{self.base}/pull/{number}" if self.provider == "github" else f"{self.base}/-/merge_requests/{number}"
        if result.get("html_url" if self.provider == "github" else "web_url") != url:
            raise ValueError("Draft reference belongs to a different repository or URL")
        if result.get("draft") is not draft or result.get("state") != (
            "open" if self.provider == "github" else "opened"
        ):
            raise ValueError("The selected reference is not an open draft at the expected readiness")
        if self.provider == "github":
            head, base = result.get("head") or {}, result.get("base") or {}
            if (
                head.get("ref") != branch
                or head.get("sha") != revision
                or head.get("repo", {}).get("full_name") != self.path
                or base.get("ref") != self.repository["default_branch"]
                or base.get("repo", {}).get("full_name") != self.path
                or not isinstance(result.get("node_id"), str)
                or not result["node_id"]
            ):
                raise ValueError("Draft source, target, repository or revision differs from the admitted checkpoint")
        elif (
            result.get("source_branch") != branch
            or result.get("target_branch") != self.repository["default_branch"]
            or result.get("sha") != revision
            or type(result.get("source_project_id")) is not int
            or result["source_project_id"] <= 0
            or type(result.get("target_project_id")) is not int
            or result["target_project_id"] <= 0
            or result["source_project_id"] != result.get("target_project_id")
            or (
                self.repository.get("remote_repository_id") is not None
                and str(result["target_project_id"]) != str(self.repository["remote_repository_id"])
            )
        ):
            raise ValueError("Draft merge request source, target or revision differs from the checkpoint")
        body = result.get("body" if self.provider == "github" else "description")
        return {
            "url": url,
            "number": number,
            "node_id": result.get("node_id"),
            "revision": revision,
            "body_sha256": hashlib.sha256(body.encode()).hexdigest() if isinstance(body, str) else None,
        }

    def _number(self, url: str) -> int:
        suffix = r"/pull/([1-9][0-9]*)" if self.provider == "github" else r"/-/merge_requests/([1-9][0-9]*)"
        match = re.fullmatch(re.escape(self.base) + suffix, url)
        if match is None:
            raise ValueError("Retained draft reference is outside this exact repository")
        return int(match[1])

    async def read(self, url: str, branch: str, revision: str, *, draft: bool = True) -> Data:
        number = self._number(url)
        result = self._validate(await self._request("GET", f"{self.endpoint}/{number}"), branch, revision, draft=draft)
        if result["number"] != number or result["url"] != url:
            raise ValueError("Draft response substituted another reference identity")
        return result

    @staticmethod
    def _same_reference(previous: Data, current: Data) -> None:
        if any(previous.get(key) != current.get(key) for key in ("url", "number", "node_id")):
            raise ValueError("The retained provider reference identity changed")

    async def find(
        self,
        branch: str,
        revision: str,
        *,
        draft: bool = True,
        expected_title: str | None = None,
        expected_body_sha256: str | None = None,
    ) -> Data | None:
        params = (
            {
                "state": "open",
                "head": self.path.split("/")[0] + ":" + branch,
                "base": self.repository["default_branch"],
                "per_page": 2,
            }
            if self.provider == "github"
            else {
                "state": "opened",
                "source_branch": branch,
                "target_branch": self.repository["default_branch"],
                "per_page": 2,
            }
        )
        results = await self._request("GET", self.endpoint, params=params)
        if not isinstance(results, list) or len(results) > 1:
            raise ValueError("Draft discovery is ambiguous or contains multiple references")
        if not results:
            return None
        value = self._validate(results[0], branch, revision, draft=draft)
        title = expected_title if self.provider == "github" else "Draft: " + (expected_title or "")
        if (expected_title is not None and results[0].get("title") != title) or (
            expected_body_sha256 is not None and value["body_sha256"] != expected_body_sha256
        ):
            raise ValueError("The discovered draft differs from the exact retained title or body")
        return value

    async def create(self, branch: str, revision: str, title: str, body: str) -> Data:
        payload = (
            {"head": branch, "base": self.repository["default_branch"], "title": title, "body": body, "draft": True}
            if self.provider == "github"
            else {
                "source_branch": branch,
                "target_branch": self.repository["default_branch"],
                "title": "Draft: " + title,
                "description": body,
            }
        )
        return self._validate(await self._request("POST", self.endpoint, json=payload), branch, revision)

    async def update(self, reference: Data, branch: str, revision: str, title: str, body: str) -> Data:
        current = await self.read(reference["url"], branch, revision)
        self._same_reference(reference, current)
        if reference.get("body_sha256") is not None and current["body_sha256"] != reference["body_sha256"]:
            raise ValueError("The retained draft body changed outside this factory delivery")
        payload = (
            {"title": title, "body": body}
            if self.provider == "github"
            else {"title": "Draft: " + title, "description": body}
        )
        method = "PATCH" if self.provider == "github" else "PUT"
        result = self._validate(
            await self._request(method, f"{self.endpoint}/{reference['number']}", json=payload), branch, revision
        )
        self._same_reference(current, result)
        return result

    async def ready(self, reference: Data, branch: str, revision: str) -> Data:
        current = await self.read(reference["url"], branch, revision)
        self._same_reference(reference, current)
        if current["body_sha256"] != reference.get("body_sha256"):
            raise ValueError("Draft content changed before readiness promotion")
        if self.provider == "github":
            graph = (
                "https://api.github.com/graphql"
                if self.api == "https://api.github.com"
                else self.api.removesuffix("/v3") + "/graphql"
            )
            result = await self._request(
                "POST",
                graph,
                json={
                    "query": (
                        "mutation($id:ID!){markPullRequestReadyForReview(input:{pullRequestId:$id})"
                        "{pullRequest{id isDraft}}}"
                    ),
                    "variables": {"id": current["node_id"]},
                },
            )
            selected = ((result.get("data") or {}).get("markPullRequestReadyForReview") or {}).get("pullRequest") or {}
            if result.get("errors") or selected.get("id") != current["node_id"] or selected.get("isDraft") is not False:
                raise ValueError("Ready-for-review mutation has an unconfirmed outcome")
        else:
            detail = await self._request("GET", f"{self.endpoint}/{current['number']}")
            observed = self._validate(detail, branch, revision)
            self._same_reference(current, observed)
            if observed["body_sha256"] != current["body_sha256"]:
                raise ValueError("Draft content changed before readiness promotion")
            title = re.sub(r"^(?:Draft:|\[Draft\]|\(Draft\))\s*", "", str(detail.get("title", "")), flags=re.I)
            if not title:
                raise ValueError("Draft title is unavailable for readiness promotion")
            promoted = self._validate(
                await self._request("PUT", f"{self.endpoint}/{current['number']}", json={"title": title}),
                branch,
                revision,
                draft=False,
            )
            self._same_reference(current, promoted)
            if promoted["body_sha256"] != current["body_sha256"]:
                raise ValueError("Ready reference content has an unconfirmed outcome")
        result = await self.read(current["url"], branch, revision, draft=False)
        self._same_reference(current, result)
        if result["body_sha256"] != current["body_sha256"]:
            raise ValueError("Ready reference content has an unconfirmed outcome")
        return result
