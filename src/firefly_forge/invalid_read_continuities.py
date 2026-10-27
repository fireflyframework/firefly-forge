# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Native same-attempt continuity with explicit current ownership-lease consent."""

from __future__ import annotations

import copy
from contextlib import nullcontext
from typing import Any

from pyfly.container import service

from firefly_forge.agent_restores import AgentRestores
from firefly_forge.agentic_checkpoint_invocation import runtime_contract, workspace_cut
from firefly_forge.bridge_completions import _outside_unit
from firefly_forge.checkpoint_mode_adoptions import CheckpointModeAdoptions
from firefly_forge.checkpoint_mode_runtime import effective_revision, mode_adoption_scope, prepare_current_mode
from firefly_forge.configuration import validate_checkpoints
from firefly_forge.invalid_read_continuity import (
    LEGACY_RUNTIME_CONTRACT,
    native_tool_custody,
    prepare_invalid_read,
    reconstruct_and_migrate,
    registered_legacy_runtime,
)
from firefly_forge.invalid_read_lease import ContinuityLeasePolicyV1, policy_snapshot
from firefly_forge.invalid_read_tickets import InvalidReadTickets
from firefly_forge.journal import Journal, digest
from firefly_forge.known_worker_completions import _entries, _task
from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied, ProjectAccess
from firefly_forge.store import Store, identifier, prepared_credential_rejection


def current_profile_seal() -> dict[str, Any]:
    from firefly_forge.source_edit_qualification import LOADER_PROFILE, REGISTRY_VERSION, profile_seal
    from firefly_forge.source_edit_supported_profiles import PROFILES

    matches = [p for p in PROFILES if p.version == REGISTRY_VERSION and p.loader_profile == LOADER_PROFILE]
    if len(matches) != 1:
        raise ValueError("The current native profile is not registered")
    seal = profile_seal(matches[0])
    return {"profile_id": matches[0].profile_id, "digest": digest(seal)}


@service
class InvalidReadContinuities(InvalidReadTickets):
    def __init__(self, store: Store, journal: Journal, engine: Any, requests: Any = None) -> None:
        self.store, self.journal, self.engine = store, journal, engine
        self.requests = requests or getattr(store, "execution_requests", None)
        self.restores = AgentRestores(store, self.requests, engine)
        self.modes = CheckpointModeAdoptions(store, journal, engine)

    async def _actor(self, run_id: str) -> Any:
        actor = CURRENT_PRINCIPAL.get()
        if actor is None:
            raise AccessDenied("Sign in before reviewing same-attempt continuity")
        run = await self.restores.completions._reader(run_id, actor, execute=True)
        await self.restores.access(run, ProjectAccess.actor_snapshot(actor))
        if CURRENT_PRINCIPAL.get() != actor:
            raise AccessDenied("Continuity operator changed")
        return actor

    async def _read(self, run_id: str) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
        await self._actor(run_id)
        active = self.engine.active.get(run_id)
        if self.engine.stopping or (active is not None and not active.done()):
            raise ValueError("Original execution must be idle during continuity admission")
        async with self.store._unit():
            run = await self.store.get_run(run_id)
            task = _task(await self.journal._task(run_id, "implement"))
            entries = _entries(await self.journal.records.find_for_run(identifier(run_id)), run_id)
        if run.get("cancel_request", {}).get("status") == "requested":
            raise ValueError("Cancelled execution cannot receive a continuity lease")
        return run, task, entries

    async def _command_cut(self, run: dict[str, Any], task: dict[str, Any], entries: list[dict[str, Any]]) -> str:
        from firefly_forge.sandbox_command_slots import _canonical, _complete

        async with self.store._unit():
            rows = await self.journal.sandboxes.for_run(identifier(run["id"]))
        retained = {
            e["payload"].get("execution_id")
            for e in entries
            if e["kind"] in {"sandbox_intent", "sandbox_receipt", "sandbox_reconcile"}
        }
        if retained != {row.id.hex for row in rows}:
            raise ValueError("Command custody has a missing native execution")
        values = {row.id.hex: _canonical(row, entries, run["id"]) for row in rows}
        if all(_complete(row, values[row.id.hex]) for row in rows):
            return digest({"unresolved_commands": []})
        acknowledgements, verification = await self.modes._command_custody(run, task, entries)
        return digest({"acknowledgements": acknowledgements, "verification": verification})

    async def _native_observation_tools(
        self, run_id: str, entries: list[dict[str, Any]], *, before_sequence: int | None = None
    ) -> list[dict[str, Any]]:
        broker = self.engine.tool_broker
        if broker is None:
            raise ValueError("Unavailable observations require the actual native tool repository")
        async with self.store._unit():
            records = await broker.records.for_run(identifier(run_id))
            rows = [
                dict(
                    id=row.id.hex,
                    run_id=row.run_id.hex,
                    task_id=row.task_id,
                    tool=row.tool,
                    correlation_id=row.correlation_id,
                    status=row.status,
                    data=copy.deepcopy(row.data),
                )
                for row in records
            ]
        native_tool_custody(run_id, entries, rows)
        if before_sequence is not None:
            entries = [e for e in entries if e["sequence"] < before_sequence]
            keys = {e["idempotency_key"] for e in entries if e["kind"] == "tool_intent"}
            rows = [row for row in rows if row["correlation_id"] in keys]
            native_tool_custody(run_id, entries, rows)
        return rows

    async def _prepare(self, run_id: str) -> dict[str, Any]:
        _outside_unit()
        run_id = identifier(run_id).hex
        actor = await self._actor(run_id)
        with prepared_credential_rejection(self.store):
            run, task, entries = await self._read(run_id)
            generation = self.store.configuration.settings_generation
            policy = policy_snapshot(self.store.configuration)
            configured = ContinuityLeasePolicyV1.model_validate(policy["settings"])
            allow_observations = configured.enabled and configured.allow_unavailable_source_observations
            tool_rows = (
                await self._native_observation_tools(run_id, entries)
                if allow_observations and self.engine.tool_broker is not None
                else None
            )
            from firefly_forge.invalid_read_epochs import epoch_scope, epoch_snapshot

            accepted_runtime = registered_legacy_runtime(entries, run_id, primary=LEGACY_RUNTIME_CONTRACT)
            epoch = epoch_snapshot(self.store, run, entries, legacy_runtime_contract=accepted_runtime)
            with epoch_scope(epoch):
                mode = await prepare_current_mode(self.journal, run_id)
            with epoch_scope(epoch), mode_adoption_scope(mode) if mode is not None else nullcontext():
                prepared = prepare_invalid_read(
                    run,
                    task,
                    entries,
                    accepted_runtime_contract=accepted_runtime,
                    allow_unavailable_source_observations=allow_observations,
                    native_tools=tool_rows,
                )
                if epoch is not None and prepared.proof["runtime_contract"] != epoch["legacy_runtime_contract"]:
                    raise ValueError("Historical policy epoch differs from the consumed runtime")
                read_guard = await self.restores.source_read_guard(
                    run, actor, task_attempt=task["attempt"], invalid_read=True
                )
                workspace = await workspace_cut(self.store, self.engine.workspaces / run_id, assert_current=read_guard)
                binding = prepared.data["checkpoint"]["invocation"]
                if workspace != binding["workspace"]:
                    raise ValueError("Current source differs from the original rejected-read frontier")
                command_cut = await self._command_cut(run, task, entries)
                if self.engine.tool_broker is not None and await self.engine.tool_broker.unresolved(run_id):
                    raise ValueError("Unsettled native tools prevent continuity")
                settings = self.store.configuration.load_snapshot(effective_revision(run))
                validate_checkpoints(settings, provider="bridge")
                if settings.get("FORGE_BRIDGE_COMPLETION_RECOVERY") != "required":
                    raise ValueError("Continuity requires native completion recovery")
                if self.store.configuration.resolve("bridge") != settings:
                    raise ValueError("Current provider configuration differs from the original frozen revision")
                timeout = int(settings.get("FORGE_PROVIDER_TIMEOUT", "1800"))
                if not 1 <= timeout <= 7200:
                    raise ValueError("Frozen provider timeout is unsupported")
                if "active_execution_budget" in run:
                    raise ValueError("The active time-budget protocol requires an explicit adapter")
                active_deadline = run.get("active_execution_deadline_ms")
                if active_deadline is not None and type(active_deadline) is not int:
                    raise ValueError("The active execution deadline is unsupported")
                holds = []
                if not policy["revision"] or not configured.enabled:
                    holds.append("lease_policy_disabled")
                try:
                    profile = current_profile_seal()
                except ValueError:
                    profile = None
                    holds.append("current_profile_unqualified")
                if epoch is not None and (
                    epoch["lease_policy_digest"] != policy["digest"] or epoch["current_profile"] != profile
                ):
                    raise ValueError("Current qualification or finite-lease policy changed during epoch preparation")
                await self.store.admit_credentials([run, task, entries, prepared.data])
                rebuilt = await reconstruct_and_migrate(prepared)
                rebuilt.migrated.resume_kwargs()["usage_limits"].check_before_request(rebuilt.migrated.usage)
                value = dict(
                    version=1,
                    kind="consumed_invalid_read_continuity_candidate",
                    run_id=run_id,
                    actor_digest=digest(ProjectAccess.actor_snapshot(actor)),
                    run_digest=digest(run),
                    journal_digest=digest({"entries": entries}),
                    source_digest=digest(workspace),
                    command_custody_digest=command_cut,
                    current_runtime_contract=runtime_contract(),
                    proof=prepared.proof,
                    policy_epoch=epoch,
                    reconstruction=rebuilt.receipt,
                    lease_policy=policy,
                    original_deadline_ms=binding["deadline_ms"],
                    frozen_timeout_seconds=timeout,
                    active_deadline_ms=active_deadline,
                    current_profile=profile,
                    admission="HOLD" if holds else "READY",
                    holds=holds,
                    execution_authorized=False,
                )
                value["digest"] = digest(value)
                await self.store.admit_credentials(value)
                if (
                    await self._read(run_id) != (run, task, entries)
                    or self.store.configuration.settings_generation is not generation
                    or policy_snapshot(self.store.configuration) != policy
                    or (
                        prepared.proof["version"] == 2
                        and native_tool_custody(run_id, entries, await self._native_observation_tools(run_id, entries))
                        != prepared.proof["native_tool_custody_digest"]
                    )
                    or await workspace_cut(self.store, self.engine.workspaces / run_id, assert_current=read_guard)
                    != workspace
                    or await self._command_cut(run, task, entries) != command_cut
                ):
                    raise ValueError("Continuity source, policy or authority changed during preparation")
                self.store.reject_credentials(value)
                return dict(
                    candidate=value,
                    run=run,
                    task=task,
                    entries=entries,
                    actor=ProjectAccess.actor_snapshot(actor),
                    binding=copy.deepcopy(binding),
                    checkpoint=rebuilt.migrated.payload.decode(),
                    checkpoint_digest=rebuilt.migrated.digest,
                    generation=generation,
                    workspace=workspace,
                    settings_digest=digest(settings),
                    read_guard=read_guard,
                )

    async def candidate(self, run_id: str) -> dict[str, Any]:
        return copy.deepcopy((await self._prepare(run_id))["candidate"])
