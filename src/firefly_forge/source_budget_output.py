# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Explicit finite activation after retained output failed its implementation contract."""

from __future__ import annotations

import copy
import hashlib
from datetime import datetime
from typing import Any, cast
from uuid import NAMESPACE_URL, uuid5

from firefly_forge.journal import digest
from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied
from firefly_forge.source_budget_allowances import (
    PROTOCOL,
    SourceBudgetAllowanceInput,
    SourceBudgetPolicy,
    SourceBudgetProposal,
    SourceBudgetReadmissions,
    admission_contract_digest,
    checked_parameters,
    history_snapshot,
    policy_snapshot,
    qualified_scope_transition,
)
from firefly_forge.source_edit_continuations import clock_ms, source_edit_scope
from firefly_forge.source_edit_models import SourceEditContinuationInput
from firefly_forge.source_human_readmission import (
    _RETAINED,
    _ancestral_unknown,
    _validated_parent,
    validated_source_plan,
)
from firefly_forge.source_patch_admission import SourcePatch
from firefly_forge.source_result_readmission import SourceCompletedOutputReadmissions, _one
from firefly_forge.store import Store, identifier, now

Data = dict[str, Any]
BASIS = "known_completed_output"
OBSERVED = "source_budget_output"


def _context(run: Data, task: Data, entries: list[Data], parent: Data, actor: Data) -> Data:
    """Require a failed, durably dispatched canonical invocation, never infer completion from its error."""
    previous = parent["payload"]["candidate"]
    attempt = task.get("attempt")
    if type(attempt) is not int or not 5 <= attempt < 32:
        raise ValueError("Known budget output requires its existing canonical activation")
    key = uuid5(NAMESPACE_URL, f"forge:{run['id']}:implement:{attempt}").hex
    dispatches = [e for e in entries if e["kind"] == "provider_dispatch_intent"]
    dispatch = _one(entries, "provider_dispatch_intent", key=key + ":provider")
    _one(entries, "intent", key=key)
    old = previous["previous_run"]
    pending = run.get("provider_dispatch") or {}
    expected_dispatch_cut = parent["payload"]["completed_cut"]
    if (
        run.get("status") != "failed"
        or run.get("active_task") != "implement"
        or task.get("status") != "running"
        or task.get("idempotency_key") != key
        or previous["previous_task"].get("attempt") != attempt
        or old.get("implementation_rounds") != attempt - 1
        or run.get("implementation_rounds") != attempt
        or pending.get("task_key") != key
        or pending.get("actor") != actor
        or run.get("provider_recovery") != old.get("provider_recovery")
        or dispatch != dispatches[-1]
        or dispatch["sequence"] <= parent["sequence"]
        or dispatch["payload"].get("task_key") != key
        or dispatch["payload"].get("attempt") != attempt
        or pending.get("cut") != expected_dispatch_cut
        or any(
            e["sequence"] > dispatch["sequence"]
            and e["kind"] in {"receipt", "worker_feedback", "worker_response", "resume", "provider_failure_observation"}
            for e in entries
        )
        or any(
            e["kind"] in {"intent", "provider_dispatch_intent"} and e["payload"].get("attempt", attempt) > attempt
            for e in entries
        )
    ):
        raise ValueError("The retained failed output is not its exact settled canonical dispatch")
    return dispatch


def _observation(value: Data, run: Data, dispatch: Data) -> None:
    if (
        value.get("implementation_success") is not False
        or value.get("process_exit_attested") is not False
        or value.get("summary_sha256") != hashlib.sha256(run["summary"].encode()).hexdigest()
        or value.get("usage_digest") != digest(cast(Data, run.get("usage")))
        or value.get("dispatch_digest") != dispatch["digest"]
    ):
        raise ValueError("The retained native output observation differs from its dispatch, summary or usage")


async def source_review(r: Any, run: Data) -> tuple[Data, str]:
    """Read tracked diff and every new public UTF-8 file without staging or refreshing the index."""
    from firefly_forge.assurance import _manifest
    from firefly_forge.configuration import redact_secrets
    from firefly_forge.engine import redact
    from firefly_forge.workspace_reconciliation import _git
    from firefly_forge.workspace_reconciliations import _source_patch
    from firefly_forge.workspace_tools import WorkspaceTools

    root = r.engine.workspaces / run["id"]
    cut = r._cut(run)
    files = _manifest(root, original=True, extra_ignored=frozenset({".cache"}))
    tracked = set(_git(root, "ls-files", "-z").decode().split("\0"))
    untracked = sorted(name for name in files if not name.endswith("/") and name not in tracked)
    tools = WorkspaceTools(root, max_file_bytes=4 * 1024 * 1024)
    patch = _source_patch(root, run["base_revision"])
    for name in untracked:
        text = await tools.read_file(name)
        patch += "\n--- new public source: " + name + " ---\n" + text + "\n"
        if len(patch.encode()) > 4 * 1024 * 1024:
            raise ValueError("The full public source review exceeds its bounded credential-admission patch")
    settings = r.store.configuration.load_snapshot(run["config_revision"])
    if redact_secrets(redact(patch), settings) != patch:
        raise ValueError("The reviewed source contains a configured credential")
    if _manifest(root, original=True, extra_ignored=frozenset({".cache"})) != files or r._cut(run) != cut:
        raise ValueError("The public source or raw index changed while reading the review")
    return {
        "version": 1,
        "source_digest": cut["source_digest"],
        "manifest_digest": digest(files),
        "files": files,
        "untracked": untracked,
        "patch_sha256": hashlib.sha256(patch.encode()).hexdigest(),
    }, patch


class SourceBudgetOutputReadmissions(SourceBudgetReadmissions):
    def __init__(self, service: Any, proposal: SourceBudgetProposal) -> None:
        super().__init__(service, proposal)
        if proposal.basis != BASIS:
            raise ValueError("The failed-output admission basis must be explicit")

    async def _review(self, run: Data, parent_request: str, actor: Data, previous_path: str, **kwargs: Any) -> Data:
        from firefly_forge.source_budget_allowances import validate_budget_parent

        await self.r.store.assert_ownership()
        await self.r._settled(run, idle=True)
        entries = await self.r._entries(run["id"])
        event = _one(entries, "workspace_reconciliation_receipt", key=parent_request)
        marker = {"request_id": parent_request, "receipt_digest": event["digest"]}
        if run.get("source_edit_continuation") != marker:
            raise ValueError("The exact completed current budget activation is required")
        validate_budget_parent(entries, event, actor)
        parent, original = _validated_parent(entries, event, actor)
        completed = {e["idempotency_key"] for e in entries if e["kind"] == "workspace_reconciliation_receipt"}
        allowed = _ancestral_unknown(entries, event, actor)
        if any(
            e["kind"] == "workspace_reconciliation_intent"
            and e["idempotency_key"] not in completed
            and e["digest"] not in allowed
            for e in entries
        ):
            raise ValueError("An unknown source admission cannot grant a new output activation")
        if any(e["kind"] == OBSERVED and e["idempotency_key"] not in completed for e in entries):
            raise ValueError("An incomplete native output observation requires reconciliation")
        task = self.r._task_view(await self.r.journal._task(run["id"], "implement"))
        dispatch = _context(run, task, entries, event, actor)
        cut = self.r._cut(run)
        if {k: v for k, v in cut.items() if k not in {"source_digest", "retained_state_digest"}} != {
            k: v for k, v in parent["completed_cut"].items() if k not in {"source_digest", "retained_state_digest"}
        }:
            raise ValueError("The completed budget activation Git index, provider or authority changed")
        retained = {k: original["previous_run"].get(k) for k in _RETAINED}
        if (
            digest(retained) != parent["completed_cut"]["retained_state_digest"]
            or digest({**retained, "implementation_rounds": task["attempt"]}) != cut["retained_state_digest"]
        ):
            raise ValueError("Known output changed retained budgets, answers or verification state")
        policy = policy_snapshot(self.r.store.configuration)
        grants = sum(
            e["kind"] == "workspace_reconciliation_receipt"
            and bool(e["payload"].get("candidate", {}).get("source_budget_readmission"))
            for e in entries
        )
        assert self.proposal is not None
        parameters = checked_parameters(
            self.proposal,
            SourceBudgetPolicy.model_validate(policy["settings"]),
            previous_attempt=task["attempt"],
            used_allowances=grants,
        )
        history = history_snapshot(await self.r.engine.tool_broker.invocations(run["id"]))
        if (
            len(history["rows"]) >= min(30, run["tool_policy_snapshot"]["settings"]["calls_limit"])
            or sum(row["tool"] == "read_file" for row in history["rows"]) >= parameters["read_file_lifetime_limit"]
        ):
            raise ValueError("No finite original shared or read_file capability remains")
        output = await SourceCompletedOutputReadmissions(self.service)._observation(run, dispatch)
        _observation(output, run, dispatch)
        review, _ = await source_review(self.r, run)
        budget = {
            "protocol": PROTOCOL,
            "basis": BASIS,
            "policy": policy,
            "proposal": self.proposal.model_dump(),
            "parameters": parameters,
            "previous_attempt": task["attempt"],
            "prior_allowance_count": grants,
            "history": history,
            "known_completed_output_observation": output,
        }
        scope = await source_edit_scope(self.r, run, archive_cut=original["source_edit"]["archive_cut"])
        scope, relocation = qualified_scope_transition(
            self.r, run, original["source_edit"], scope, previous_path, budget
        )
        if self.r._cut(run) != cut or await self.r._entries(run["id"]) != entries:
            raise ValueError("Known output review changed while authenticating its retained evidence")
        value = {
            "version": 1,
            "basis": BASIS,
            "run_id": run["id"],
            "parent_request_id": parent_request,
            "parent_receipt_digest": event["digest"],
            "actor": actor,
            "previous_task": task,
            "previous_run_digest": digest(run),
            "cut": cut,
            "link": {
                "version": 1,
                "protocol": PROTOCOL,
                "from_attempt": task["attempt"],
                "to_attempt": task["attempt"] + 1,
            },
            "source_edit": scope,
            "configuration_relocation": relocation,
            "source_plan": validated_source_plan(entries, event, actor, run),
            "source_review": review,
            "observation": output,
            "budget_review": budget,
            "admission_contract_digest": admission_contract_digest(),
            "journal_digest": entries[-1]["digest"],
        }
        self.r.store.reject_credentials(value)
        self._assert_review_current(value)
        return {**value, "digest": digest(value)}

    async def candidate(
        self, run_id: str, parent_request: str, previous_catalog_path: str, proposal: SourceBudgetProposal | None = None
    ) -> Data:
        identifier(parent_request)
        actor = await self.r._operator(run_id)
        async with self.r._locked(run_id, self.service._actor(actor)) as row:
            value = await self._review(row.data, parent_request, self.service._actor(actor), previous_catalog_path)
        if await self.r._operator(run_id) != actor:
            raise AccessDenied("The source output administrator changed")
        return value

    async def admit(self, run_id: str, parent_request: str, body: Any) -> Data:
        body = SourceBudgetAllowanceInput.model_validate(body.model_dump())
        actor = await self.r._operator(run_id)
        snapshot = self.service._actor(actor)
        async with self.r._locked(run_id, snapshot):
            entries = await self.r._entries(run_id)
            prior = self._existing(entries, parent_request, body.request_id, snapshot)
            if prior is not None:
                if prior["human_request"] != body.model_dump():
                    raise ValueError("The output request already contains different consent")
                return prior
            if any(e["kind"] == OBSERVED and e["idempotency_key"] == body.request_id for e in entries):
                raise ValueError("An incomplete output observation cannot be replayed")
        review = await self.candidate(run_id, parent_request, body.previous_catalog_path)
        if (
            body.request_id == parent_request
            or body.candidate_digest != review["digest"]
            or body.admission_contract_digest != review["admission_contract_digest"]
            or body.reviewed_source_digest != review["cut"]["source_digest"]
            or body.reviewed_patch_sha256 != review["source_review"]["patch_sha256"]
            or body.valid_for_seconds > review["source_edit"]["provider_timeout"]
        ):
            raise ValueError("Known output requires exact fresh source, patch and finite consent")
        run = await self.r.store.get_run(run_id)
        _, patch = await source_review(self.r, run)
        await Store.admit_source_patch(self.r.store, SourcePatch(patch))
        if await self.r._operator(run_id) != actor or CURRENT_PRINCIPAL.get() != actor:
            raise AccessDenied("The source output administrator changed during credential admission")
        async with self._admission_lock(run_id, snapshot) as row:
            if await self._review(row.data, parent_request, snapshot, body.previous_catalog_path) != review:
                raise ValueError("The exact known output admission changed during credential admission")
            proof = {
                "basis": BASIS,
                "parent_marker": copy.deepcopy(row.data["source_edit_continuation"]),
                "previous_task": review["previous_task"],
                "link": review["link"],
                "budget_review": review["budget_review"],
                "review_digest": review["digest"],
                "admission_contract_digest": review["admission_contract_digest"],
            }
            key = uuid5(NAMESPACE_URL, f"forge:{run_id}:implement:{review['link']['to_attempt']}").hex
            task = {
                "id": "implement",
                "status": "queued",
                "attempt": review["link"]["to_attempt"],
                "idempotency_key": key,
            }
            candidate = {
                "version": 1,
                "run_id": run_id,
                "actor": snapshot,
                "previous_run": copy.deepcopy(row.data),
                "previous_run_digest": digest(row.data),
                "previous_task": task,
                "cut": review["cut"],
                "plan": review["source_plan"]["plan"],
                "source_edit": review["source_edit"],
                "source_budget_readmission": proof,
                "journal_digest": review["journal_digest"],
            }
            candidate["digest"] = digest(candidate)
            request = SourceEditContinuationInput(
                version=1,
                request_id=body.request_id,
                candidate_digest=candidate["digest"],
                acknowledge_environment_only=True,
                acknowledge_resume_existing_task=True,
                acknowledge_existing_read_scope=True,
                acknowledge_no_commands=True,
            ).model_dump()
            expires = clock_ms() + body.valid_for_seconds * 1000
            observed = await self.r.journal._append(
                run_id,
                "implement",
                body.request_id,
                OBSERVED,
                {
                    "version": 1,
                    "request": body.model_dump(),
                    "actor": snapshot,
                    "review": review,
                    "new_candidate_digest": candidate["digest"],
                    "expires_at_ms": expires,
                    "no_environment_effect": True,
                    "no_provider_dispatch": True,
                },
            )
            payload = {
                "version": 1,
                "request": request,
                "actor": snapshot,
                "candidate": candidate,
                "previous_task": task,
                "human_request": body.model_dump(),
                "readmission_digest": observed["digest"],
                "expires_at_ms": expires,
                "public_patch_sha256": review["source_review"]["patch_sha256"],
            }
            await self._assert_admission_current(run_id, snapshot, review)
            intent = await self.r.journal._append(
                run_id, "implement", body.request_id, "workspace_reconciliation_intent", payload
            )
            queued = await self.r.journal._task(run_id, "implement")
            queued.status, queued.attempt, queued.data = "queued", task["attempt"], {"idempotency_key": key}
            await self.r.journal.tasks.save(queued)
            row.status = "queued"
            row.data = {**row.data, "status": "queued", "resume_existing": True, "updated_at": now()}
            receipt = {
                **payload,
                "intent_digest": intent["digest"],
                "completed_cut": self.r._cut(row.data),
                "effect": {
                    "version": 1,
                    "basis": BASIS,
                    "no_environment_effect": True,
                    "completed_output_observation": digest(review["observation"]),
                },
            }
            if receipt["completed_cut"] != review["cut"]:
                raise ValueError("The queued output activation changed its sealed source and budget cut")
            self.r.store.reject_credentials(receipt)
            event = await self.r.journal._append(
                run_id, "implement", body.request_id, "workspace_reconciliation_receipt", receipt
            )
            row.data = {
                **row.data,
                "source_edit_continuation": {"request_id": body.request_id, "receipt_digest": event["digest"]},
            }
            await self.r.store.run_records.save(row)
            await self._assert_admission_current(run_id, snapshot, review)
            final_review, final_patch = await source_review(self.r, row.data)
            history = history_snapshot(await self.r.engine.tool_broker.invocations(run_id))
            if (
                CURRENT_PRINCIPAL.get() != actor
                or await self.r._operator(run_id) != actor
                or final_review != review["source_review"]
                or final_patch != patch
                or self.r._task_view(await self.r.journal._task(run_id, "implement")) != task
                or history != review["budget_review"]["history"]
                or self.r._cut(row.data) != review["cut"]
                or clock_ms() >= expires
            ):
                raise ValueError("The source, authority, tool history or finite admission changed before commit")
            await self._assert_admission_current(run_id, snapshot, review)
            from firefly_forge.assurance import _manifest

            if (
                self.r._cut(row.data) != review["cut"]
                or _manifest(self.r.engine.workspaces / run_id, original=True, extra_ignored=frozenset({".cache"}))
                != review["source_review"]["files"]
                or clock_ms() >= expires
            ):
                raise ValueError(
                    "The public source, exact index or finite consent changed at the final native commit boundary"
                )
            return copy.deepcopy(receipt)


def validate_output_parent(entries: list[Data], event: Data, actor: Data) -> Data:
    from firefly_forge.source_budget_allowances import validate_budget_parent

    receipt, candidate = _validated_parent(entries, event, actor)
    proof = candidate["source_budget_readmission"]
    body = SourceBudgetAllowanceInput.model_validate(receipt["human_request"])
    intent = _one(entries, "workspace_reconciliation_intent", fingerprint=receipt["intent_digest"])
    observed = _one(entries, OBSERVED, fingerprint=receipt["readmission_digest"])
    marker = proof["parent_marker"]
    parent = _one(
        entries, "workspace_reconciliation_receipt", key=marker["request_id"], fingerprint=marker["receipt_digest"]
    )
    prior_entries = [e for e in entries if e["sequence"] < intent["sequence"] and e["digest"] != observed["digest"]]
    validate_budget_parent(prior_entries, parent, actor)
    review = observed["payload"]["review"]
    run = candidate["previous_run"]
    dispatch = _context(run, proof["previous_task"], prior_entries, parent, actor)
    budget = proof["budget_review"]
    policy = budget["policy"]
    parameters = checked_parameters(
        body.proposal,
        SourceBudgetPolicy.model_validate(policy["settings"]),
        previous_attempt=proof["link"]["from_attempt"],
        used_allowances=budget["prior_allowance_count"],
    )
    grant_count = sum(
        e["kind"] == "workspace_reconciliation_receipt"
        and bool(e["payload"].get("candidate", {}).get("source_budget_readmission"))
        for e in prior_entries
    )

    def milliseconds(row: Data) -> int:
        return int(datetime.fromisoformat(row["created_at"]).timestamp() * 1000)

    if (
        proof["basis"] != BASIS
        or body.proposal.basis != BASIS
        or not parent["sequence"] < observed["sequence"] < intent["sequence"] < event["sequence"]
        or body.request_id != observed["idempotency_key"]
        or body.request_id != receipt["request"]["request_id"]
        or observed["payload"]["request"] != body.model_dump()
        or observed["payload"]["actor"] != actor
        or observed["payload"]["new_candidate_digest"] != candidate["digest"]
        or review["digest"] != digest({k: v for k, v in review.items() if k != "digest"})
        or proof["review_digest"] != review["digest"]
        or body.candidate_digest != review["digest"]
        or review["basis"] != BASIS
        or review["actor"] != actor
        or review["parent_request_id"] != marker["request_id"]
        or review["parent_receipt_digest"] != marker["receipt_digest"]
        or review["previous_task"] != proof["previous_task"]
        or review["link"] != proof["link"]
        or review["source_edit"] != candidate["source_edit"]
        or review["cut"] != candidate["cut"]
        or review["source_plan"]["plan"] != candidate["plan"]
        or review["previous_run_digest"] != digest(run)
        or candidate["previous_run_digest"] != digest(run)
        or run["source_edit_continuation"] != marker
        or proof["admission_contract_digest"] != body.admission_contract_digest
        or review["admission_contract_digest"] != body.admission_contract_digest
        or review["budget_review"] != budget
        or budget["basis"] != BASIS
        or policy["digest"] != digest({k: v for k, v in policy.items() if k != "digest"})
        or budget["proposal"] != body.proposal.model_dump()
        or budget["parameters"] != parameters
        or budget["prior_allowance_count"] != grant_count
        or not 0 <= grant_count < 3
        or proof["link"]
        != {
            "version": 1,
            "protocol": PROTOCOL,
            "from_attempt": proof["previous_task"]["attempt"],
            "to_attempt": parameters["activation_ceiling"],
        }
        or candidate["previous_task"]["attempt"] != parameters["activation_ceiling"]
        or body.reviewed_source_digest != review["cut"]["source_digest"]
        or body.reviewed_patch_sha256 != review["source_review"]["patch_sha256"]
        or receipt["public_patch_sha256"] != body.reviewed_patch_sha256
        or budget["history"]["digest"] != digest({"rows": budget["history"]["rows"]})
        or candidate["source_edit"]["budget_allowance"]["policy_digest"] != policy["digest"]
        or any(candidate["source_edit"]["budget_allowance"][k] != v for k, v in parameters.items())
        or not milliseconds(observed)
        < receipt["expires_at_ms"]
        <= milliseconds(observed) + body.valid_for_seconds * 1000
        or milliseconds(event) >= receipt["expires_at_ms"]
        or observed["payload"]["expires_at_ms"] != receipt["expires_at_ms"]
        or intent["payload"]["expires_at_ms"] != receipt["expires_at_ms"]
        or intent["payload"]["human_request"] != body.model_dump()
        or intent["payload"]["readmission_digest"] != observed["digest"]
        or intent["payload"]["public_patch_sha256"] != body.reviewed_patch_sha256
        or receipt["completed_cut"] != candidate["cut"]
        or review["source_review"]["manifest_digest"] != digest(review["source_review"]["files"])
        or review["source_review"]["source_digest"] != candidate["cut"]["source_digest"]
        or review["journal_digest"] != candidate["journal_digest"]
        or observed["previous_hash"] != review["journal_digest"]
        or intent["previous_hash"] != observed["digest"]
        or event["previous_hash"] != intent["digest"]
        or observed["payload"]["no_environment_effect"] is not True
        or observed["payload"]["no_provider_dispatch"] is not True
        or receipt["effect"]
        != {
            "version": 1,
            "basis": BASIS,
            "no_environment_effect": True,
            "completed_output_observation": digest(review["observation"]),
        }
    ):
        raise ValueError("The known output receipt lost its exact finite source, budget or observation consent")
    _observation(review["observation"], run, dispatch)
    if budget["known_completed_output_observation"] != review["observation"]:
        raise ValueError("The known output budget proof differs from its authenticated native observation")
    return parent
