# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Bounded factory execution and continuation of typed verification requests."""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Any

from pyfly.container import service

from firefly_forge.journal import Journal, digest
from firefly_forge.models import Command
from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied, ProjectAccess
from firefly_forge.sandbox_models import validate_sandbox_snapshot
from firefly_forge.store import Store
from firefly_forge.tool_broker import tool_policy_snapshot
from firefly_forge.verification_models import (
    VerificationAdoption,
    automatic_verification_grant,
    check_verification_authority,
    check_verification_cut,
    verification_provider,
    verification_scope,
    verification_source,
)
from firefly_forge.verification_source_preparation import (
    PreparedVerificationSource,
    current_verification_source,
    prepare_verification_source,
    verification_catalog_root,
    verification_source_scope,
)

Data = dict[str, Any]


def automatic_candidate(run: Data) -> bool:
    request = run.get("worker_request", {})
    state = run.get("verification_handoff", {})
    if state.get("request_id") != request.get("id"):
        state = {}
    if run.get("verification_refusal", {}).get("request_digest") == digest(request):
        return False
    if state.get("status") == "held" or run.get("status") not in {"paused", "needs_reconciliation"}:
        return False
    if state.get("status") == "completed" and request.get("response_source") == "factory_verification":
        return True
    snapshot = run.get("verification_adoption", {}).get("policy_snapshot") or run.get("tool_policy_snapshot")
    return bool(
        request.get("kind") == "verification"
        and request.get("status") == "pending"
        and request.get("task_id") == "implement"
        and run.get("commands")
        and type(request.get("attempt")) is int
        and 1 <= request["attempt"] < 4
        and automatic_verification_grant(snapshot) is not None
    )


def verification_intent(store: Store, run: Data, workspace: Path, entries: list[Data], executions: list[Data]) -> Data:
    request = run.get("worker_request", {})
    if type(request.get("attempt")) is not int or not 1 <= request["attempt"] < 4:
        raise ValueError("Automatic verification cannot expand the implementation attempt limit")
    if not automatic_candidate(run):
        raise ValueError("This worker request has no frozen automatic verification grant")
    adoption = run.get("verification_adoption", {})
    snapshot = adoption.get("policy_snapshot") or run.get("tool_policy_snapshot")
    grant = automatic_verification_grant(snapshot)
    assert grant is not None
    assert snapshot is not None
    current = tool_policy_snapshot(store.configuration)
    if current["digest"] != snapshot["digest"]:
        raise ValueError("Current automatic verification policy changed")
    commands = [Command.model_validate(command).model_dump() for command in run["commands"]]
    config = validate_sandbox_snapshot(run["sandbox_snapshot"])
    used = sum(entry["kind"] == "verification_handoff_intent" for entry in entries)
    if used >= grant.max_continuations:
        raise ValueError("Automatic verification continuation budget exhausted")
    ordinal = max(
        (
            row["ordinal"]
            for row in executions
            if row["task_id"] == "implement" and row["parent_attempt"] == request["attempt"]
        ),
        default=0,
    )
    if len(executions) + len(commands) > config.command_limit or ordinal + len(commands) > config.command_limit:
        raise ValueError("Frozen verification command budget is exhausted")
    selection = VerificationAdoption.model_validate(adoption["request"]) if adoption else None
    intent = {
        "version": 1,
        "id": uuid.uuid4().hex,
        "request_id": request["id"],
        "request_digest": digest(request),
        "source_digest": verification_source(workspace),
        "commands_digest": digest({"commands": commands}),
        "policy_digest": current["digest"],
        "provider_digest": verification_provider(store, run, selection.agent if selection else None),
        "scope_digest": verification_scope(run),
        "continuation": used + 1,
        "max_continuations": grant.max_continuations,
        "commands_count": len(commands),
        "parent_attempt": request["attempt"],
        "parent_round": int(run.get("implementation_rounds", request["attempt"])),
        "ordinals": list(range(ordinal + 1, ordinal + 1 + len(commands))),
        "timeouts": [min(command["timeout"], config.timeout) for command in commands],
        "sandbox_digest": run["sandbox_snapshot"]["digest"],
        "workspace": str(workspace.resolve()),
        "cwd": (run.get("component_snapshot") or {}).get("path", "."),
    }
    if selection:
        expected = {
            "pending_request_digest": intent["request_digest"],
            **{key: intent[key] for key in ("source_digest", "commands_digest", "policy_digest", "provider_digest")},
        }
        if any(getattr(selection, key) != value for key, value in expected.items()):
            raise ValueError("Explicit verification adoption cut changed")
        intent["agent"] = selection.agent
        intent["adoption_request_id"] = selection.request_id
    return intent


@service
class VerificationHandoffs:
    def __init__(self, store: Store, journal: Journal, engine: Any) -> None:
        self.store, self.journal, self.engine = store, journal, engine

    async def _operator(self, run_id: str) -> Data:
        actor = CURRENT_PRINCIPAL.get()
        if actor is None or actor.authentication_method == "scm_mapping":
            raise AccessDenied("An authenticated native operator is required for verification adoption")
        run = await self.store.get_run(run_id)
        await self.journal._worker_authority(run, actor, writing=True)
        await ProjectAccess(self.store).authorize_execution(run)
        return run

    async def candidates(self, run_id: str, agent: str) -> Data:
        from firefly_forge.checkpoint_mode_runtime import mode_execution_scope

        async with mode_execution_scope(self.journal, run_id):
            return await self._candidates(run_id, agent)

    async def _candidates(self, run_id: str, agent: str) -> Data:
        if agent not in {"codex", "claude", "bridge", "agentic"}:
            raise ValueError("Choose an explicit configured coding provider")
        run = await self._operator(run_id)
        request = run.get("worker_request", {})
        if (
            run["status"] != "paused"
            or request.get("kind") != "verification"
            or request.get("status") != "pending"
            or request.get("task_id") != "implement"
        ):
            raise ValueError("Only a pending native implementation verification request can be adopted")
        policy = tool_policy_snapshot(self.store.configuration)
        if automatic_verification_grant(policy) is None or not run.get("commands"):
            raise ValueError("Automatic verification requires current policy and frozen approved commands")
        ProjectAccess.require_provider_isolation(run, agent)
        return {
            "version": 1,
            "run_id": run_id,
            "worker_request_id": request["id"],
            "pending_request_digest": digest(request),
            "source_digest": verification_source(self.engine.workspaces / run_id),
            "commands_digest": digest({"commands": run["commands"]}),
            "policy_digest": policy["digest"],
            "provider_digest": verification_provider(self.store, run, agent),
            "agent": agent,
            "commands_count": len(run["commands"]),
            "max_continuations": policy["settings"]["automatic_verification"]["max_continuations"],
        }

    async def adoption(self, run_id: str, request_id: str) -> Data:
        from firefly_forge.store import identifier

        identifier(request_id)
        await self._operator(run_id)
        entries = await self.journal.entries(run_id)
        receipt = next(
            (
                entry["payload"]
                for entry in entries
                if entry["kind"] == "verification_handoff_adoption"
                and entry["payload"]["request"]["request_id"] == request_id
            ),
            None,
        )
        if receipt is None:
            raise KeyError("Verification adoption not found")
        return dict(receipt)

    async def adopt(self, run_id: str, body: VerificationAdoption) -> Data:
        from firefly_forge.checkpoint_mode_runtime import mode_execution_scope

        async with mode_execution_scope(self.journal, run_id):
            return await self._adopt(run_id, body)

    async def _adopt(self, run_id: str, body: VerificationAdoption) -> Data:
        run = await self._operator(run_id)
        entries = await self.journal.entries(run_id)
        existing = next(
            (
                entry["payload"]
                for entry in entries
                if entry["kind"] == "verification_handoff_adoption"
                and entry["payload"]["request"]["request_id"] == body.request_id
            ),
            None,
        )
        if existing:
            return await self.journal.verification_adopt(
                run_id, body.model_dump(), existing["policy_snapshot"], check_current=lambda current: None
            )
        candidate = await self.candidates(run_id, body.agent)
        if any(
            getattr(body, key) != candidate[key]
            for key in (
                "pending_request_digest",
                "source_digest",
                "commands_digest",
                "policy_digest",
                "provider_digest",
            )
        ):
            raise ValueError("Verification adoption differs from the observed source, request or current configuration")
        policy = tool_policy_snapshot(self.store.configuration)
        temporary = {
            **run,
            "verification_adoption": {"request": body.model_dump(), "policy_snapshot": policy},
            "verification_refusal": {},
            "verification_handoff": {},
        }
        intent = verification_intent(
            self.store,
            temporary,
            self.engine.workspaces / run_id,
            entries,
            await self.journal.sandbox_executions(run_id),
        )
        return await self.journal.verification_adopt(
            run_id,
            body.model_dump(),
            policy,
            check_current=lambda current: check_verification_cut(self.store, current, intent),
        )

    async def ready(self) -> list[str]:
        async with self.store._unit():
            rows = await self.store.run_records.find_verification_handoffs()
            result = []
            for row in rows:
                try:
                    if automatic_candidate(row.data):
                        result.append(row.id.hex)
                except ValueError:
                    continue
            return result

    async def execute(self, run_id: str) -> None:
        from firefly_forge.checkpoint_mode_runtime import mode_execution_scope

        # The active repository slot owns implementation; this existing lock excludes checkpoint writes.
        async with self.engine.publication_lock, mode_execution_scope(self.journal, run_id):
            await self._execute(run_id)

    async def _execute(self, run_id: str) -> None:
        intent: Data | None = None
        run: Data | None = None
        try:
            await self.engine.assert_ownership()
            run = await self.store.get_run(run_id)
            entries = await self.journal.entries(run_id)
            retained = run.get("verification_handoff", {})
            existing = next(
                (
                    entry["payload"]
                    for entry in entries
                    if entry["kind"] == "verification_handoff_intent"
                    and entry["payload"]["id"] == retained.get("id")
                    and entry["payload"]["request_id"] == run.get("worker_request", {}).get("id")
                ),
                None,
            )
            workspace = self.engine.workspaces / run_id

            def scope(current: Data) -> str:
                return digest(
                    {
                        "scope": verification_scope(current),
                        "request": current.get("worker_request"),
                        "commands": current.get("commands"),
                        "adoption": current.get("verification_adoption"),
                        "handoff": current.get("verification_handoff"),
                        "configuration": current.get("config_revision"),
                    }
                )

            initial = scope(run)

            async def initial_current() -> None:
                await self.engine.assert_ownership()
                current = await self.store.get_run(run_id)
                await ProjectAccess(self.store).authorize_execution(current)
                await self.store.assert_campaign_current(current)
                if (current.get("cancel_request") or {}).get("status") == "requested":
                    raise asyncio.CancelledError
                if scope(current) != initial:
                    raise ValueError("Verification scope changed during source preparation")

            prepared = await prepare_verification_source(
                workspace,
                verification_catalog_root(self.store.path, run_id),
                budget=self.journal.source_catalog_budget,
                assert_current=initial_current,
            )
            with verification_source_scope(prepared):
                intent = existing or verification_intent(
                    self.store, run, workspace, entries, await self.journal.sandbox_executions(run_id)
                )
                if existing is None or retained.get("status") != "completed":
                    await self._authorize(run_id, intent)
                    await self.journal.verification_begin(
                        run_id,
                        intent,
                        check_current=lambda current: check_verification_cut(self.store, current, intent),
                    )
                    for index, command in enumerate(run["commands"]):
                        await self._authorize(run_id, intent)
                        directory = self.engine.component_directory(run, workspace)
                        await self.engine.sandbox.execute(
                            run_id,
                            workspace,
                            directory,
                            command["argv"],
                            command["timeout"],
                            "implement",
                            ordinal=intent["ordinals"][index],
                            git_root=workspace,
                        )
                        await self._authorize(run_id, intent)
                        await self.journal.verification_command(
                            run_id,
                            intent,
                            index,
                            check_current=lambda current: check_verification_cut(self.store, current, intent),
                        )
                    await self._authorize(run_id, intent)
                    await self.journal.verification_finish(
                        run_id,
                        intent,
                        check_current=lambda current: check_verification_cut(self.store, current, intent),
                    )
                await self._authorize(run_id, intent)
                await self.journal.resume(
                    run_id,
                    agent=intent.get("agent"),
                    verification_handoff_id=intent["id"],
                    expected_provider_digest=intent["provider_digest"],
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            await self.engine.assert_ownership()
            if intent:
                await self.journal.verification_hold(run_id, intent["id"])
            elif run:
                await self.journal.verification_refuse(run_id, digest(run.get("worker_request", {})))

    async def _authorize(self, run_id: str, intent: Data) -> None:
        async def current() -> None:
            await self.engine.assert_ownership()
            run = await self.store.get_run(run_id)
            await ProjectAccess(self.store).authorize_execution(run)
            await self.store.assert_campaign_current(run)
            check_verification_authority(self.store, run, intent)

        prepared = current_verification_source(Path(intent["workspace"]))
        if prepared is not None:
            await prepared.revalidate(assert_current=current)
        await current()
        check_verification_cut(self.store, await self.store.get_run(run_id), intent)


async def prepare_journal_verification_source(
    journal: Journal, run_id: str, *, provider: bool
) -> PreparedVerificationSource | None:
    """Prepare outside the final native writer unit, including a later provider task."""
    from pyfly.data.transaction import current_unit_of_work

    if current_unit_of_work() is not None:
        raise ValueError("Verification source preparation must run outside a caller transaction")
    store = journal.store
    async with store._unit():
        run = await store.get_run(run_id)
        state = run.get("verification_handoff", {})
        if provider:
            response = run.get("worker_response", {})
            if response.get("source") != "factory_verification":
                return None
            wanted = response.get("verification_handoff_id")
        else:
            if state.get("status") != "running":
                return None
            wanted = state.get("id")
        rows = await journal.records.find_for_run(uuid.UUID(run_id))
        intent = next(
            (
                row.data["payload"]
                for row in rows
                if row.kind == "verification_handoff_intent" and row.idempotency_key == wanted
            ),
            None,
        )
        if intent is None:
            raise ValueError("Verification source requires its immutable native intent")
        if provider and int(run.get("implementation_rounds", 0)) > intent["parent_round"]:
            return None
    workspace = Path(intent["workspace"])

    async def current() -> None:
        await store.assert_ownership()
        current_run = await store.get_run(run_id)
        await ProjectAccess(store).authorize_execution(current_run)
        await store.assert_campaign_current(current_run)
        check_verification_authority(store, current_run, intent)

    prepared = current_verification_source(workspace)
    if prepared is None:
        prepared = await prepare_verification_source(
            workspace,
            verification_catalog_root(store.path, run_id),
            budget=journal.source_catalog_budget,
            assert_current=current,
        )
    else:
        await prepared.revalidate(assert_current=current)
    check_verification_cut(store, await store.get_run(run_id), intent, prepared=prepared)
    return prepared
