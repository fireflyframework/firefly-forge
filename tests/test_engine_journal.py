# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
import asyncio
import sys

import pytest
from test_execution import seed_repository

from firefly_forge.engine import Engine
from firefly_forge.journal import Journal
from firefly_forge.models import Command, RepositoryInput, RunInput
from firefly_forge.providers import AgentResult, ProviderLimitError


async def test_quota_pause_and_handoff_preserve_workspace_and_write_ahead_evidence(tmp_path, store):
    source = await seed_repository(tmp_path / "source")
    repository = await store.add_repository(RepositoryInput(name="Inventory", url=str(source)))
    run = await store.create_run(
        RunInput(
            repository_id=repository["id"],
            brief="Build inventory",
            commands=[Command(argv=[sys.executable, "-c", "from product import total; assert total([2,3]) == 5"])],
        )
    )
    journal = Journal(store)

    class PausingWorker:
        calls = 0

        async def execute(self, workspace, brief, backend, frontend, emit):
            self.calls += 1
            entries = await journal.entries(run["id"])
            assert entries[-1]["kind"] == "intent" and entries[-1]["task_id"] == "implement"
            if self.calls == 1:
                (workspace / "partial.py").write_text("partial = True\n")
                raise ProviderLimitError("Account credits exhausted", kind="quota")
            assert (workspace / "partial.py").is_file()
            (workspace / "product.py").write_text("def total(items):\n    return sum(items)\n")
            return AgentResult("Inventory complete", "fixture")

    worker = PausingWorker()
    engine = Engine(store, tmp_path / "jobs", lambda _: worker, journal=journal)
    await engine.execute_run(run["id"])
    paused = await store.get_run(run["id"])
    assert paused["status"] == "paused"
    assert paused["pause"]["kind"] == "quota"
    assert paused["pause"]["automatic"] is False
    old_revision = paused["config_revision"]
    resumed = await journal.resume(run["id"], "claude")
    assert resumed["resume_existing"] and resumed["config_revision"] != old_revision
    await engine.execute_run(run["id"])
    final = await store.get_run(run["id"])
    assert final["status"] == "succeeded"
    assert final["checkpoint_revision"] != final["base_revision"]
    assert await engine.validated_workspace(run["id"]) == tmp_path / "jobs" / run["id"]
    assert worker.calls == 2
    assert final["policy_snapshot"] == paused["policy_snapshot"]
    entries = await journal.entries(run["id"])
    assert Journal.verify_chain(entries)
    assert sum(e["kind"] == "intent" and e["task_id"] == "prepare" for e in entries) == 1
    assert any(e["kind"] == "handoff" for e in entries)


async def test_project_fair_dispatch_does_not_block_second_project(tmp_path, store):
    import asyncio

    source = await seed_repository(tmp_path / "source")
    first = await store.add_repository(RepositoryInput(name="First", url=str(source)))
    second = await store.add_repository(RepositoryInput(name="Second", url=str(source)))
    runs = []
    for project in [first, first, second]:
        runs.append(
            await store.create_run(
                RunInput(
                    repository_id=project["id"],
                    brief="Build " + project["name"],
                    commands=[
                        Command(argv=[sys.executable, "-c", "from product import product; assert product == 'built'"])
                    ],
                )
            )
        )
    release = asyncio.Event()

    class Worker:
        async def execute(self, workspace, brief, backend, frontend, emit):
            await release.wait()
            (workspace / "product.py").write_text("product = 'built'\n")
            return AgentResult("Built", "fixture")

    engine = Engine(store, tmp_path / "jobs", lambda _: Worker())
    await engine.dispatch_queued()
    assert set(engine.active) == {runs[0]["id"], runs[2]["id"]}
    release.set()
    await asyncio.gather(*engine.active.values())
    assert (await store.get_run(runs[1]["id"]))["status"] == "queued"


async def test_checkpoint_recovery_delivers_archive_without_repeating_worker(tmp_path, store, monkeypatch):
    source = await seed_repository(tmp_path / "source")
    repository = await store.add_repository(RepositoryInput(name="Recovery", url=str(source)))
    run = await store.create_run(
        RunInput(
            repository_id=repository["id"],
            brief="Add a product",
            skills=[],
            commands=[Command(argv=[sys.executable, "-c", "from product import value; assert value == 7"])],
        )
    )

    class Worker:
        calls = 0

        async def execute(self, workspace, brief, backend, frontend, emit):
            self.calls += 1
            (workspace / "product.py").write_text("value = 7\n")
            return AgentResult("Built", "fixture")

    worker = Worker()
    journal = Journal(store)
    engine = Engine(store, tmp_path / "jobs", lambda _: worker, journal=journal)
    artifact = engine._artifact

    async def interrupt_delivery(*args):
        raise asyncio.CancelledError

    monkeypatch.setattr(engine, "_artifact", interrupt_delivery)
    engine.stopping = True
    with pytest.raises(asyncio.CancelledError):
        await engine.execute_run(run["id"])
    engine.stopping = False
    interrupted = await store.get_run(run["id"])
    assert interrupted["checkpoint_revision"] and not interrupted["artifacts"]
    await journal.recover()
    await journal.resume(run["id"])
    monkeypatch.setattr(engine, "_artifact", artifact)
    await engine.execute_run(run["id"])
    final = await store.get_run(run["id"])
    assert final["status"] == "succeeded" and final["artifacts"]
    assert final["checkpoint_revision"] == interrupted["checkpoint_revision"]
    assert worker.calls == 1
    assert (await journal.plan(run["id"]))["tasks"][-1]["status"] == "done"


async def test_recovery_does_not_repeat_completed_test_commands(tmp_path, store, monkeypatch):
    source = await seed_repository(tmp_path / "source")
    repository = await store.add_repository(RepositoryInput(name="Test recovery", url=str(source)))
    counter = tmp_path / "counter.txt"
    code = f"from pathlib import Path; p=Path({str(counter)!r}); p.write_text(p.read_text()+'x' if p.exists() else 'x')"
    run = await store.create_run(
        RunInput(
            repository_id=repository["id"],
            brief="Check existing source",
            operation="test",
            skills=[],
            commands=[Command(argv=[sys.executable, "-c", code])],
        )
    )
    journal = Journal(store)
    engine = Engine(store, tmp_path / "jobs", journal=journal)
    checkpoint = engine._checkpoint_commit

    async def interrupt_checkpoint(*args):
        raise asyncio.CancelledError

    monkeypatch.setattr(engine, "_checkpoint_commit", interrupt_checkpoint)
    engine.stopping = True
    with pytest.raises(asyncio.CancelledError):
        await engine.execute_run(run["id"])
    engine.stopping = False
    assert counter.read_text() == "x"

    await journal.recover()
    await journal.resume(run["id"])
    monkeypatch.setattr(engine, "_checkpoint_commit", checkpoint)
    await engine.execute_run(run["id"])
    assert (await store.get_run(run["id"]))["status"] == "succeeded"
    assert counter.read_text() == "x"


async def test_operator_resume_racing_automatic_retry_keeps_dispatch_alive(tmp_path, store, monkeypatch):
    repository = await store.add_repository(
        RepositoryInput(name="Resume race", url="https://github.com/team/product.git")
    )
    run = await store.create_run(RunInput(repository_id=repository["id"], brief="Continue product work"))
    journal = Journal(store)
    intent = await journal.intent(run["id"], "implement")
    await journal.fail(run["id"], intent["idempotency_key"], "rate_limit", "Known retry time", retry_after=1)
    pause = (await store.get_run(run["id"]))["pause"]
    await store.update(run["id"], pause={**pause, "retry_at": 0})
    ready = journal.ready_retries
    selected = False

    async def ready_with_operator_resume():
        nonlocal selected
        identifiers = await ready()
        if identifiers and not selected:
            selected = True
            await journal.resume(run["id"])
        return identifiers

    monkeypatch.setattr(journal, "ready_retries", ready_with_operator_resume)
    engine = Engine(store, tmp_path / "jobs", journal=journal)
    dispatched = asyncio.Event()

    async def dispatch():
        assert (await store.get_run(run["id"]))["status"] == "queued"
        dispatched.set()

    monkeypatch.setattr(engine, "dispatch_queued", dispatch)
    poller = asyncio.create_task(engine._poll())
    try:
        await asyncio.wait_for(dispatched.wait(), timeout=2)
        assert not poller.done()
        assert len([entry for entry in await journal.entries(run["id"]) if entry["kind"] == "resume"]) == 1
    finally:
        poller.cancel()
        await asyncio.gather(poller, return_exceptions=True)
