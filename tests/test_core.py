# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
import asyncio
from pathlib import Path

import pytest
from pydantic import ValidationError

from firefly_forge.models import Command, RepositoryInput, RunInput


def test_credentials_and_unsafe_git_arguments_rejected():
    for url in ["https://user:secret@example.com/repo.git", "--upload-pack=evil", "file:///etc/passwd"]:
        with pytest.raises(ValidationError):
            RepositoryInput(name="demo", url=url)


def test_brief_and_commands_are_bounded():
    with pytest.raises(ValidationError):
        RunInput(repository_id="r", brief=" ")
    with pytest.raises(ValidationError):
        Command(argv=[], timeout=20)
    with pytest.raises(ValidationError):
        Command(argv=["python"], timeout=0)


async def test_claim_is_atomic_across_connections_and_history_persists(tmp_path: Path, store_factory):
    first, second = (await store_factory(tmp_path / "factory.db"), await store_factory(tmp_path / "factory.db"))
    repo = await first.add_repository(RepositoryInput(name="demo", url="https://github.com/example/demo.git"))
    run = await first.create_run(RunInput(repository_id=repo["id"], brief="Build an order service"))
    assert await second.claim(run["id"])
    assert not await first.claim(run["id"])
    await second.event(run["id"], "preparing", "Clone created")
    await first.finish(run["id"], "failed", error="Verification failed")
    reopened = await (await store_factory(tmp_path / "factory.db")).get_run(run["id"])
    assert reopened["status"] == "failed"
    assert reopened["events"][-1]["message"] == "Verification failed"
    assert any(event["message"] == "Clone created" for event in reopened["events"])


async def test_restart_marks_inflight_interrupted_and_keeps_queue(tmp_path: Path, store_factory):
    store = await store_factory(tmp_path / "factory.db")
    repo = await store.add_repository(RepositoryInput(name="demo", url="https://github.com/example/demo.git"))
    claimed = await store.create_run(RunInput(repository_id=repo["id"], brief="Create service"))
    queued = await store.create_run(RunInput(repository_id=repo["id"], brief="Create UI"))
    await store.claim(claimed["id"])
    assert await store.recover() == 1
    assert (await store.get_run(claimed["id"]))["status"] == "interrupted"
    assert (await store.get_run(queued["id"]))["status"] == "queued"


async def test_terminal_transition_and_event_are_atomic(tmp_path: Path, store_factory):
    first, second = (await store_factory(tmp_path / "factory.db"), await store_factory(tmp_path / "factory.db"))
    repo = await first.add_repository(RepositoryInput(name="demo", url="https://github.com/example/demo.git"))
    run = await first.create_run(RunInput(repository_id=repo["id"], brief="Create service"))
    await first.claim(run["id"])
    await asyncio.gather(first.finish(run["id"], "succeeded"), second.finish(run["id"], "cancelled"))
    result = await first.get_run(run["id"])
    terminal_events = [event for event in result["events"] if event["stage"] in {"succeeded", "cancelled"}]
    assert len(terminal_events) == 1
    assert terminal_events[0]["stage"] == result["status"]
