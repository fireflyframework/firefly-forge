# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Live authority is rechecked after the runner capacity queue, before effects."""

import pytest
from diagnostic_legacy_fixtures import create_retained_diagnostic_run

IMAGE = "python@sha256:" + "a" * 64


async def queued_admission(store, tmp_path, change, *, dispatch=None, extra_grants=()):
    import asyncio

    from test_project_access import principal, seed

    from firefly_forge.agent_roles import ProjectRoles
    from firefly_forge.engine import Engine
    from firefly_forge.journal import Journal
    from firefly_forge.models import RunInput
    from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied, ProjectAccess
    from firefly_forge.sandbox import SandboxExecutor
    from firefly_forge.sandbox_models import SandboxInput
    from firefly_forge.workspace_tools import tool_catalog

    repo, projects = await seed(store, tmp_path)
    actor = principal()
    access = ProjectAccess(store)
    await access.set_membership(projects[0]["id"], actor, "developer")
    token = CURRENT_PRINCIPAL.set(actor)
    try:
        run = await create_retained_diagnostic_run(
            store,
            RunInput(
                repository_id=repo["id"],
                project_id=projects[0]["id"],
                component_id="service",
                brief="Observe queued workload",
                agent="agentic",
                sandbox=SandboxInput(mode="docker", image=IMAGE, context="owned-test", workspace_transport="archive"),
                roles=ProjectRoles(allowed_tools={"implementer": ["execute_command", *extra_grants]}),
            ),
            prepared_tools=tool_catalog({tool: True for tool in ["execute_command", *extra_grants]}),
        )
    finally:
        CURRENT_PRINCIPAL.reset(token)
    workspace = tmp_path / "source"
    (workspace / "workload.py").write_text("print('must not execute after revoke or cancel')\n")
    await store.acquire_ownership()
    journal = Journal(store)
    await journal.ensure_plan(run["id"])
    await journal.intent(run["id"], "implement")
    runner = SandboxExecutor(store, journal)
    runner.transfer_image = IMAGE
    reached, release = asyncio.Event(), asyncio.Event()
    calls = []

    class Capacity:
        async def __aenter__(self):
            reached.set()
            await release.wait()

        async def __aexit__(self, *args):
            pass

    runner._capacity = Capacity()

    async def forbidden(*args, **kwargs):
        calls.append(args)
        raise AssertionError("Stale authority crossed the Docker effect boundary")

    runner._docker = forbidden
    engine = Engine(store, tmp_path / "jobs", journal=journal)
    engine.sandbox = runner
    operation = (
        dispatch(engine, run, workspace)
        if dispatch
        else engine._project_command(run["id"], run, workspace, ["python3", "workload.py"], 10, "implement")
    )
    task = asyncio.create_task(operation)
    await asyncio.wait_for(reached.wait(), 3)
    if change == "revoke":
        await access.set_membership(projects[0]["id"], actor, None)
    else:
        await store.update(run["id"], cancel_request={"status": "requested"})
    before = await journal.entries(run["id"])
    release.set()
    with pytest.raises(AccessDenied if change == "revoke" else asyncio.CancelledError):
        await task
    assert calls == []
    assert await journal.sandbox_executions(run["id"]) == []
    assert await journal.entries(run["id"]) == before
    return run, journal, runner


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["revoke", "cancel"])
async def test_command_rechecks_authority_and_cancellation_after_capacity_wait(store, tmp_path, change):
    await queued_admission(store, tmp_path, change)


@pytest.mark.asyncio
async def test_denied_cached_command_preserves_known_receipts_and_cleanup_can_still_drain(store, tmp_path):
    import uuid

    from firefly_forge.process import ProcessResult
    from firefly_forge.project_access import AccessDenied
    from firefly_forge.sandbox_models import SandboxInput

    run, journal, runner = await queued_admission(store, tmp_path, "revoke")
    execution_id = uuid.uuid4().hex
    payload = {
        "parent_attempt": 1,
        "ordinal": 1,
        "container_name": "forge-sandbox-" + execution_id,
        "snapshot_digest": run["sandbox_snapshot"]["digest"],
        "request_digest": "a" * 64,
    }
    for effect in ["create", "start"]:
        await journal.sandbox_effect_intent(run["id"], "implement", execution_id, effect=effect, payload=payload)
        await journal.sandbox_effect_receipt(
            run["id"],
            "implement",
            execution_id,
            effect=effect,
            evidence={"exit_code": 7, "output": "retained known result"}
            if effect == "start"
            else {"container_id": "owned-fixture"},
        )
    before = await journal.entries(run["id"])
    with pytest.raises(AccessDenied):
        await runner.execute(
            run["id"], tmp_path / "source", tmp_path / "source", ["python3", "workload.py"], 10, "implement", ordinal=1
        )
    assert await journal.entries(run["id"]) == before
    await store.update(run["id"], cancel_request={"status": "requested"})
    removed = []

    async def owned_container(config, name, labels):
        assert name in {payload["container_name"], payload["container_name"] + "-transfer"}
        return {"Id": "owned-fixture"}

    async def remove_only(config, argv, *args, **kwargs):
        assert argv[:4] == ["container", "rm", "--force", "--volumes"]
        removed.append(argv[4])
        return ProcessResult(argv, 0, "")

    runner._inspect = owned_container
    runner._docker = remove_only
    await runner._cleanup(
        SandboxInput.model_validate(run["sandbox_snapshot"]["configuration"]),
        run["id"],
        "implement",
        execution_id,
        payload,
        {},
    )
    assert set(removed) == {payload["container_name"], payload["container_name"] + "-transfer"}
    row = (await journal.sandbox_executions(run["id"]))[0]
    assert row["status"] == "removed" and row["effects"]["cleanup"]["status"] == "done"
    assert row["effects"]["start"]["evidence"] == {"exit_code": 7, "output": "retained known result"}
    after = await journal.entries(run["id"])
    assert after[: len(before)] == before and journal.verify_chain(after)
