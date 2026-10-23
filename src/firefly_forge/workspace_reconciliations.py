# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Administrator-sealed environment repair before an existing native task dispatches."""

from __future__ import annotations

import copy
from typing import Any

from pyfly.container import service

from firefly_forge.configuration import redact_secrets
from firefly_forge.engine import redact
from firefly_forge.journal import digest
from firefly_forge.policies import policy_commands
from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied, ProjectAccess
from firefly_forge.provider_recoveries import ProviderRecoveries
from firefly_forge.sandbox_models import validate_sandbox_snapshot
from firefly_forge.scm_source_edit_models import SealedSourceEditAdmission
from firefly_forge.store import identifier
from firefly_forge.verification_models import check_verification_cut
from firefly_forge.workspace_reconciliation import _git, apply_dependency_plan, dependency_plan
from firefly_forge.workspace_reconciliation_models import WorkspaceReconciliationInput, queued_environment_scope

Data = dict[str, Any]


@service
class WorkspaceReconciliations:
    def __init__(self, recoveries: ProviderRecoveries) -> None:
        self.recoveries = recoveries

    @staticmethod
    def _actor(actor: Any) -> Data:
        return {
            **ProjectAccess.actor_snapshot(actor),
            "identity_digest": digest({"issuer": actor.issuer, "subject": actor.subject}),
        }

    @staticmethod
    def _original(run: Data) -> Data:
        return {k: copy.deepcopy(v) for k, v in run.items() if k != "events"}

    async def _candidate(
        self, run: Data, actor: Data, *, reserved: str | None = None, source_edit: bool = False
    ) -> Data:
        r = self.recoveries
        await r.store.assert_ownership()
        await r._settled(run, idle=True)
        entries = await r._entries(run["id"])
        if reserved is not None:
            entries = [
                e
                for e in entries
                if not (e["kind"] == "workspace_reconciliation_intent" and e["idempotency_key"] == reserved)
            ]
        receipts = {e["idempotency_key"] for e in entries if e["kind"] == "workspace_reconciliation_receipt"}
        if any(
            e["kind"] == "workspace_reconciliation_intent" and e["idempotency_key"] not in receipts for e in entries
        ):
            raise ValueError("An unknown environment effect requires reconciliation; it cannot be replayed")
        handoff = (run.get("verification_handoff") or {}).get("id")
        verified = next(
            (
                e["payload"]
                for e in entries
                if e["kind"] == "verification_handoff_intent" and e["payload"].get("id") == handoff
            ),
            None,
        )
        if verified is None:
            raise ValueError("The existing task has no completed immutable verification source cut")
        check_verification_cut(r.store, run, verified)
        task = r._task_view(await r.journal._task(run["id"], "implement"))
        executions = await r.journal.sandbox_executions(run["id"])
        runner = validate_sandbox_snapshot(run["sandbox_snapshot"])
        from firefly_forge.cli_command_budget import assert_environment_command_budget

        source_scope = None
        if source_edit:
            from firefly_forge.source_edit_continuations import source_edit_scope

            source_scope = await source_edit_scope(r, run)
        else:
            await assert_environment_command_budget(r.store, r.journal, run, entries)
        commands = run.get("commands", [])
        final = len(commands) + len(policy_commands(run.get("policy_snapshot", {}), run["backend"]))
        budget = queued_environment_scope(
            run,
            task,
            entries,
            verification_allowance=await r.journal.verification_continuation_allowance(run["id"]),
            human_allowance=await r.journal.human_clarification_continuation_allowance(run["id"]),
            remaining=source_scope["remaining_commands"] if source_scope else runner.command_limit - len(executions),
            final_checks=final,
        )
        if source_edit:
            check_verification_cut(r.store, run, verified)
        cut = r._cut(run)
        workspace = r.engine.workspaces / run["id"]
        plan = dependency_plan(
            workspace,
            run["base_revision"],
            run["git_metadata_digest"],
            component=(run.get("component_snapshot") or {}).get("path", "."),
        )
        if r._cut(run) != cut:
            raise ValueError("The environment review source or authority cut changed")
        value = {
            "version": 1,
            "run_id": run["id"],
            "actor": actor,
            "previous_run_digest": digest(self._original(run)),
            "previous_run": self._original(run),
            "previous_task": task,
            "journal_digest": entries[-1]["digest"],
            "cut": cut,
            "plan": plan,
            "budget": budget,
            **({"source_edit": source_scope} if source_scope else {}),
        }
        r.store.reject_credentials(value)
        return {**value, "digest": digest(value)}

    async def source_candidate(self, run_id: str) -> Data:
        identifier(run_id)
        r = self.recoveries
        actor = await r._operator(run_id)
        async with r._locked(run_id, self._actor(actor)) as row:
            value = await self._candidate(row.data, self._actor(actor), source_edit=True)
        await r._operator(run_id)
        return value

    async def activate_source_edit(self, run_id: str, body: Any) -> Data:
        from firefly_forge.source_edit_models import SourceEditContinuationInput

        body = SourceEditContinuationInput.model_validate(body.model_dump())
        return await self._admit(run_id, body, source_edit=True)

    async def activate_source_edit_from_scm(self, admission: SealedSourceEditAdmission) -> Data:
        from firefly_forge.source_edit_models import SourceEditContinuationInput

        body = SourceEditContinuationInput.model_validate(admission.request.model_dump())
        return await self._admit(admission.run_id, body, source_edit=True, scm_admission=admission)

    async def candidate(self, run_id: str) -> Data:
        identifier(run_id)
        r = self.recoveries
        actor = await r._operator(run_id)
        async with r._locked(run_id, self._actor(actor)) as row:
            value = await self._candidate(row.data, self._actor(actor))
        await r._operator(run_id)
        return value

    @staticmethod
    def _existing(entries: list[Data], request_id: str) -> Data | None:
        matching = [
            e
            for e in entries
            if e["idempotency_key"] == request_id
            and e["kind"]
            in {
                "workspace_reconciliation_intent",
                "workspace_reconciliation_receipt",
            }
        ]
        receipt = next((e["payload"] for e in matching if e["kind"] == "workspace_reconciliation_receipt"), None)
        if receipt is not None:
            return copy.deepcopy(dict(receipt))
        intent = next((e for e in matching if e["kind"] == "workspace_reconciliation_intent"), None)
        if intent is not None:
            return {
                **copy.deepcopy(intent["payload"]),
                "status": "needs_reconciliation",
                "intent_digest": intent["digest"],
            }
        return None

    async def get(self, run_id: str, request_id: str) -> Data:
        identifier(request_id)
        r = self.recoveries
        actor = await r._operator(run_id)
        snapshot = self._actor(actor)
        async with r._locked(run_id, snapshot):
            found = self._existing(await r._entries(run_id), request_id)
            if found is None:
                raise KeyError("The original environment reconciliation was not found")
            if found["actor"] != snapshot:
                raise AccessDenied("Only the original current administrator can read this transition")
            r.store.reject_credentials(found)
            return found

    async def admit(self, run_id: str, body: WorkspaceReconciliationInput) -> Data:
        body = WorkspaceReconciliationInput.model_validate(body.model_dump())
        return await self._admit(run_id, body)

    async def _admit(
        self,
        run_id: str,
        body: Any,
        *,
        source_edit: bool = False,
        scm_admission: SealedSourceEditAdmission | None = None,
    ) -> Data:
        r = self.recoveries

        async def operator() -> Any:
            if scm_admission is None:
                return await r._operator(run_id)
            from firefly_forge.source_edit_continuations import _assert_source_administrator

            if (
                not source_edit
                or scm_admission.run_id != run_id
                or scm_admission.request.model_dump() != body.model_dump()
                or CURRENT_PRINCIPAL.get() != scm_admission.caller
                or scm_admission.caller.authentication_method != "scm_mapping"
            ):
                raise AccessDenied("The exact verified SCM source activation is required")
            await scm_admission.verify()
            await _assert_source_administrator(r.store, scm_admission.native_actor)
            await ProjectAccess(r.store).authorize_execution(await r.store.get_run(run_id))
            if CURRENT_PRINCIPAL.get() != scm_admission.caller:
                raise AccessDenied("The source activation caller changed")
            return scm_admission.caller

        actor = await operator()
        snapshot = copy.deepcopy(scm_admission.native_actor) if scm_admission else self._actor(actor)
        request = body.model_dump()
        # The native admission wait precedes the locked current-authority recheck.
        await r.store.admit_credentials(request)
        await operator()
        async with r._locked(run_id, snapshot) as row:
            await operator()
            prior = self._existing(await r._entries(run_id), body.request_id)
            if prior is not None:
                if prior["request"] != request or prior["actor"] != snapshot:
                    raise ValueError("The reconciliation identity already binds another request or operator")
                if scm_admission is not None and prior.get("scm_source_activation") != scm_admission.evidence:
                    raise AccessDenied("The prior native activation does not bind this exact SCM trigger provenance")
                if prior.get("status") == "needs_reconciliation":
                    raise ValueError("The original environment outcome is unknown and requires reconciliation")
                return prior
            candidate = await self._candidate(row.data, snapshot, source_edit=source_edit)
            if candidate["digest"] != body.candidate_digest:
                raise ValueError("The reviewed environment candidate changed")
            payload = {
                "version": 1,
                "request": request,
                "actor": snapshot,
                "candidate": candidate,
                "previous_task": candidate["previous_task"],
                **({"scm_source_activation": copy.deepcopy(scm_admission.evidence)} if scm_admission else {}),
            }
            if source_edit:
                from firefly_forge.source_edit_continuations import clock_ms

                payload["expires_at_ms"] = clock_ms() + int(candidate["source_edit"]["provider_timeout"] * 1000)
            r.store.reject_credentials(payload)
            intent = await r.journal._append(
                run_id, "implement", body.request_id, "workspace_reconciliation_intent", payload
            )
            await r.store.assert_ownership()

        # The committed intent survives every filesystem, admission, cancellation or SQL failure below.
        async with r._locked(run_id, snapshot) as row:
            await operator()
            fresh = await self._candidate(row.data, snapshot, reserved=body.request_id, source_edit=source_edit)
            if fresh != candidate or CURRENT_PRINCIPAL.get() != actor:
                raise ValueError("The durable environment intent lost its reviewed cut")
            await r.store.assert_ownership()
            await operator()
            workspace = r.engine.workspaces / run_id
            effect = apply_dependency_plan(workspace, candidate["plan"])
            current = {**row.data, "git_metadata_digest": effect["metadata_after"]}
            after = r._cut(current)
            if {k: v for k, v in after.items() if k not in {"metadata_digest", "index_digest"}} != {
                k: v for k, v in candidate["cut"].items() if k not in {"metadata_digest", "index_digest"}
            }:
                raise ValueError("Environment reconciliation changed source, history or execution authority")
            # Porcelain diff can refresh index stat data and invalidate the sealed source cut.
            patch = _git(
                workspace,
                "diff-index",
                "-p",
                "--binary",
                "--no-ext-diff",
                "--no-color",
                candidate["plan"]["base_revision"],
                "--",
            ).decode()
        settings = r.store.configuration.load_snapshot(candidate["previous_run"]["config_revision"])
        if redact_secrets(redact(patch), settings) != patch:
            raise ValueError("Reconciled source still contains a configured credential; delivery remains held")
        await r.store.admit_credentials(patch)
        await operator()
        async with r._locked(run_id, snapshot) as row:
            await operator()
            if digest(self._original(row.data)) != candidate["previous_run_digest"]:
                raise ValueError("The failed native run changed after the environment effect")
            await r._settled(row.data, idle=True)
            if r._cut({**row.data, "git_metadata_digest": effect["metadata_after"]}) != after:
                raise ValueError("The reconciled source or authority changed during credential admission")
            if CURRENT_PRINCIPAL.get() != actor:
                raise AccessDenied("The environment operator changed before completion")
            await r.store.assert_ownership()
            from firefly_forge.cli_command_budget import assert_environment_command_budget

            if source_edit:
                from firefly_forge.source_edit_continuations import clock_ms, source_edit_scope

                if (
                    await source_edit_scope(r, row.data) != candidate["source_edit"]
                    or payload["expires_at_ms"] <= clock_ms()
                ):
                    raise ValueError("The source-only capability or original deadline changed")
                await r.store.assert_ownership()
                if r._cut({**row.data, "git_metadata_digest": effect["metadata_after"]}) != after:
                    raise ValueError("The reconciled source or authority changed during native policy qualification")
            else:
                await assert_environment_command_budget(r.store, r.journal, row.data, await r._entries(run_id))
            await operator()
            receipt = {**payload, "intent_digest": intent["digest"], "effect": effect, "completed_cut": after}
            r.store.reject_credentials(receipt)
            await r.journal._append(run_id, "implement", body.request_id, "workspace_reconciliation_receipt", receipt)
            await r.journal.queue_environment_reconciled(
                run_id, body.request_id, **({"scm_source_admission": scm_admission} if scm_admission else {})
            )
            return copy.deepcopy(receipt)
