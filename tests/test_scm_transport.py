# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
from pathlib import Path

import httpx

from firefly_forge.scm import PullRequests, clone, git_credentials, push


async def test_git_clone_uses_ephemeral_askpass_without_url_or_file_credentials(tmp_path, monkeypatch):
    calls = []

    async def checked(argv, cwd, timeout=300, *, env=None):
        calls.append((argv, env))
        if argv[1] == "clone":
            assert "private-token" not in " ".join(argv)
            helper = Path(env["GIT_ASKPASS"])
            assert "private-token" not in helper.read_text()
            assert env["FORGE_GIT_PASSWORD"] == "private-token"
            assert env["GIT_CONFIG_VALUE_0"] == ""
            return ""
        return "baseline"

    monkeypatch.setattr("firefly_forge.scm.checked", checked)
    await clone(
        {"url": "https://github.com/org/project.git", "default_branch": "main"},
        tmp_path / "clone",
        "forge/run",
        env={"FORGE_GIT_USERNAME": "x-access-token", "FORGE_GIT_PASSWORD": "private-token"},
    )
    assert not Path(calls[0][1]["GIT_ASKPASS"]).exists()
    assert calls[1][1] is None


async def test_gitlab_application_oauth_uses_bearer_without_legacy_environment():
    def response(request):
        assert request.headers["Authorization"] == "Bearer application-token"
        assert "PRIVATE-TOKEN" not in request.headers
        return httpx.Response(201, json={"web_url": "https://gitlab.com/group/project/-/merge_requests/1"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
        url = await PullRequests(client).create(
            {"url": "https://gitlab.com/group/project.git", "provider": "gitlab", "default_branch": "main"},
            "forge/run",
            "Title",
            "Evidence",
            authentication=("application-token", "https://gitlab.com/api/v4"),
        )
    assert url.endswith("/1")


async def test_askpass_refuses_a_different_origin(tmp_path):
    import subprocess
    import sys

    async with git_credentials(
        {
            "FORGE_GIT_USERNAME": "user",
            "FORGE_GIT_PASSWORD": "private-token",
            "FORGE_GIT_ORIGIN": "https://github.com",
        }
    ) as environment:
        wrong = subprocess.run(
            [sys.executable, environment["GIT_ASKPASS"], "Password for 'https://evil.example':"],
            env=environment,
            capture_output=True,
            text=True,
        )
        right = subprocess.run(
            [sys.executable, environment["GIT_ASKPASS"], "Password for 'https://user@github.com':"],
            env=environment,
            capture_output=True,
            text=True,
        )
        assert wrong.returncode != 0 and "private-token" not in wrong.stdout + wrong.stderr
        assert right.returncode == 0 and right.stdout.strip() == "private-token"


async def test_publication_does_not_run_worker_git_hooks(tmp_path):
    from test_execution import seed_repository

    from firefly_forge.process import checked

    source = await seed_repository(tmp_path / "source")
    remote = tmp_path / "remote.git"
    await checked(["git", "init", "--bare", str(remote)], tmp_path)
    await checked(["git", "remote", "add", "origin", str(remote)], source)
    marker = tmp_path / "hook-ran"
    hook = source / ".git" / "hooks" / "pre-push"
    hook.write_text(f"#!/bin/sh\nprintf leaked > '{marker}'\nexit 1\n")
    hook.chmod(0o700)
    await push(source, "forge/test", repository_url=str(remote))
    assert not marker.exists()
    assert await checked(["git", "rev-parse", "refs/heads/forge/test"], remote) == await checked(
        ["git", "rev-parse", "HEAD"], source
    )
