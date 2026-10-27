# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Native knowledge prerequisites used only by seven client/maintain controls.

Official and SDK HTTP plus framework interpreter checks are controlled boundaries.
Storage, authority, corpus ingestion, engine receipts and target activation are native.
"""

import json
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from checkpoint_test_native_fixtures import checkpoint_native
from framework_knowledge_fixtures import ControlledFlycanon, official_fixture
from scm_polling_native_fixtures import native_poll
from worker_reply_fixtures import authenticated_operator

from firefly_forge.engine import Engine
from firefly_forge.framework_knowledge import FrameworkKnowledge
from firefly_forge.framework_knowledge_conformance import conformance_commands, runtime_probe_command
from firefly_forge.framework_knowledge_models import (
    FrameworkCheckUpdatesInput,
    FrameworkGenerationTargetInput,
    FrameworkRefreshInput,
)
from firefly_forge.framework_knowledge_worker import FrameworkKnowledgeWorker
from firefly_forge.journal import Journal
from firefly_forge.knowledge import KnowledgeConfiguration, KnowledgeManager
from firefly_forge.models import Command, RepositoryInput, RunInput
from firefly_forge.process import ProcessResult, checked
from firefly_forge.project_models import ProjectDefinition
from firefly_forge.projects import Projects
from firefly_forge.providers import AgentResult
from firefly_forge.runtime_roles import RuntimeRoles
from firefly_forge.sandbox import SandboxExecutor
from firefly_forge.store import Store

VERSION = "26.10.11"
MANIFEST = '[project]\nname="native-client-control"\ndependencies=["pyfly==26.10.11"]\n'
PRODUCT_CHECK = ["controlled-framework-product-check"]


class FrameworkFixtureBackend:
    def __init__(self, delegate=None):
        self.delegate = delegate

    async def run(self, config, argv, cwd, timeout, limit, **kwargs):
        if argv == runtime_probe_command({"runtime": "python"}):
            return ProcessResult(argv, 0, json.dumps({"runtime": "python", "version": "3.13.13"}))
        if argv == PRODUCT_CHECK or argv in conformance_commands({"framework": "pyfly", "framework_version": VERSION}):
            return ProcessResult(argv, 0, "Controlled framework prerequisite check")
        if self.delegate is None:
            raise AssertionError("Unexpected command in controlled framework prerequisite")
        return await self.delegate.run(config, argv, cwd, timeout, limit, **kwargs)


def bind_framework_knowledge(engine, remote):
    knowledge = KnowledgeManager(engine.store, engine, transport=remote.transport())
    manager = FrameworkKnowledge(engine.store, knowledge, transport=official_fixture()[0])
    engine.store.framework_knowledge = manager
    engine.knowledge = knowledge
    knowledge.frameworks, knowledge.journal = manager, engine.journal
    return manager


def preserve_product_checks_with_framework_fixture(engine):
    backends = engine.sandbox.backends
    engine.sandbox.backends = SimpleNamespace(select=lambda config: FrameworkFixtureBackend(backends.select(config)))


@contextmanager
def native_maintain_knowledge_fixture(retained):
    """Create a local replacement before capture; never edit the old checkpoint."""
    from firefly_forge import scm

    directory = retained.origin.parent / "maintain-knowledge-prerequisite"
    directory.mkdir()
    retained.case.http.portal.call(retained.store.release_ownership)
    with pytest.MonkeyPatch.context() as patch:
        # The API root stays identical to its immutable split-role storage binding.
        polling = native_poll.__wrapped__(retained.origin.parent, retained.store.schema, patch, SimpleNamespace())
        checkpoint = None
        try:
            case = next(polling)
            checked = retained.original_checked
            origin = directory / "controlled-git-origin"

            async def exact_source(argv, cwd, *args, **kwargs):
                result = await checked(argv, cwd, *args, **kwargs)
                if argv[1] == "init" and Path(cwd) == origin:
                    (origin / "pyproject.toml").write_text(MANIFEST)
                return result

            patch.setattr(scm, "checked", exact_source)
            checkpoint = checkpoint_native.__wrapped__(case, directory, patch, SimpleNamespace())
            current = next(checkpoint)
            remote = case.http.portal.call(activate_native_client_target, current.store, directory / "target")
            bind_framework_knowledge(current.engine, remote)
            preserve_product_checks_with_framework_fixture(current.engine)
            yield current
        finally:
            try:
                if checkpoint is not None:
                    checkpoint.close()
            finally:
                polling.close()


def test_local_manifest_hook_preserves_new_clone_and_remote_without_retained_origin(tmp_path, monkeypatch):
    import ast
    import asyncio
    import sys

    from firefly_forge import scm

    captured = []
    url = "https://github.com/team/product.git"
    old_origin = tmp_path / "controlled-git-origin"
    old_origin.mkdir()
    marker = old_origin / "retained.txt"
    marker.write_bytes(b"retained source must stay exact\n")
    before = marker.read_bytes()
    fixture_source = Path(__file__).with_name("checkpoint_test_native_fixtures.py").read_text()
    fixture_ast = next(
        node for node in ast.parse(fixture_source).body if getattr(node, "name", "") == "checkpoint_native"
    )
    hook = next(node for node in fixture_ast.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "git")

    async def primitive(argv, cwd, *args, **kwargs):
        captured.append((list(argv), Path(cwd)))
        return ""

    case = SimpleNamespace(repo={"url": url})

    def native_hook(origin, checked):
        namespace = dict(case=case, origin=origin, original_checked=checked, transport=[], Path=Path)
        exec(compile(ast.Module(body=[hook], type_ignores=[]), "unchanged-native-git-hook", "exec"), namespace)
        return namespace["git"]

    outer = native_hook(old_origin, primitive)
    monkeypatch.setattr(scm, "checked", outer)

    class Portal:
        def call(self, method, *args):
            return asyncio.run(method(*args))

    async def release():
        return None

    case.http = SimpleNamespace(portal=Portal())
    retained = SimpleNamespace(
        origin=old_origin,
        original_checked=primitive,
        case=case,
        store=SimpleNamespace(release_ownership=release, schema="owned_fixture_test"),
    )

    def polling(*args):
        yield case

    def checkpoint(case, directory, *args):
        origin = directory / "controlled-git-origin"
        origin.mkdir()
        git = native_hook(origin, scm.checked)

        async def commands():
            await git(["git", "init", "--initial-branch=main"], origin)
            await git(["git", "clone", url, str(directory / "workspace")], directory)

        asyncio.run(commands())
        yield SimpleNamespace(store=retained.store, engine=SimpleNamespace())

    async def activate(*args):
        return object()

    module = sys.modules[__name__]
    monkeypatch.setattr(module, "native_poll", SimpleNamespace(__wrapped__=polling))
    monkeypatch.setattr(module, "checkpoint_native", SimpleNamespace(__wrapped__=checkpoint))
    monkeypatch.setattr(module, "activate_native_client_target", activate)
    monkeypatch.setattr(module, "bind_framework_knowledge", lambda *args: None)
    monkeypatch.setattr(module, "preserve_product_checks_with_framework_fixture", lambda *args: None)
    with native_maintain_knowledge_fixture(retained):
        new_origin = tmp_path / "maintain-knowledge-prerequisite" / "controlled-git-origin"
        assert (new_origin / "pyproject.toml").read_text() == MANIFEST
        assert [argv for argv, _ in captured if argv[1] == "clone"] == [
            ["git", "clone", str(new_origin), str(new_origin.parent / "workspace")]
        ]
        assert [argv for argv, _ in captured if argv[1:3] == ["remote", "set-url"]] == [
            ["git", "remote", "set-url", "origin", url]
        ]
        assert all(str(old_origin) not in argv and cwd != old_origin for argv, cwd in captured)
    assert scm.checked is outer
    assert marker.read_bytes() == before and list(old_origin.iterdir()) == [marker]


def test_validation_engine_selects_controlled_worker_at_construction(tmp_path):
    import ast

    module = ast.parse(Path(__file__).read_text())
    fixture = next(node for node in module.body if getattr(node, "name", "") == "activate_native_client_target")
    constructor = next(
        node
        for node in ast.walk(fixture)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "Engine"
    )

    class Worker:
        pass

    engine = eval(
        compile(ast.Expression(body=constructor), "native-client-validation-constructor", "eval"),
        dict(
            Engine=Engine,
            worker=SimpleNamespace(path=tmp_path / "factory.db"),
            directory=tmp_path,
            journal=SimpleNamespace(),
            Worker=Worker,
        ),
    )
    assert engine.configured_provider is False
    assert isinstance(engine.provider_factory("codex"), Worker)


async def activate_native_client_target(store, directory: Path):
    """Produce and activate canonical proof without assigning ready/proof rows."""
    directory.mkdir()
    separate = store.runtime_roles is not None and store.runtime_roles.role == "api"
    worker = Store(store.path) if separate else store
    runtime = None
    try:
        if separate:
            worker.configuration = store.configuration
            runtime = RuntimeRoles(worker, "worker")
            worker.runtime_roles = runtime
            await worker.initialize()
            await runtime.start()
        await worker.acquire_ownership()
        remote = ControlledFlycanon()
        journal = Journal(worker)
        engine = Engine(worker, directory / "workspaces", lambda _: Worker(), journal=journal)
        engine.enforce_ownership = True
        engine.sandbox = SandboxExecutor(
            worker, journal, backends=SimpleNamespace(select=lambda config: FrameworkFixtureBackend())
        )
        manager = bind_framework_knowledge(engine, remote)
        async with authenticated_operator(worker):
            manager.knowledge.configure(
                KnowledgeConfiguration(
                    values={
                        "enabled": True,
                        "endpoint": "http://127.0.0.1:18500",
                        "tenant_id": "firefly",
                        "api_key": "fictional-native-client-knowledge-key",
                        "auto_memory": False,
                    }
                )
            )
            discovery = await manager.check_updates("pyfly", FrameworkCheckUpdatesInput())
            refresh_id = uuid4().hex
            await manager.refresh(
                "pyfly",
                refresh_id,
                FrameworkRefreshInput(
                    version=1,
                    discovery_digest=discovery["release"]["digest"],
                    expected_status_revision=discovery["status"]["revision"],
                    policy_digest=discovery["policy_digest"],
                ),
            )
            await FrameworkKnowledgeWorker(manager).advance(refresh_id)
            complete = await manager.get_refresh("pyfly", refresh_id)
            assert complete["state"] == "ready", complete
            source = directory / "source"
            source.mkdir()
            await checked(["git", "init", "--initial-branch=main"], source)
            (source / "pyproject.toml").write_text(MANIFEST)
            await checked(["git", "add", "pyproject.toml"], source)
            await checked(
                [
                    "git",
                    "-c",
                    "user.name=Native client fixture",
                    "-c",
                    "user.email=fixture@example.invalid",
                    "commit",
                    "-m",
                    "Seed exact framework target",
                ],
                source,
            )
            repository = await worker.add_repository(
                RepositoryInput(name="Framework client prerequisite", url=str(source))
            )
            project = await Projects(worker).configure(
                ProjectDefinition(
                    name="Framework client prerequisite",
                    description="Canonical controlled framework validation",
                    topology="monorepo",
                    owners=["Fixture owner"],
                    components=[
                        dict(id="api", name="API", repository_id=repository["id"], responsibility="Validate target")
                    ],
                )
            )

            class Worker:
                async def execute(self, workspace, brief, *args):
                    assert VERSION in brief
                    (workspace / "service.py").write_text("from pyfly.container import service\n")
                    return AgentResult("Controlled framework client prerequisite", "controlled fixture")

            run = await worker.create_run(
                RunInput(
                    repository_id=repository["id"],
                    project_id=project["id"],
                    component_id="api",
                    agent="codex",
                    brief="Validate exact current framework for native client controls",
                    frontend="none",
                    skills=[],
                    commands=[Command(argv=PRODUCT_CHECK, timeout=10)],
                    framework_target={"framework_version": VERSION},
                )
            )
            await engine.execute_run(run["id"])
            final = await worker.get_run(run["id"])
            assert final["status"] == "succeeded", final.get("error")
            assert final["framework_runtime_attestation"] and final["final_verification"]
            candidates = await manager.generation_candidates("pyfly", complete["corpus"]["corpus_ref"])
            assert [item["validation_run_id"] for item in candidates["items"]] == [run["id"]]
            status = next(item for item in (await manager.status())["frameworks"] if item["framework"] == "pyfly")
            target = await manager.activate_generation_target(
                "pyfly",
                uuid4().hex,
                FrameworkGenerationTargetInput(
                    version=1,
                    corpus_ref=complete["corpus"]["corpus_ref"],
                    expected_status_revision=status["revision"],
                    validation_run_id=run["id"],
                ),
            )
            assert target["validation_digest"] == candidates["items"][0]["validation_digest"]
            if separate:
                store.framework_knowledge.knowledge.transport = remote.transport()
            return remote
    finally:
        if separate:
            try:
                await worker.release_ownership()
            finally:
                if runtime is not None:
                    await runtime.stop()
                await worker.close()
