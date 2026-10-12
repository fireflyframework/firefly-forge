# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
import asyncio
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any

import httpx
import pytest

from firefly_forge.engine import Engine
from firefly_forge.models import Command, RepositoryInput, RunInput
from firefly_forge.process import checked, execute
from firefly_forge.scm import PullRequests, coordinates


async def seed_repository(path: Path) -> Path:
    path.mkdir()
    await checked(["git", "init", "-b", "main"], path)
    await checked(["git", "config", "user.email", "fixture@example.invalid"], path)
    await checked(["git", "config", "user.name", "Forge fixture"], path)
    (path / "README.md").write_text("Fixture repository\n")
    await checked(["git", "add", "README.md"], path)
    await checked(["git", "commit", "-m", "Create test fixture"], path)
    return path


@pytest.mark.parametrize(
    "url", ["git@github.com:org/repo.git", "ssh://git@github.com/org/repo.git", "ssh://git@github.com:22/org/repo.git"]
)
def test_ssh_clone_urls_resolve_https_api_host(url: str):
    assert coordinates(url) == ("https", "github.com", "org/repo")


class FixtureProvider:
    async def execute(self, workspace: Path, brief: str, backend: str, frontend: str, emit: Any) -> Any:
        from firefly_forge.providers import AgentResult

        (workspace / "product.py").write_text("def total(items):\n    return sum(items)\n")
        (workspace / "test_product.py").write_text("from product import total\nassert total([2, 3]) == 5\n")
        await emit("developing", "Implemented product")
        return AgentResult(summary="Implemented product", provider="fixture")


async def test_real_clone_checks_and_diff_preserve_source(tmp_path: Path, store_factory):
    source = await seed_repository(tmp_path / "source")
    store = await store_factory(tmp_path / "state.db")
    repo = await store.add_repository(RepositoryInput(name="product", url=str(source)))
    run = await store.create_run(
        RunInput(
            repository_id=repo["id"],
            brief="Create product",
            commands=[Command(argv=[sys.executable, "test_product.py"])],
        )
    )
    engine = Engine(store, tmp_path / "jobs", lambda _: FixtureProvider())
    await engine.execute_run(run["id"])
    result = await store.get_run(run["id"])
    assert result["status"] == "succeeded"
    assert result["verification"][0]["exit_code"] == 0
    assert "product.py" in result["changed_files"]
    assert "def total" in result["diff"]
    assert not (source / "product.py").exists()
    assert await checked(["git", "status", "--porcelain"], source) == ""
    with zipfile.ZipFile(tmp_path / "jobs" / f"{run['id']}.zip") as archive:
        patch = archive.read("forge-changes.patch")
    applied = subprocess.run(
        ["git", "apply", "--check", "-"], cwd=source, input=patch, capture_output=True, check=False
    )
    assert applied.returncode == 0, applied.stderr.decode()


async def test_test_operation_verifies_unchanged_source_without_coding(tmp_path: Path, store_factory):
    source = await seed_repository(tmp_path / "source")
    store = await store_factory(tmp_path / "state.db")
    repo = await store.add_repository(RepositoryInput(name="product", url=str(source)))
    run = await store.create_run(
        RunInput(
            repository_id=repo["id"],
            brief="Check existing source",
            operation="test",
            commands=[Command(argv=[sys.executable, "-c", "assert 2 + 3 == 5"])],
        )
    )

    def unexpected_provider(name):
        raise AssertionError("Test operation must not invoke a coding provider")

    await Engine(store, tmp_path / "jobs", unexpected_provider).execute_run(run["id"])
    result = await store.get_run(run["id"])
    assert result["status"] == "succeeded"
    assert result["changed_files"] == []
    assert result["final_verification"][0]["exit_code"] == 0
    assert result["source_tree"]


async def test_queued_private_config_and_skills_redact_every_evidence_surface(tmp_path: Path, store_factory):
    from firefly_forge.configuration import ProviderConfiguration

    secret = "private-config-not-in-environment"
    source = await seed_repository(tmp_path / "source")
    store = await store_factory(tmp_path / "state.db")
    store.configuration.update("bridge", ProviderConfiguration(values={"token": secret}))
    repo = await store.add_repository(RepositoryInput(name="product", url=str(source)))
    run = await store.create_run(
        RunInput(
            repository_id=repo["id"],
            brief="Create product",
            agent="bridge",
            commands=[Command(argv=[sys.executable, "test_product.py"])],
        )
    )
    store.configuration.update("bridge", ProviderConfiguration(values={"token": "replaced-secret"}))

    class SensitiveProvider(FixtureProvider):
        async def execute(self, workspace, brief, backend, frontend, emit):
            assert "Selected reusable skills" in brief
            assert "credentials" in brief
            result = await super().execute(workspace, brief, backend, frontend, emit)
            await emit("provider", secret)
            result.summary = secret
            result.usage = {"provider_echo": secret}
            return result

    await Engine(store, tmp_path / "jobs", lambda _: SensitiveProvider()).execute_run(run["id"])
    result = await store.get_run(run["id"])
    import json

    assert secret not in json.dumps(result)
    assert result["status"] == "succeeded"


async def test_failed_checks_block_publication(tmp_path: Path, store_factory):
    source = await seed_repository(tmp_path / "source")
    store = await store_factory(tmp_path / "state.db")
    repo = await store.add_repository(RepositoryInput(name="product", url=str(source)))
    run = await store.create_run(
        RunInput(
            repository_id=repo["id"],
            brief="Create product",
            commands=[Command(argv=[sys.executable, "-c", "raise SystemExit(7)"])],
        )
    )
    engine = Engine(store, tmp_path / "jobs", lambda _: FixtureProvider())
    await engine.execute_run(run["id"])
    assert (await store.get_run(run["id"]))["status"] == "failed"
    with pytest.raises(ValueError, match="successful"):
        await engine.publish(run["id"])


async def test_factory_repairs_from_failed_verification_and_keeps_attempt_evidence(tmp_path: Path, store_factory):

    class RepairProvider(FixtureProvider):
        calls = 0

        async def execute(self, workspace: Path, brief: str, backend: str, frontend: str, emit: Any) -> Any:
            self.calls += 1
            result = await super().execute(workspace, brief, backend, frontend, emit)
            if self.calls == 1:
                (workspace / "product.py").write_text("def total(items):\n    return 0\n")
            else:
                assert "Verification failed" in brief
                assert "AssertionError" in brief
            return result

    source = await seed_repository(tmp_path / "source")
    store = await store_factory(tmp_path / "state.db")
    repo = await store.add_repository(RepositoryInput(name="product", url=str(source)))
    run = await store.create_run(
        RunInput(
            repository_id=repo["id"],
            brief="Create product",
            commands=[Command(argv=[sys.executable, "test_product.py"])],
        )
    )
    provider = RepairProvider()
    engine = Engine(store, tmp_path / "jobs", lambda _: provider)
    await engine.execute_run(run["id"])
    result = await store.get_run(run["id"])
    assert result["status"] == "succeeded"
    assert provider.calls == 2
    assert [check["exit_code"] for check in result["verification"]] == [1, 0]
    assert result["final_verification"][0]["exit_code"] == 0


async def test_verification_cannot_sneak_sensitive_files_into_published_source(tmp_path: Path, store_factory):
    source = await seed_repository(tmp_path / "source")
    store = await store_factory(tmp_path / "state.db")
    repo = await store.add_repository(RepositoryInput(name="product", url=str(source)))
    run = await store.create_run(
        RunInput(
            repository_id=repo["id"],
            brief="Create product",
            commands=[
                Command(
                    argv=[
                        sys.executable,
                        "-c",
                        "from pathlib import Path; Path('.env.production').write_text('password=hidden')",
                    ]
                )
            ],
        )
    )
    await Engine(store, tmp_path / "jobs", lambda _: FixtureProvider()).execute_run(run["id"])
    assert (await store.get_run(run["id"]))["status"] == "failed"
    assert "private" in (await store.get_run(run["id"]))["error"]


async def test_publication_refuses_changes_after_verification(tmp_path: Path, monkeypatch: Any, store_factory):
    source = await seed_repository(tmp_path / "source")
    store = await store_factory(tmp_path / "state.db")
    repo = await store.add_repository(RepositoryInput(name="product", url=str(source), provider="github"))
    run = await store.create_run(
        RunInput(
            repository_id=repo["id"],
            brief="Create product",
            commands=[Command(argv=[sys.executable, "test_product.py"])],
        )
    )
    engine = Engine(store, tmp_path / "jobs", lambda _: FixtureProvider())
    await engine.execute_run(run["id"])
    assert (await store.get_run(run["id"]))["status"] == "succeeded"
    (tmp_path / "jobs" / run["id"] / "product.py").write_text("broken source after verification")
    monkeypatch.setenv("FORGE_GITHUB_TOKEN", "fixture-token")
    with pytest.raises(ValueError, match="changed since verification"):
        await engine.publish(run["id"])


async def test_second_server_cannot_interrupt_first_workers(tmp_path: Path, store_factory):
    first = Engine(await store_factory(tmp_path / "state.db"), tmp_path / "jobs", lambda _: FixtureProvider())
    second = Engine(await store_factory(tmp_path / "state.db"), tmp_path / "jobs", lambda _: FixtureProvider())
    await first.start()
    try:
        with pytest.raises(RuntimeError, match="already running"):
            await second.start()
    finally:
        await first.stop()


async def test_empty_implementation_and_missing_checks_fail(tmp_path: Path, store_factory):
    source = await seed_repository(tmp_path / "source")
    store = await store_factory(tmp_path / "state.db")
    repo = await store.add_repository(RepositoryInput(name="product", url=str(source)))
    run = await store.create_run(RunInput(repository_id=repo["id"], brief="Create product"))
    engine = Engine(store, tmp_path / "jobs", lambda _: FixtureProvider())
    await engine.execute_run(run["id"])
    result = await store.get_run(run["id"])
    assert result["status"] == "failed"
    assert "verification" in result["error"].lower()


async def test_timeout_stops_command(tmp_path: Path):
    with pytest.raises(TimeoutError):
        await execute([sys.executable, "-c", "import time; time.sleep(30)"], tmp_path, timeout=1)


async def test_timeout_kills_descendants_after_leader_exits(tmp_path: Path):
    child = "import time; from pathlib import Path; time.sleep(3); Path('orphan.txt').write_text('alive')"
    parent = f"import subprocess, sys; subprocess.Popen([sys.executable, '-c', {child!r}])"
    with pytest.raises(TimeoutError):
        await execute([sys.executable, "-c", parent], tmp_path, timeout=1)
    await asyncio.sleep(2.2)
    assert not (tmp_path / "orphan.txt").exists()


async def test_cancellation_stops_active_worker(tmp_path: Path, store_factory):

    class BlockingProvider:
        async def execute(self, workspace: Path, brief: str, backend: str, frontend: str, emit: Any) -> Any:
            await asyncio.sleep(60)

    source = await seed_repository(tmp_path / "source")
    store = await store_factory(tmp_path / "state.db")
    repo = await store.add_repository(RepositoryInput(name="product", url=str(source)))
    run = await store.create_run(RunInput(repository_id=repo["id"], brief="Create product"))
    engine = Engine(store, tmp_path / "jobs", lambda _: BlockingProvider())
    await engine.start()
    for _ in range(100):
        if (await store.get_run(run["id"]))["status"] == "developing":
            break
        await asyncio.sleep(0.02)
    await engine.cancel(run["id"])
    await engine.stop()
    assert (await store.get_run(run["id"]))["status"] == "cancelled"


@pytest.mark.parametrize(
    ("provider", "url", "endpoint", "response"),
    [
        (
            "github",
            "https://github.com/org/repo.git",
            "https://api.github.com/repos/org/repo/pulls",
            {"html_url": "https://github.com/org/repo/pull/1"},
        ),
        (
            "gitlab",
            "https://gitlab.com/group/team/repo.git",
            "https://gitlab.com/api/v4/projects/group%2Fteam%2Frepo/merge_requests",
            {"web_url": "https://gitlab.com/group/team/repo/-/merge_requests/1"},
        ),
        (
            "bitbucket",
            "https://bitbucket.org/org/repo.git",
            "https://api.bitbucket.org/2.0/repositories/org/repo/pullrequests",
            {"links": {"html": {"href": "https://bitbucket.org/org/repo/pull-requests/1"}}},
        ),
        (
            "gitea",
            "https://git.example.com/org/repo.git",
            "https://git.example.com/api/v1/repos/org/repo/pulls",
            {"html_url": "https://git.example.com/org/repo/pulls/1"},
        ),
        (
            "azure",
            "https://dev.azure.com/org/project/_git/repo",
            "https://dev.azure.com/org/project/_apis/git/repositories/repo/pullrequests?api-version=7.1",
            {"pullRequestId": 1},
        ),
    ],
)
async def test_source_control_pull_request_contracts(
    provider: str, url: str, endpoint: str, response: Any, monkeypatch: Any
):
    monkeypatch.setenv(f"FORGE_{provider.upper()}_TOKEN", "fixture-token")
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert str(request.url) == endpoint
        return httpx.Response(201, json=response)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await PullRequests(client).create(
            {"provider": provider, "url": url, "default_branch": "main"}, "forge/job", "Build product", "Checks passed"
        )
    assert result.startswith("https://")
    assert len(seen) == 1
    assert b"forge/job" in seen[0].content
    assert b"main" in seen[0].content
