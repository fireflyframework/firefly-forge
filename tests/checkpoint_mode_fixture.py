# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Controlled memory setup using the actual native verification and command producers."""

from types import SimpleNamespace
from uuid import uuid4

from firefly_forge.agent_roles import WorkerFeedback
from firefly_forge.process import ProcessResult
from firefly_forge.sandbox import SandboxExecutor
from firefly_forge.sandbox_models import SandboxInput, capture_sandbox
from firefly_forge.tool_broker import FactoryToolPolicyInput, tool_policy_snapshot
from firefly_forge.verification_handoffs import verification_intent


async def missing_mode_failure(store, journal, workspace, revision, settings):
    run_id = store.run.id.hex
    store.configuration._write(store.configuration.path, {"bridge": settings})
    policy = FactoryToolPolicyInput(automatic_verification={"enabled": True, "max_continuations": 2})
    store.configuration._write(
        store.configuration.directory / "tool-policy.json",
        {"revision": uuid4().hex, "settings": policy.model_dump(mode="json")},
    )
    store.run.status = "developing"
    store.run.data.update(
        id=run_id,
        status="developing",
        error=None,
        config_revision=revision,
        agent="bridge",
        active_task="implement",
        attempt=1,
        implementation_rounds=1,
        implementation_pending=False,
        verification_handoff={},
        worker_response=None,
        sandbox_snapshot=capture_sandbox(SandboxInput(mode="host", command_limit=22)),
        tool_policy_snapshot=tool_policy_snapshot(store.configuration),
    )
    task = await journal._task(run_id, "implement")
    task.status, task.attempt = "queued", 1
    original = await journal.intent(run_id, "implement", 1, {"config_revision": revision})
    task.status = "running"
    task.data = {**task.data, "idempotency_key": original["idempotency_key"]}
    feedback = WorkerFeedback.model_validate(
        {
            "id": "controlled-verification-feedback",
            "role_id": "implementer",
            "task_id": "implement",
            "attempt": 1,
            "disposition": "blocked",
            "summary": "Configured checks are required",
            "request": {
                "id": "controlled-verification-question",
                "kind": "verification",
                "question": "Can the approved checks run?",
                "context": "Controlled native predecessor",
            },
        }
    )
    await journal.worker_feedback(run_id, feedback)
    intent = verification_intent(
        store,
        await store.get_run(run_id),
        workspace,
        await journal.entries(run_id),
        await journal.sandbox_executions(run_id),
    )
    await journal.verification_begin(run_id, intent, check_current=lambda _: None)

    class Backend:
        async def run(self, configuration, argv, cwd, timeout, output_limit):
            return ProcessResult(
                argv, 1 if argv == store.run.data["commands"][0]["argv"] else 0, "Known controlled check"
            )

    executor = SandboxExecutor(store, journal, SimpleNamespace(select=lambda _: Backend()))
    for index, command in enumerate(store.run.data["commands"]):
        await executor.execute(
            run_id,
            workspace,
            workspace,
            command["argv"],
            command["timeout"],
            "implement",
            ordinal=intent["ordinals"][index],
        )
        await journal.verification_command(run_id, intent, index, check_current=lambda _: None)
    await journal.verification_finish(run_id, intent, check_current=lambda _: None)
    await journal._append(
        run_id,
        "handoff",
        uuid4().hex,
        "handoff",
        {"automatic": True, "verification_handoff_id": intent["id"], "agent": "bridge", "config_revision": revision},
    )
    task.status, task.attempt = "queued", 2
    store.run.data.update(attempt=2, implementation_pending=True)
    await journal.intent(run_id, "implement", 2, {"config_revision": revision})
    await journal.verification_provider_admission(run_id)
    store.run.status = "failed"
    store.run.data.update(status="failed", error="native_agentic_checkpoint_wal_required", active_task="implement")
    store.configuration._write(
        store.configuration.path,
        {
            "bridge": settings
            | {"FORGE_AGENTIC_CHECKPOINTS": "required", "FORGE_BRIDGE_COMPLETION_RECOVERY": "required"}
        },
    )
    return intent
