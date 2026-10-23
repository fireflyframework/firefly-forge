# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Git workspace operations and replaceable pull-request provider adapters."""

from __future__ import annotations

import base64
import os
import re
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote, urlsplit

import httpx

from firefly_forge.configuration import redact_secrets
from firefly_forge.git_workspace import git_environment, trusted_git
from firefly_forge.process import checked as process_checked


async def checked(argv: list[str], cwd: Path, timeout: int = 300, *, env: dict[str, str] | None = None) -> str:
    return await process_checked(trusted_git(argv), cwd, timeout, env=git_environment(env))


@asynccontextmanager
async def git_credentials(credentials: dict[str, str] | None) -> AsyncIterator[dict[str, str] | None]:
    if not credentials:
        yield None
        return
    with tempfile.TemporaryDirectory(prefix="forge-git-auth-") as directory:
        helper = Path(directory) / "askpass"
        helper.write_text(
            "#!/usr/bin/env python3\nimport os,sys,re\nfrom urllib.parse import urlsplit\n"
            "match=re.search(r'https?://[^\\s\\']+',sys.argv[1])\n"
            "expected=urlsplit(os.environ['FORGE_GIT_ORIGIN'])\n"
            "actual=urlsplit(match.group(0)) if match else None\n"
            "if actual is None or (actual.scheme,actual.hostname,actual.port)!= "
            "(expected.scheme,expected.hostname,expected.port): sys.exit(1)\n"
            "print(os.environ['FORGE_GIT_USERNAME'] if 'username' in sys.argv[1].lower() "
            "else os.environ['FORGE_GIT_PASSWORD'])\n"
        )
        helper.chmod(0o700)
        yield {
            **credentials,
            "GIT_ASKPASS": str(helper),
            "GIT_CONFIG_COUNT": "2",
            "GIT_CONFIG_KEY_0": "credential.helper",
            "GIT_CONFIG_VALUE_0": "",
            "GIT_CONFIG_KEY_1": "http.followRedirects",
            "GIT_CONFIG_VALUE_1": "false",
        }


async def clone(
    repository: dict[str, Any],
    destination: Path,
    branch: str,
    *,
    env: dict[str, str] | None = None,
    revision: str | None = None,
) -> str:
    if revision is not None and not re.fullmatch(r"[a-f0-9]{40}|[a-f0-9]{64}", revision):
        raise ValueError("The frozen source base must be a Git commit")
    destination.parent.mkdir(parents=True, exist_ok=True)
    async with git_credentials(env) as credential_environment:
        try:
            await checked(
                [
                    "git",
                    "clone",
                    "--no-hardlinks",
                    "--no-checkout",
                    "--single-branch",
                    "--branch",
                    repository["default_branch"],
                    "--",
                    repository["url"],
                    str(destination),
                ],
                destination.parent,
                env=credential_environment,
            )
            if revision:
                try:
                    await checked(["git", "cat-file", "-e", revision + "^{commit}"], destination)
                except RuntimeError:
                    await checked(
                        ["git", "fetch", "--no-tags", "--", "origin", revision],
                        destination,
                        env=credential_environment,
                    )
        except RuntimeError as exc:
            raise RuntimeError(redact_secrets(str(exc), env or {})) from None
    await checked(["git", "checkout", "--force", "-b", branch, revision or "HEAD"], destination)
    return await checked(["git", "rev-parse", "HEAD"], destination)


async def source_revision(repository: dict[str, Any], *, env: dict[str, str] | None = None) -> str:
    reference = "refs/heads/" + repository["default_branch"]
    with tempfile.TemporaryDirectory(prefix="forge-source-ref-") as directory:
        async with git_credentials(env) as credential_environment:
            try:
                result = await checked(
                    ["git", "ls-remote", "--exit-code", "--", repository["url"], reference],
                    Path(directory),
                    30,
                    env=credential_environment,
                )
            except RuntimeError as exc:
                raise RuntimeError(redact_secrets(str(exc), env or {})) from None
    lines = [line.split("\t") for line in result.splitlines()]
    if len(lines) != 1 or len(lines[0]) != 2 or lines[0][1] != reference:
        raise ValueError("The authorized source branch did not resolve to one commit")
    revision = lines[0][0]
    if not re.fullmatch(r"[a-f0-9]{40}|[a-f0-9]{64}", revision):
        raise ValueError("Source resolution returned an invalid commit")
    return revision


async def push(
    workspace: Path,
    branch: str,
    *,
    env: dict[str, str] | None = None,
    repository_url: str | None = None,
    revision: str | None = None,
    expected_remote_revision: str | None | Literal[False] = False,
) -> None:
    if repository_url and await checked(["git", "remote", "get-url", "--push", "origin"], workspace) != repository_url:
        raise ValueError("Git push origin changed from the authorized repository")
    repository_url = repository_url or await checked(["git", "remote", "get-url", "--push", "origin"], workspace)
    revision = revision or await checked(["git", "rev-parse", "HEAD"], workspace)
    lease: list[str] = []
    if expected_remote_revision is not False:
        if (
            not branch.startswith("forge/")
            or await checked(["git", "symbolic-ref", "--short", "HEAD"], workspace) != branch
        ):
            raise ValueError("Checkpoint push requires the exact factory-owned implementation branch")
        await checked(["git", "check-ref-format", "refs/heads/" + branch], workspace)
        if await checked(["git", "rev-parse", "HEAD"], workspace) != revision:
            raise ValueError("Checkpoint push revision differs from the factory-owned workspace head")
        if expected_remote_revision is not None:
            if type(expected_remote_revision) is not str or not re.fullmatch(
                r"[a-f0-9]{40}|[a-f0-9]{64}", expected_remote_revision
            ):
                raise ValueError("Checkpoint push needs an exact previous remote revision")
            try:
                await checked(["git", "merge-base", "--is-ancestor", expected_remote_revision, revision], workspace)
            except RuntimeError:
                raise ValueError("Checkpoint push cannot rewrite divergent remote history") from None
        # Expected-ref CAS alone can permit rewrites; the separate ancestry proof is mandatory.
        lease = [f"--force-with-lease=refs/heads/{branch}:{expected_remote_revision or ''}"]
    # Worker-controlled Git hooks and TLS settings never receive application credentials.
    with tempfile.TemporaryDirectory(prefix="forge-publication-") as directory:
        staging = Path(directory)
        hooks = staging / "empty-hooks"
        hooks.mkdir()
        await checked(["git", "init", "--quiet"], staging)
        await checked(
            ["git", "-c", f"core.hooksPath={hooks}", "fetch", "--no-tags", "--", str(workspace), revision], staging
        )
        async with git_credentials(env) as credential_environment:
            try:
                await checked(
                    [
                        "git",
                        "-c",
                        f"core.hooksPath={hooks}",
                        "-c",
                        "http.sslVerify=true",
                        "push",
                        *lease,
                        "--",
                        repository_url,
                        f"{revision}:refs/heads/{branch}",
                    ],
                    staging,
                    env=credential_environment,
                )
            except RuntimeError as exc:
                raise RuntimeError(redact_secrets(str(exc), env or {})) from None


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

    async def create(
        self,
        repository: dict[str, Any],
        branch: str,
        title: str,
        body: str,
        *,
        authentication: tuple[str, str] | None = None,
    ) -> str:
        provider = repository["provider"]
        token = authentication[0] if authentication else os.environ.get(f"FORGE_{provider.upper()}_TOKEN", "")
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
            api = authentication[1] if authentication else api
            endpoint = f"{api.rstrip('/')}/repos/{path}/pulls"
            payload = {"head": branch, "base": target, "title": title[:200], "body": body, "draft": True}
        elif provider == "gitlab":
            api = authentication[1] if authentication else os.environ.get("FORGE_GITLAB_API", f"{base}/api/v4")
            endpoint = f"{api.rstrip('/')}/projects/{quote(path, safe='')}/merge_requests"
            headers = {"Authorization": f"Bearer {token}"} if authentication else {"PRIVATE-TOKEN": token}
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
