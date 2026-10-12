# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Git workspace operations and replaceable pull-request provider adapters."""

from __future__ import annotations

import base64
import os
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from firefly_forge.process import checked


async def clone(repository: dict[str, Any], destination: Path, branch: str) -> str:
    destination.parent.mkdir(parents=True, exist_ok=True)
    await checked(
        [
            "git",
            "clone",
            "--no-hardlinks",
            "--single-branch",
            "--branch",
            repository["default_branch"],
            "--",
            repository["url"],
            str(destination),
        ],
        destination.parent,
    )
    await checked(["git", "checkout", "-b", branch], destination)
    return await checked(["git", "rev-parse", "HEAD"], destination)


def coordinates(url: str) -> tuple[str, str, str]:
    if url.startswith("git@"):
        host, path = url[4:].split(":", 1)
        return "https", host, path.removesuffix(".git").strip("/")
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    if parsed.scheme == "https" and parsed.port:
        host += f":{parsed.port}"
    return "https", host, parsed.path.strip("/").removesuffix(".git")


class PullRequests:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client

    async def create(self, repository: dict[str, Any], branch: str, title: str, body: str) -> str:
        provider = repository["provider"]
        token = os.environ.get(f"FORGE_{provider.upper()}_TOKEN", "")
        if provider == "git":
            raise ValueError("Generic Git supports branches and patches; select a provider to create pull requests")
        if not token:
            raise ValueError(f"Configure FORGE_{provider.upper()}_TOKEN to publish a pull request")
        scheme, host, path = coordinates(repository["url"])
        base = f"{scheme}://{host}"
        target = repository["default_branch"]
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        payload: dict[str, Any]
        if provider == "github":
            api = os.environ.get(
                "FORGE_GITHUB_API", "https://api.github.com" if host == "github.com" else f"{base}/api/v3"
            )
            endpoint = f"{api.rstrip('/')}/repos/{path}/pulls"
            payload = {"head": branch, "base": target, "title": title[:200], "body": body, "draft": True}
        elif provider == "gitlab":
            api = os.environ.get("FORGE_GITLAB_API", f"{base}/api/v4")
            endpoint = f"{api.rstrip('/')}/projects/{quote(path, safe='')}/merge_requests"
            headers = {"PRIVATE-TOKEN": token}
            payload = {
                "source_branch": branch,
                "target_branch": target,
                "title": f"Draft: {title[:190]}",
                "description": body,
            }
        elif provider == "bitbucket":
            endpoint = f"https://api.bitbucket.org/2.0/repositories/{path}/pullrequests"
            payload = {
                "source": {"branch": {"name": branch}},
                "destination": {"branch": {"name": target}},
                "title": title[:200],
                "description": body,
                "close_source_branch": False,
            }
        elif provider == "gitea":
            api = os.environ.get("FORGE_GITEA_API", f"{base}/api/v1")
            endpoint = f"{api.rstrip('/')}/repos/{path}/pulls"
            headers = {"Authorization": f"token {token}"}
            payload = {"head": branch, "base": target, "title": title[:200], "body": body}
        elif provider == "azure":
            parts = path.split("/")
            if host == "dev.azure.com" and len(parts) == 4 and parts[2] == "_git":
                organization, project, _, repo = parts
            elif host.endswith(".visualstudio.com") and len(parts) == 3 and parts[1] == "_git":
                organization, project, repo = host.split(".")[0], parts[0], parts[2]
            else:
                raise ValueError("Azure pull requests require https://dev.azure.com/org/project/_git/repo")
            base = f"https://dev.azure.com/{organization}/{project}"
            endpoint = f"{base}/_apis/git/repositories/{quote(repo, safe='')}/pullrequests?api-version=7.1"
            headers = {"Authorization": "Basic " + base64.b64encode(f":{token}".encode()).decode()}
            payload = {
                "sourceRefName": f"refs/heads/{branch}",
                "targetRefName": f"refs/heads/{target}",
                "title": title[:200],
                "description": body,
                "isDraft": True,
            }
        else:
            raise ValueError(f"Unsupported pull-request provider: {provider}")
        response = await self.client.post(endpoint, headers=headers, json=payload, timeout=30)
        if response.status_code not in (200, 201):
            raise RuntimeError(f"{provider} rejected pull-request creation (HTTP {response.status_code})")
        result = response.json()
        if provider == "gitlab":
            return str(result["web_url"])
        if provider == "bitbucket":
            return str(result["links"]["html"]["href"])
        if provider == "azure":
            return f"{base}/_git/{repo}/pullrequest/{result['pullRequestId']}"
        return str(result["html_url"])
