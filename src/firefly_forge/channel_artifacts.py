# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Opt-in immutable data-reference branches through connected provider content APIs."""

from __future__ import annotations

import base64
import hashlib
import json
import re
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import quote

import httpx

from firefly_forge.scm_connections import SCMConnections

Data = dict[str, Any]
MAX_REFERENCE_BYTES = 10_000_000
BRAND_FILES = {"tokens.json", "logo.svg", "logo-dark.svg", "theme.css", "brand-guide.md", "FONT-LICENSE.txt"}


def reference_manifest(
    repository_id: str, digest: str, kind: str, sources: dict[str, str]
) -> tuple[Data, dict[str, str]]:
    if (
        not re.fullmatch(r"[a-f0-9]{32}", repository_id)
        or not re.fullmatch(r"[a-f0-9]{64}", digest)
        or kind not in {"brand", "design", "proposal"}
    ):
        raise ValueError("Invalid immutable reference identity")
    if (
        not sources
        or len(sources) > 210
        or sum(len(value.encode()) for value in sources.values()) > MAX_REFERENCE_BYTES
    ):
        raise ValueError("Reference files exceed their bounded size or count")
    files = {}
    for name, value in sources.items():
        if len(value.encode()) > 500_000:
            raise ValueError("Reference file exceeds 500 KB")
        valid = (
            name in BRAND_FILES
            if kind == "brand"
            else bool(re.fullmatch(r"docs/product/proposals/[a-f0-9]{32}\.(?:json|md)", name))
            if kind == "proposal"
            else bool(
                re.fullmatch(
                    r"docs/design/(?:reference-journey-[0-9]{2}-(?:desktop|mobile)-(?:light|dark)\.svg|"
                    r"interaction-contract\.json|layout-contract\.json|functional-contract\.json|processes\.md|grid\.svg|FONT-LICENSE\.txt)",
                    name,
                )
            )
        )
        if not valid:
            raise ValueError("Only generated brand, design and proposal reference paths may be published")
        path = (
            name
            if kind == "proposal"
            else "docs/forge-references/" + digest + "/" + kind + "/" + name.rsplit("/", 1)[-1]
        )
        files[path] = value
    manifest = {
        "version": 1,
        "repository_id": repository_id,
        "digest": digest,
        "kind": kind,
        "files": {path: hashlib.sha256(value.encode()).hexdigest() for path, value in sorted(files.items())},
    }
    files["docs/forge-references/" + digest + "/manifest.json"] = json.dumps(manifest, sort_keys=True, indent=2)
    return manifest, files


class ReferencePublisher:
    def __init__(self, scm: SCMConnections) -> None:
        self.scm = scm

    async def publish(
        self,
        repo: Data,
        digest: str,
        kind: str,
        sources: dict[str, str],
        *,
        intent: Callable[[Data], Awaitable[None]],
        guard: Callable[[], Awaitable[None]] | None = None,
        readonly: bool = False,
    ) -> Data:
        manifest, files = reference_manifest(repo["id"], digest, kind, sources)
        branch = "forge/references/" + digest
        token, api = await self.scm.reference_authentication(repo)
        provider = repo["connection_provider"]
        name = repo["name"]
        if provider in {"github", "bitbucket"} and not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", name):
            raise ValueError("Invalid connected repository path")
        base = api + (
            "/repos/" + name
            if provider == "github"
            else "/projects/" + quote(repo["remote_repository_id"], safe="")
            if provider == "gitlab"
            else "/repositories/" + name
        )
        headers = {"Authorization": "Bearer " + token, "Accept": "application/json"}

        async def request(method: str, path: str, *, missing: bool = False, **kwargs: Any) -> httpx.Response:
            if method != "GET" and guard:
                await guard()
            return await self.scm._request(
                method,
                base + path,
                headers=headers,
                allowed_statuses=(200, 201, 404) if missing else (200, 201),
                response_limit=1_000_000,
                **kwargs,
            )

        refpath = (
            "/git/ref/heads/" + quote(branch, safe="")
            if provider == "github"
            else "/repository/branches/" + quote(branch, safe="")
            if provider == "gitlab"
            else "/refs/branches/" + quote(branch, safe="")
        )
        existing = await request("GET", refpath, missing=True)
        if existing.status_code != 404:
            for path, content in files.items():
                if provider == "github":
                    response = await request("GET", "/contents/" + quote(path, safe="/"), params={"ref": branch})
                    remote = base64.b64decode(response.json()["content"], validate=False)
                elif provider == "gitlab":
                    response = await request(
                        "GET", "/repository/files/" + quote(path, safe="") + "/raw", params={"ref": branch}
                    )
                    remote = response.content
                else:
                    response = await request("GET", "/src/" + quote(branch, safe="") + "/" + quote(path, safe="/"))
                    remote = response.content
                if (
                    len(remote) > MAX_REFERENCE_BYTES
                    or hashlib.sha256(remote).hexdigest() != hashlib.sha256(content.encode()).hexdigest()
                ):
                    raise ValueError("Existing reference branch differs; reconcile it without overwriting")
            return self._result(repo, branch, manifest, existing=True)
        if readonly:
            return {"branch": branch, "published": False, "digest": digest}
        default = repo["default_branch"]
        message = "Add " + ("unreviewed" if kind == "proposal" else "saved") + " " + kind + " references " + digest
        await intent({"branch": branch, "manifest": manifest, "operation": "create_immutable_references"})
        if provider == "github":
            source = (await request("GET", "/git/ref/heads/" + quote(default, safe=""))).json()["object"]["sha"]
            commit = (await request("GET", "/git/commits/" + source)).json()
            namespace = "docs/forge-references/" + digest
            if (
                await request("GET", "/contents/" + quote(namespace, safe="/"), params={"ref": source}, missing=True)
            ).status_code != 404:
                raise ValueError("Reference namespace already exists on the source branch")
            if kind == "proposal":
                for path in sources:
                    if (
                        await request("GET", "/contents/" + quote(path, safe="/"), params={"ref": source}, missing=True)
                    ).status_code != 404:
                        raise ValueError("Proposal reference already exists on source; do not overwrite it")
            tree = (
                await request(
                    "POST",
                    "/git/trees",
                    json={
                        "base_tree": commit["tree"]["sha"],
                        "tree": [
                            {"path": path, "mode": "100644", "type": "blob", "content": value}
                            for path, value in files.items()
                        ],
                    },
                )
            ).json()["sha"]
            created = (
                await request(
                    "POST",
                    "/git/commits",
                    json={
                        "message": message,
                        "tree": tree,
                        "parents": [source],
                    },
                )
            ).json()["sha"]
            await request("POST", "/git/refs", json={"ref": "refs/heads/" + branch, "sha": created})
        elif provider == "gitlab":
            source = (await request("GET", "/repository/branches/" + quote(default, safe=""))).json()["commit"]["id"]
            await request(
                "POST",
                "/repository/commits",
                json={
                    "branch": branch,
                    "start_sha": source,
                    "commit_message": message,
                    "actions": [
                        {"action": "create", "file_path": path, "content": value} for path, value in files.items()
                    ],
                },
            )
        else:
            source = (await request("GET", "/refs/branches/" + quote(default, safe=""))).json()["target"]["hash"]
            namespace = "docs/forge-references/" + digest
            if (await request("GET", "/src/" + source + "/" + namespace, missing=True)).status_code != 404:
                raise ValueError("Reference namespace already exists on the source branch")
            if kind == "proposal":
                for path in sources:
                    if (await request("GET", "/src/" + source + "/" + path, missing=True)).status_code != 404:
                        raise ValueError("Proposal reference already exists on source; do not overwrite it")
            await request("POST", "/refs/branches", json={"name": branch, "target": {"hash": source}})
            await request(
                "POST",
                "/src",
                data={"branch": branch, "parents": source, "message": message},
                files={
                    path: (path.rsplit("/", 1)[-1], value.encode(), "application/octet-stream")
                    for path, value in files.items()
                },
            )
        return self._result(repo, branch, manifest, existing=False)

    def _result(self, repo: Data, branch: str, manifest: Data, *, existing: bool) -> Data:
        provider = repo["connection_provider"]
        suffix = "/tree/" if provider == "github" else "/-/tree/" if provider == "gitlab" else "/src/"
        return {
            "published": True,
            "branch": branch,
            "digest": manifest["digest"],
            "existing": existing,
            "url": self.scm._host(provider) + "/" + repo["name"] + suffix + quote(branch, safe=""),
            "manifest": manifest,
        }
