# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Frozen same-provider verification and local readiness admission, without remote calls."""
# ruff: noqa: F811

import copy
from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import uuid4

import pytest
from test_checkpoint_mode_adoption_host import mode_case  # noqa: F401
from test_sandbox_command_ordinals_host import command_case  # noqa: F401

from firefly_forge.journal import digest
from firefly_forge.verification_models import VerificationAdoption, verification_provider, verification_source


def test_explicit_same_provider_verification_uses_the_frozen_selection(mode_case):
    c = mode_case
    run = c.store.run.data
    frozen = c.store.configuration.load_snapshot(run["config_revision"])
    c.store.configuration._write(c.store.configuration.path, {"bridge": {}})
    assert verification_provider(c.store, run, "bridge") == digest({"agent": "bridge", "settings": frozen})


async def test_unready_explicit_bridge_refuses_before_entering_a_resume_writer(mode_case, monkeypatch):
    c = mode_case
    c.store.run.status = "paused"
    c.store.run.data.update(status="paused", error=None)
    c.store.configuration._write(c.store.configuration.path, {"bridge": {}})
    before = copy.deepcopy(c.store.run.data)
    entries = await c.journal.entries(c.run_id)

    @asynccontextmanager
    async def forbidden_writer():
        raise AssertionError("Unready provider entered the native resume writer")
        yield

    with monkeypatch.context() as scoped:
        scoped.setattr(c.store, "_unit", forbidden_writer)
        with pytest.raises(ValueError, match="configured.*(endpoint|credential)|Bridge.*(URL|token)"):
            await c.journal.resume(c.run_id, agent="bridge")
    assert c.store.run.data == before
    assert await c.journal.entries(c.run_id) == entries


def test_an_explicit_different_provider_still_uses_its_current_selection(mode_case):
    c = mode_case
    selected = c.store.configuration.resolve("codex")
    assert verification_provider(c.store, c.store.run.data, "codex") == digest({"agent": "codex", "settings": selected})


@pytest.mark.parametrize("provider", ["bridge", "agentic", "codex", "claude"])
async def test_actual_same_provider_resume_preserves_named_selection_and_revision(mode_case, provider):
    from firefly_forge.agent_roles import WorkerFeedback
    from firefly_forge.process import ProcessResult
    from firefly_forge.sandbox import SandboxExecutor
    from firefly_forge.tool_broker import tool_policy_snapshot
    from firefly_forge.verification_handoffs import verification_intent

    c = mode_case
    if provider != "bridge":
        if provider == "agentic":
            c.store.configuration._write(
                c.store.configuration.path, {"agentic": {"FORGE_AGENTIC_API_KEY": "synthetic-selection-key"}}
            )
        c.store.run.data.update(c.store.configuration.snapshot(provider))
    c.store.records.rows.clear()
    c.store.sandboxes.rows.clear()
    task = c.store.tasks.rows[0]
    task.status, task.attempt, task.data = "queued", 1, {}
    c.store.run.status = "developing"
    c.store.run.data.update(
        status="developing",
        error=None,
        attempt=1,
        implementation_pending=False,
        agent=provider,
        verification_handoff={},
        worker_request={},
        worker_response={},
        model_profile_id="original-maintenance",
        model_selection={"profile_id": "original-maintenance", "config_revision": c.store.run.data["config_revision"]},
    )
    await c.journal.intent(c.run_id, "implement", 1)
    await c.journal.worker_feedback(
        c.run_id,
        WorkerFeedback.model_validate(
            {
                "id": "selection-feedback",
                "role_id": "implementer",
                "task_id": "implement",
                "attempt": 1,
                "disposition": "blocked",
                "summary": "Run configured checks",
                "request": {
                    "id": "selection-verification",
                    "kind": "verification",
                    "question": "Run checks?",
                    "context": "Frozen checks",
                },
            }
        ),
    )
    run = await c.store.get_run(c.run_id)
    adoption = VerificationAdoption(
        request_id=uuid4().hex,
        pending_request_digest=digest(run["worker_request"]),
        source_digest=verification_source(c.workspace),
        commands_digest=digest({"commands": run["commands"]}),
        policy_digest=tool_policy_snapshot(c.store.configuration)["digest"],
        provider_digest=verification_provider(c.store, run, provider),
        agent=provider,
    )
    c.store.run.data["verification_adoption"] = {
        "request": adoption.model_dump(),
        "policy_snapshot": tool_policy_snapshot(c.store.configuration),
    }
    intent = verification_intent(c.store, c.store.run.data, c.workspace, await c.journal.entries(c.run_id), [])
    await c.journal.verification_begin(c.run_id, intent, check_current=lambda _: None)

    class Backend:
        async def run(self, configuration, argv, cwd, timeout, limit):
            return ProcessResult(argv, 1, "Known RED")

    executor = SandboxExecutor(c.store, c.journal, SimpleNamespace(select=lambda _: Backend()))
    for i, command in enumerate(run["commands"]):
        await executor.execute(
            c.run_id,
            c.workspace,
            c.workspace,
            command["argv"],
            command["timeout"],
            "implement",
            ordinal=intent["ordinals"][i],
        )
        await c.journal.verification_command(c.run_id, intent, i, check_current=lambda _: None)
    await c.journal.verification_finish(c.run_id, intent, check_current=lambda _: None)
    before = copy.deepcopy(c.store.run.data)
    c.store.configuration._write(c.store.configuration.path, {provider: {}})
    resumed = await c.journal.resume(
        c.run_id,
        agent=provider,
        verification_handoff_id=intent["id"],
        expected_provider_digest=intent["provider_digest"],
    )
    assert resumed["status"] == "queued"
    for key in ("config_revision", "model_selection", "model_profile_id", "repair_budget", "implementation_rounds"):
        assert resumed.get(key) == before.get(key)
    handoff = (await c.journal.entries(c.run_id))[-1]
    assert handoff["kind"] == "handoff" and handoff["payload"]["selection_mode"] == "frozen"
