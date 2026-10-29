# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Prepared real-storage parity and activation controls; HTTP/backend execution is controlled."""

import copy
import json
import os
import subprocess
from types import SimpleNamespace
from uuid import uuid4

import pytest
from framework_knowledge_fixtures import official_fixture
from test_framework_knowledge_native import native_knowledge

from firefly_forge.engine import Engine
from firefly_forge.framework_knowledge_models import (
    FrameworkCheckUpdatesInput,
    FrameworkGenerationTargetInput,
    FrameworkRefreshInput,
)
from firefly_forge.framework_knowledge_worker import FrameworkKnowledgeWorker
from firefly_forge.git_workspace import git_environment, trusted_git
from firefly_forge.journal import Journal
from firefly_forge.models import Command, RunInput
from firefly_forge.process import ProcessResult
from firefly_forge.providers import AgentResult
from firefly_forge.sandbox import SandboxExecutor


async def index_framework(manager, framework):
    manager.transport = official_fixture(framework)[0]
    discovery = await manager.check_updates(framework, FrameworkCheckUpdatesInput())
    request_id = uuid4().hex
    await manager.refresh(
        framework,
        request_id,
        FrameworkRefreshInput(
            version=1,
            discovery_digest=discovery["release"]["digest"],
            expected_status_revision=discovery["status"]["revision"],
            policy_digest=discovery["policy_digest"],
        ),
    )
    await FrameworkKnowledgeWorker(manager).advance(request_id)
    result = await manager.get_refresh(framework, request_id)
    assert result["state"] == "ready", result
    return result


async def test_native_generation_activation_uses_actual_engine_runtime_qa_and_git_receipts(store, tmp_path):
    async with native_knowledge(store, tmp_path) as (manager, remote, _, original):
        complete = await index_framework(manager, "pyfly")

        def git(*args):
            return subprocess.run(
                trusted_git(["git", *args]),
                cwd=tmp_path,
                env=git_environment({"PATH": os.defpath, "LANG": "C.UTF-8"}),
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()

        git("init", "-b", "main")
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname="controlled-product"\ndependencies=["pyfly==26.10.11"]\n'
        )
        git("add", "pyproject.toml")
        git(
            "-c",
            "user.name=Native knowledge fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-m",
            "Seed controlled framework target",
        )
        store.framework_knowledge = manager
        journal = Journal(store)
        manager.knowledge.journal = journal
        manager.knowledge.frameworks = manager
        calls = []

        class Worker:
            async def execute(self, workspace, brief, *args):
                calls.append(brief)
                assert "26.10.11" in brief
                (workspace / "service.py").write_text("from pyfly.container import service\n")
                return AgentResult("Controlled framework implementation", "controlled")

        class Backend:
            async def run(self, config, argv, cwd, timeout, limit, **kwargs):
                output = (
                    json.dumps({"runtime": "python", "version": "3.13.13"})
                    if "--no-project" in argv
                    else "Known controlled product check"
                )
                return ProcessResult(argv, 0, output)

        engine = Engine(store, tmp_path / "workspaces", lambda _: Worker(), journal=journal)
        engine.sandbox = SandboxExecutor(store, journal, backends=SimpleNamespace(select=lambda config: Backend()))
        engine.knowledge = manager.knowledge
        manager.knowledge.engine = engine
        engine.enforce_ownership = True
        run = await store.create_run(
            RunInput(
                repository_id=original["repository_id"],
                project_id=original["project_id"],
                component_id="api",
                agent="codex",
                brief="Build a controlled current pyfly service",
                frontend="none",
                skills=[],
                commands=[Command(argv=["known-product-check"], timeout=10)],
                framework_target={"framework_version": "26.10.11"},
            )
        )
        await engine.execute_run(run["id"])
        final = await store.get_run(run["id"])
        assert final["status"] == "succeeded", final.get("error")
        assert len(calls) == 1 and final["framework_runtime_attestation"]
        corpus_ref = complete["corpus"]["corpus_ref"]
        candidates = await manager.generation_candidates("pyfly", corpus_ref, page_size=1)
        assert candidates["next_cursor"] is None and len(candidates["items"]) == 1
        candidate = candidates["items"][0]
        assert candidate["validation_run_id"] == run["id"]
        assert candidate["repository_name"] == final["repository_name"] and candidate["component_id"] == "api"
        assert candidate["source_tree"] == final["source_tree"]
        assert candidate["runtime"] == {"runtime": "python", "version": "3.13.13"}
        status = next(item for item in (await manager.status())["frameworks"] if item["framework"] == "pyfly")
        request_id = uuid4().hex
        body = FrameworkGenerationTargetInput(
            version=1,
            corpus_ref=corpus_ref,
            expected_status_revision=status["revision"],
            validation_run_id=run["id"],
        )
        altered = copy.deepcopy(final["framework_runtime_attestation"])
        altered["observed"]["version"] = "3.11.9"
        await store.update(run["id"], framework_runtime_attestation=altered)
        assert not (await manager.generation_candidates("pyfly", corpus_ref))["items"]
        with pytest.raises(ValueError, match="canonical|validation"):
            await manager.activate_generation_target("pyfly", uuid4().hex, body)
        await store.update(run["id"], framework_runtime_attestation=final["framework_runtime_attestation"])
        target = await manager.activate_generation_target("pyfly", request_id, body)
        assert target["validation_digest"] == candidate["validation_digest"]
        assert await manager.activate_generation_target("pyfly", request_id, body) == target
        assert await manager.generation_target("pyfly", request_id) == target
        future = await manager.prepare_target_request({"backend": "pyfly", "operation": "build"})
        assert future["generation_activation"] == target and future["supported_default"] == "26.10.11"
        assert final["framework_knowledge_request"]["generation_activation"] is None
        assert await manager.assert_run_pin(await store.get_run(run["id"]))
