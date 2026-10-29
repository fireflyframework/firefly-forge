# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""PostgreSQL duration custody with fictional retained commands; no Docker dispatch."""

import copy
import uuid

import pytest

from firefly_forge.journal import Journal, digest
from firefly_forge.models import RepositoryInput, RunInput
from firefly_forge.sandbox import SandboxExecutor, pack_workspace
from firefly_forge.sandbox_models import SandboxInput
from firefly_forge.work_duration_models import WorkDurationPolicyV2

IMAGE = "python@sha256:" + "a" * 64


async def test_new_uncapped_docker_diagnostics_still_refuse_before_any_run_or_effect(store):
    repository = await store.add_repository(
        RepositoryInput(name="Fictional diagnostic", url="https://example.invalid/test.git")
    )
    before = await store.runs()
    with pytest.raises(ValueError, match="owner watchdog"):
        await store.create_run(
            RunInput(
                repository_id=repository["id"],
                brief="New uncapped diagnostic admission",
                sandbox=SandboxInput(
                    mode="docker",
                    image=IMAGE,
                    context="fictional",
                    workspace_transport="archive",
                    work_duration=WorkDurationPolicyV2(),
                ),
            )
        )
    assert await store.runs() == before


async def test_new_finite_policy_keeps_owned_task_and_exact_retained_command_without_reexecution(store, tmp_path):
    await store.acquire_ownership()
    repository = await store.add_repository(
        RepositoryInput(name="Fictional diagnostic", url="https://example.invalid/test.git")
    )
    policy = WorkDurationPolicyV2(timeout_seconds=120)
    run = await store.create_run(
        RunInput(
            repository_id=repository["id"],
            brief="New finite diagnostic admission",
            sandbox=SandboxInput(
                mode="docker", image=IMAGE, context="fictional", workspace_transport="archive", work_duration=policy
            ),
        )
    )
    assert (
        run["work_duration"]
        == run["sandbox_snapshot"]["configuration"]["work_duration"]
        == policy.model_dump(mode="json")
    )
    journal = Journal(store)
    intent = await journal.intent(run["id"], "implement")
    await store.update(run["id"], status="developing", attempt=1)
    workspace = tmp_path / "source"
    workspace.mkdir()
    (workspace / "workload.py").write_text("raise SystemExit(7)\n")
    argv = ["python3", "workload.py"]
    request = {
        "browser": False,
        "output_limit": 1000000,
        "input_sha256": None,
        "argv": argv,
        "cwd": ".",
        "workspace": str(workspace.resolve()),
        "configuration": copy.deepcopy(run["sandbox_snapshot"]["configuration"]),
        "timeout": 10,
        "git_root": None,
        "transfer_image": IMAGE,
        "workspace_digest": pack_workspace(workspace, 67108864)[1],
    }
    execution = uuid.uuid5(uuid.NAMESPACE_URL, f"forge:sandbox:{run['id']}:implement:1:1").hex
    payload = {
        "parent_attempt": 1,
        "ordinal": 1,
        "container_name": "forge-sandbox-" + execution,
        "snapshot_digest": run["sandbox_snapshot"]["digest"],
        "request_digest": digest(request),
        "request": request,
    }
    for effect in ["create", "start", "cleanup"]:
        await journal.sandbox_effect_intent(run["id"], "implement", execution, effect=effect, payload=payload)
        await journal.sandbox_effect_receipt(
            run["id"],
            "implement",
            execution,
            effect=effect,
            evidence={"exit_code": 7, "output": "Known fictional diagnostic outcome"}
            if effect == "start"
            else {"container_absent": True},
        )
    before = await journal.entries(run["id"])
    runner = SandboxExecutor(store, journal)
    runner.transfer_image = IMAGE
    calls = []

    async def forbidden(*args, **kwargs):
        calls.append(args)
        raise AssertionError("Known diagnostic commands cannot execute again")

    runner._docker = forbidden
    observations = []
    result = await runner.execute(
        run["id"],
        workspace,
        workspace,
        argv,
        10,
        "implement",
        ordinal=1,
        _diagnostic_snapshot=run["sandbox_snapshot"]["digest"],
        _receipt_sink=observations.append,
    )
    assert result.exit_code == 7 and result.output == "Known fictional diagnostic outcome"
    assert calls == [] and len(observations) == 1 and observations[0]["request_digest"] == digest(request)
    assert await journal.entries(run["id"]) == before and Journal.verify_chain(before)
    task = next(task for task in (await journal.plan(run["id"]))["tasks"] if task["id"] == "implement")
    assert task["attempt"] == 1 and task["idempotency_key"] == intent["idempotency_key"] and task["status"] == "running"
    assert len(await journal.sandbox_executions(run["id"])) == 1
    assert (await store.get_run(run["id"]))["work_duration"] == policy.model_dump(mode="json")
