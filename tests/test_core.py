# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from pydantic import ValidationError

from firefly_forge.models import Command, RepositoryInput, RunInput
from firefly_forge.store import Store


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


def test_claim_is_atomic_across_connections_and_history_persists(tmp_path: Path):
    first, second = Store(tmp_path / "factory.db"), Store(tmp_path / "factory.db")
    repo = first.add_repository(RepositoryInput(name="demo", url="https://github.com/example/demo.git"))
    run = first.create_run(RunInput(repository_id=repo["id"], brief="Build an order service"))
    assert second.claim(run["id"])
    assert not first.claim(run["id"])
    second.event(run["id"], "preparing", "Clone created")
    first.finish(run["id"], "failed", error="Verification failed")
    reopened = Store(tmp_path / "factory.db").get_run(run["id"])
    assert reopened["status"] == "failed"
    assert reopened["events"][-1]["message"] == "Verification failed"
    assert any(event["message"] == "Clone created" for event in reopened["events"])


def test_restart_marks_inflight_interrupted_and_keeps_queue(tmp_path: Path):
    store = Store(tmp_path / "factory.db")
    repo = store.add_repository(RepositoryInput(name="demo", url="https://github.com/example/demo.git"))
    claimed = store.create_run(RunInput(repository_id=repo["id"], brief="Create service"))
    queued = store.create_run(RunInput(repository_id=repo["id"], brief="Create UI"))
    store.claim(claimed["id"])
    assert store.recover() == 1
    assert store.get_run(claimed["id"])["status"] == "interrupted"
    assert store.get_run(queued["id"])["status"] == "queued"


def test_terminal_transition_and_event_are_atomic(tmp_path: Path):
    first, second = Store(tmp_path / "factory.db"), Store(tmp_path / "factory.db")
    repo = first.add_repository(RepositoryInput(name="demo", url="https://github.com/example/demo.git"))
    run = first.create_run(RunInput(repository_id=repo["id"], brief="Create service"))
    first.claim(run["id"])
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda item: item[0].finish(run["id"], item[1]), [(first, "succeeded"), (second, "cancelled")]))
    result = first.get_run(run["id"])
    terminal_events = [event for event in result["events"] if event["stage"] in {"succeeded", "cancelled"}]
    assert len(terminal_events) == 1
    assert terminal_events[0]["stage"] == result["status"]
