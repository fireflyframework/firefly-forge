# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""One explicitly renewed source-only attempt for an immutable answered human question."""

from __future__ import annotations

import copy
import hashlib
from datetime import datetime
from importlib import import_module
from pathlib import Path
from typing import Any, Literal
from uuid import NAMESPACE_URL, uuid5

from pydantic import BaseModel, ConfigDict, Field, field_validator

from firefly_forge.human_clarifications import continuation_link
from firefly_forge.journal import digest
from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied
from firefly_forge.source_edit_continuations import clock_ms, source_edit_scope
from firefly_forge.source_edit_models import SourceEditContinuationInput
from firefly_forge.source_edit_observed_reconciliation import _configuration_relocation
from firefly_forge.source_patch_admission import SourcePatch
from firefly_forge.store import Store, identifier

Data = dict[str, Any]
_RETAINED = (
    "base_revision",
    "checkpoint_revision",
    "repair_budget",
    "implementation_rounds",
    "tool_policy_snapshot",
    "verification_handoff",
    "verification_adoption",
    "worker_response",
)


class SourceHumanReadmissionInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True)
    version: Literal[1]
    request_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    candidate_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    admission_contract_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    previous_catalog_path: str = Field(min_length=1, max_length=4096)
    valid_for_seconds: int = Field(ge=1, le=1800)
    acknowledge_same_answer_and_next_attempt: Literal[True]
    acknowledge_original_budgets: Literal[True]
    acknowledge_no_environment_replay: Literal[True]
    acknowledge_new_finite_activation: Literal[True]

    @field_validator("version", mode="before")
    @classmethod
    def exact_version(cls, value: Any) -> int:
        if type(value) is not int or value != 1:
            raise ValueError("Human source readmission requires exact version one")
        return value

    @field_validator(
        "acknowledge_same_answer_and_next_attempt",
        "acknowledge_original_budgets",
        "acknowledge_no_environment_replay",
        "acknowledge_new_finite_activation",
        mode="before",
    )
    @classmethod
    def explicit_acknowledgement(cls, value: Any) -> bool:
        if value is not True:
            raise ValueError("Explicit human source readmission acknowledgements are required")
        return value


def admission_contract_digest() -> str:
    root = Path(__file__).parent
    return digest(
        {
            name: hashlib.sha256((root / name).read_bytes()).hexdigest()
            for name in (
                "source_human_readmission.py",
                "journal.py",
                "human_clarifications.py",
                "provider_recoveries.py",
                "workspace_reconciliation_controllers.py",
                "source_edit_observed_reconciliation.py",
                "source_index_sanitation.py",
                "source_result_readmission.py",
            )
        }
    )


def assert_resume_admission(
    run: Data, task: Data, entries: list[Data], actor: Data, link: Data | None, request_id: str | None
) -> None:
    """A caller-supplied identifier is authority only when the exact locked native intent exists."""
    identifier(request_id or "")
    intent = next(
        (e for e in entries if e["kind"] == "workspace_reconciliation_intent" and e["idempotency_key"] == request_id),
        None,
    )
    value = intent["payload"] if intent else {}
    candidate = value.get("candidate", {})
    human = candidate.get("human_readmission", {})
    principal = CURRENT_PRINCIPAL.get()
    if principal is None:
        raise AccessDenied("A current native human readmission operator is required")
    expected_actor = {**actor, "identity_digest": digest({"issuer": principal.issuer, "subject": principal.subject})}
    if (
        link is None
        or expected_actor != value.get("actor")
        or candidate.get("previous_run_digest") != digest(run)
        or human.get("link") != link
        or human.get("previous_task") != task
        or human.get("parent_marker") != run.get("source_edit_continuation")
        or human.get("admission_contract_digest") != admission_contract_digest()
        or candidate.get("digest") != digest({k: v for k, v in candidate.items() if k != "digest"})
        or value.get("expires_at_ms", 0) <= clock_ms()
        or any(e["kind"] == "workspace_reconciliation_receipt" and e["idempotency_key"] == request_id for e in entries)
    ):
        raise ValueError("The fresh native source-only human admission no longer binds this exact answer and task")


def _validated_parent(entries: list[Data], event: Data, actor: Data) -> tuple[Data, Data]:
    parent = event["payload"]
    body = SourceEditContinuationInput.model_validate(parent["request"])
    candidate = parent["candidate"]
    intent = next(
        (
            e
            for e in entries
            if e["kind"] == "workspace_reconciliation_intent" and e["idempotency_key"] == body.request_id
        ),
        None,
    )
    if (
        event["idempotency_key"] != body.request_id
        or intent is None
        or intent["sequence"] >= event["sequence"]
        or parent.get("intent_digest") != intent["digest"]
        or parent["actor"] != actor
        or body.candidate_digest != candidate["digest"]
        or candidate["digest"] != digest({k: v for k, v in candidate.items() if k != "digest"})
        or any(parent[k] != intent["payload"][k] for k in ("request", "actor", "candidate", "previous_task"))
    ):
        raise AccessDenied("The completed source ancestry does not bind its exact native administrator and intent")
    return parent, candidate


def _completed_output_parent(entries: list[Data], event: Data, actor: Data) -> Data:
    # The optional successor is loaded only for its explicit, strictly validated native receipt kind.
    module = import_module("firefly_forge.source_result_readmission")
    ancestor: Data = module.validate_completed_output_parent(entries, event, actor)
    return ancestor


def _ancestral_unknown(entries: list[Data], event: Data, actor: Data) -> set[str]:
    seen: set[str] = set()
    from firefly_forge.source_budget_allowances import unwrap_budget_parents

    event = unwrap_budget_parents(entries, event, actor)
    for _ in range(4):
        if event["digest"] in seen:
            raise ValueError("Human source ancestry is cyclic")
        seen.add(event["digest"])
        parent, candidate = _validated_parent(entries, event, actor)
        if candidate.get("completed_output_readmission") is not None:
            event = _completed_output_parent(entries, event, actor)
            continue
        human = candidate.get("human_readmission")
        if human is not None:
            marker = human["parent_marker"]
            ancestor = next(
                (
                    e
                    for e in entries
                    if e["kind"] == "workspace_reconciliation_receipt"
                    and e["idempotency_key"] == marker["request_id"]
                    and e["digest"] == marker["receipt_digest"]
                ),
                None,
            )
            intent = next(e for e in entries if e["digest"] == parent["intent_digest"])
            if (
                ancestor is None
                or ancestor["sequence"] >= intent["sequence"]
                or human["link"]["to_attempt"] != candidate["previous_task"]["attempt"]
                or human["link"]["from_attempt"] != ancestor["payload"]["candidate"]["previous_task"]["attempt"]
                or len(
                    [
                        e
                        for e in entries
                        if e["kind"] == "resume"
                        and intent["sequence"] < e["sequence"] < event["sequence"]
                        and e["payload"].get("human_clarification") == human["link"]
                    ]
                )
                != 1
            ):
                raise ValueError("Human source ancestry lost its exact prior receipt or resume link")
            event = ancestor
            continue
        observed = candidate.get("observed_readmission")
        if observed is None:
            return set()
        renewal = next(
            (
                e
                for e in entries
                if e["kind"] == "workspace_observed_readmission" and e["digest"] == parent.get("readmission_digest")
            ),
            None,
        )
        original = next(
            (
                e
                for e in entries
                if e["kind"] == "workspace_reconciliation_intent" and e["digest"] == observed["original_intent_digest"]
            ),
            None,
        )
        if (
            renewal is None
            or original is None
            or original["sequence"] >= renewal["sequence"]
            or renewal["sequence"] >= event["sequence"]
            or renewal["payload"]["actor"] != actor
            or original["payload"]["actor"] != actor
            or renewal["payload"]["original_intent_digest"] != original["digest"]
            or renewal["payload"]["new_candidate_digest"] != candidate["digest"]
            or renewal["payload"]["observed_effect"]["digest"] != observed["observed_candidate_digest"]
            or renewal["payload"]["observed_effect"]["original_request_id"] != original["idempotency_key"]
            or renewal["payload"]["observed_effect"]["admission_contract_digest"]
            != observed["admission_contract_digest"]
        ):
            raise ValueError("The observed source ancestor has no exact completed native readmission")
        from firefly_forge.source_edit_observed_reconciliation import ObservedSourceReadmissionInput

        consent = ObservedSourceReadmissionInput.model_validate(renewal["payload"]["request"])
        review = renewal["payload"]["observed_effect"]
        original_body = SourceEditContinuationInput.model_validate(original["payload"]["request"])
        original_candidate = original["payload"]["candidate"]
        if (
            review["digest"] != digest({k: v for k, v in review.items() if k != "digest"})
            or review["original_intent_digest"] != original["digest"]
            or consent.request_id != parent["request"]["request_id"]
            or consent.original_intent_digest != original["digest"]
            or consent.observed_candidate_digest != review["digest"]
            or original_body.candidate_digest != original_candidate["digest"]
            or original_candidate["digest"] != digest({k: v for k, v in original_candidate.items() if k != "digest"})
            or observed["scm_provenance_digest"] != digest(original["payload"].get("scm_source_activation"))
            or renewal["payload"]["no_provider_dispatch"] is not True
            or renewal["payload"]["new_expires_at_ms"] != parent["expires_at_ms"]
        ):
            raise ValueError("The observed source ancestor lost its exact immutable consent or review")
        return {original["digest"]}
    raise ValueError("Human source ancestry exceeds the finite ordinary attempt bound")


def validated_source_plan(entries: list[Data], event: Data, actor: Data, run: Data) -> Data:
    """Preserve the entire sealed original plan; a renewed deadline never invents source context."""
    _ancestral_unknown(entries, event, actor)
    inherited = []
    from firefly_forge.source_budget_allowances import unwrap_budget_parents

    event = unwrap_budget_parents(entries, event, actor)
    for _ in range(4):
        parent, candidate = _validated_parent(entries, event, actor)
        if candidate.get("completed_output_readmission") is not None:
            if "plan" in candidate:
                inherited.append(candidate["plan"])
            event = _completed_output_parent(entries, event, actor)
            continue
        human = candidate.get("human_readmission")
        if human is None:
            break
        if "plan" in candidate:
            inherited.append(candidate["plan"])
        marker = human["parent_marker"]
        event = next(e for e in entries if e["digest"] == marker["receipt_digest"])
    else:
        raise ValueError("Source plan ancestry exceeds the finite ordinary attempt bound")
    plan: Data = candidate["plan"]
    if (
        plan.get("version") != 1
        or plan.get("base_revision") != run["base_revision"]
        or plan.get("component") != (run.get("component_snapshot") or {}).get("path", ".")
        or plan.get("source_before") != parent["completed_cut"]["source_digest"]
        or any(digest(value) != digest(plan) for value in inherited)
    ):
        raise ValueError("The source reader context differs from the complete validated ancestor plan")
    return {"plan": copy.deepcopy(plan), "receipt_digest": event["digest"]}


def _index_semantics(
    entries: list[Data], event: Data, actor: Data, observed_parents: set[str], workspace: Path, run: Data, cut: Data
) -> Data:
    from firefly_forge.source_edit_observed_reconciliation import ObservedSourceReconciliation
    from firefly_forge.source_index_sanitation import SANITATION_RECEIPT, IndexSanitationInput, inspect_public_index

    chain = []
    from firefly_forge.source_budget_allowances import unwrap_budget_parents

    event = unwrap_budget_parents(entries, event, actor)
    for _ in range(4):
        parent, candidate = _validated_parent(entries, event, actor)
        chain.append((event, parent, candidate))
        if candidate.get("completed_output_readmission") is not None:
            event = _completed_output_parent(entries, event, actor)
            continue
        human = candidate.get("human_readmission")
        if human is None:
            break
        marker = human["parent_marker"]
        event = next(e for e in entries if e["digest"] == marker["receipt_digest"])
    else:
        raise ValueError("Index semantic ancestry exceeds the finite ordinary attempt bound")
    anchor, completed, candidate = chain[-1]
    observed = candidate.get("observed_readmission", {})
    original_digest = observed.get("original_intent_digest")
    if original_digest not in observed_parents:
        raise ValueError("Index refresh requires a completed observed source and sanitation ancestry")
    original = next(e for e in entries if e["digest"] == original_digest)
    matches = [
        e
        for e in entries
        if e["kind"] == SANITATION_RECEIPT
        and e["payload"].get("original_intent_digest") == original_digest
        and e["payload"].get("effect", {}).get("index_after") == completed["completed_cut"]["index_digest"]
    ]
    if len(matches) != 1:
        raise ValueError("Index refresh has no unique completed native sanitation receipt")
    sanitation = matches[0]
    receipt = ObservedSourceReconciliation._sanitation_existing(
        entries, original["idempotency_key"], sanitation["idempotency_key"], actor
    )
    if receipt is None:
        raise ValueError("Index refresh has no completed native sanitation intent")
    body = IndexSanitationInput.model_validate(receipt["request"])
    review, effect = receipt["candidate"], receipt["effect"]
    plan = review["index_plan"]
    intent = next(e for e in entries if e["digest"] == receipt["intent_digest"])
    anchor_intent = next(e for e in entries if e["digest"] == completed["intent_digest"])
    if (
        not original["sequence"] < intent["sequence"] < sanitation["sequence"] < anchor_intent["sequence"]
        or receipt["actor"] != actor
        or review["actor"] != actor
        or body.request_id != sanitation["idempotency_key"]
        or body.original_intent_digest != original_digest
        or body.candidate_digest != review["digest"]
        or review["digest"] != digest({k: v for k, v in review.items() if k != "digest"})
        or body.admission_contract_digest != review["admission_contract_digest"]
        or review["original_intent_digest"] != original_digest
        or review["original_request_id"] != original["idempotency_key"]
        or review["original_candidate_digest"] != original["payload"]["candidate"]["digest"]
        or review["run_id"] != run["id"]
        or receipt["no_queue_or_provider_dispatch"] is not True
        or int(datetime.fromisoformat(sanitation["created_at"]).timestamp() * 1000) > receipt["expires_at_ms"]
        or not 0
        < receipt["expires_at_ms"] - int(datetime.fromisoformat(intent["created_at"]).timestamp() * 1000)
        <= body.valid_for_seconds * 1000
        or effect["version"] != 1
        or effect["index_before"] != plan["index_before"]
        or effect["public_index_digest"] != digest(plan["public_entries"])
        or effect["metadata_digest"] != plan["metadata_before"]
        or effect["source_digest"] != plan["source_before"]
        or effect["index_after"] != receipt["completed_cut"]["index_digest"]
        or effect["metadata_digest"] != cut["metadata_digest"]
        or effect["source_digest"] != cut["source_digest"]
        or plan["base_revision"] != run["base_revision"]
        or plan["component"] != original["payload"]["candidate"]["plan"]["component"]
    ):
        raise ValueError("Index refresh lost its exact native sanitation consent, source or completed effect")
    proof = {
        "version": 1,
        "source_activation_receipt_digest": anchor["digest"],
        "sanitation_receipt_digest": sanitation["digest"],
        "sanitation_intent_digest": intent["digest"],
        "sanitation_index_digest": effect["index_after"],
        "public_index_digest": effect["public_index_digest"],
    }
    for position, (_, previous, value) in enumerate(chain[:-1]):
        ancestor = chain[position + 1][1]
        renewal = value.get("completed_output_readmission")
        if renewal is None:
            renewal = value["human_readmission"]
        inherited = renewal.get("index_semantics")
        if previous["completed_cut"]["index_digest"] != value["cut"]["index_digest"] or (
            (inherited is not None or value["cut"]["index_digest"] != ancestor["completed_cut"]["index_digest"])
            and inherited != proof
        ):
            raise ValueError("The human source receipt lost its exact inherited index semantics")
    current = inspect_public_index(workspace, plan["base_revision"], component=plan["component"])
    if (
        current["public_entries"] != plan["public_entries"]
        or current["index_before"] != cut["index_digest"]
        or current["metadata_before"] != cut["metadata_digest"]
        or current["source_before"] != cut["source_digest"]
    ):
        raise ValueError("The current public index differs from the completed native sanitation")
    return proof


class SourceHumanReadmissions:
    _input_model: Any = SourceHumanReadmissionInput
    _kind = "human_readmission"
    _resume_argument = "source_human_readmission"
    _link_effect = "human_clarification"

    def __init__(self, reconciliations: Any) -> None:
        self.service = reconciliations
        self.r = reconciliations.recoveries

    def _assert_review_current(self, review: Data) -> None:
        return None

    async def _assert_admission_current(self, run_id: str, actor: Data, review: Data) -> None:
        return None

    async def _review(
        self, run: Data, parent_request: str, actor: Data, previous_path: str, *, budget: Data | None = None
    ) -> Data:
        await self.r.store.assert_ownership()
        await self.r._settled(run, idle=True)
        entries = await self.r._entries(run["id"])
        marker = run.get("source_edit_continuation") or {}
        event = next(
            (
                e
                for e in entries
                if e["kind"] == "workspace_reconciliation_receipt" and e["idempotency_key"] == parent_request
            ),
            None,
        )
        if event is None or marker != {"request_id": parent_request, "receipt_digest": event["digest"]}:
            raise ValueError("The completed current source activation is required")
        parent, original = _validated_parent(entries, event, actor)
        completed = {e["idempotency_key"] for e in entries if e["kind"] == "workspace_reconciliation_receipt"}
        observed_parents = _ancestral_unknown(entries, event, actor)
        if any(
            e["kind"] == "workspace_reconciliation_intent"
            and e["idempotency_key"] not in completed
            and e["digest"] not in observed_parents
            for e in entries
        ):
            raise ValueError("An unknown native environment admission cannot grant another continuation")
        task = self.r._task_view(await self.r.journal._task(run["id"], "implement"))
        previous_attempt = original["previous_task"]["attempt"]
        if type(previous_attempt) is not int or (
            not 1 <= previous_attempt < 4 if budget is None else previous_attempt != budget["previous_attempt"]
        ):
            raise ValueError("The completed activation has no finite ordinary successor attempt")
        next_attempt = previous_attempt + 1
        key = uuid5(NAMESPACE_URL, f"forge:{run['id']}:implement:{previous_attempt}").hex
        previous_rounds = original["previous_run"].get("implementation_rounds")
        if budget is None:
            link = continuation_link(run, task, entries, {k: actor[k] for k in ("user_id", "authentication_method")})
        else:
            from firefly_forge.source_budget_allowances import budget_continuation_link

            link = budget_continuation_link(
                run, task, entries, {k: actor[k] for k in ("user_id", "authentication_method")}, budget
            )
        if (
            run.get("status") != "paused"
            or run.get("active_task") != "implement"
            or task.get("attempt") != previous_attempt
            or task.get("idempotency_key") != key
            or link is None
            or link["to_attempt"] != next_attempt
            or type(previous_rounds) is not int
            or previous_rounds != previous_attempt - 1
            or run.get("implementation_rounds") != previous_rounds + 1
            or not any(
                e["kind"] == "intent" and e["task_id"] == "implement" and e["idempotency_key"] == key for e in entries
            )
            or any(
                e["kind"] in {"intent", "provider_dispatch_intent"}
                and e["idempotency_key"] == uuid5(NAMESPACE_URL, f"forge:{run['id']}:implement:{next_attempt}").hex
                for e in entries
            )
        ):
            raise ValueError("Only the answered current source-only attempt may admit its exact ordinary successor")
        response = next(e["payload"] for e in entries if e["digest"] == link["response_digest"])
        if run.get("worker_response") != response:
            raise ValueError("The current answer differs from the immutable authenticated human response")
        cut = self.r._cut(run)
        index_semantics = None
        if (
            cut["index_digest"] != parent["completed_cut"]["index_digest"]
            or original.get("human_readmission", {}).get("index_semantics") is not None
        ):
            index_semantics = _index_semantics(
                entries, event, actor, observed_parents, self.r.engine.workspaces / run["id"], run, cut
            )
        compared = {"retained_state_digest"} | ({"index_digest"} if index_semantics else set())
        if {k: v for k, v in cut.items() if k not in compared} != {
            k: v for k, v in parent["completed_cut"].items() if k not in compared
        }:
            raise ValueError("The completed activation source, Git, provider or configuration changed")
        prior = {k: original["previous_run"].get(k) for k in _RETAINED}
        if (
            digest(prior) != parent["completed_cut"]["retained_state_digest"]
            or prior["implementation_rounds"] != previous_rounds
        ):
            raise ValueError("The original retained implementation cut is not exact")
        expected = {
            **prior,
            "implementation_rounds": previous_rounds + 1,
            "verification_handoff": {},
            "verification_adoption": {},
            "worker_response": run["worker_response"],
        }
        if digest(expected) != cut["retained_state_digest"]:
            raise ValueError("Retained state changed beyond the exact answered worker question")
        scope = await source_edit_scope(self.r, run, archive_cut=original["source_edit"]["archive_cut"])
        if budget is None:
            relocation = _configuration_relocation(self.r, run, original["source_edit"], scope, previous_path)
        else:
            from firefly_forge.source_budget_allowances import qualified_scope_transition

            scope, relocation = qualified_scope_transition(
                self.r, run, original["source_edit"], scope, previous_path, budget
            )
        if self.r._cut(run) != cut:
            raise ValueError("Human source review changed during native qualification")
        contract_digest = admission_contract_digest()
        if budget is not None:
            from firefly_forge.source_budget_allowances import admission_contract_digest as budget_contract_digest

            contract_digest = budget_contract_digest()
        value = {
            "version": 1,
            "run_id": run["id"],
            "parent_request_id": parent_request,
            "parent_receipt_digest": event["digest"],
            "actor": actor,
            "previous_task": task,
            "previous_run_digest": digest(run),
            "cut": cut,
            "link": link,
            "source_edit": scope,
            "configuration_relocation": relocation,
            "source_plan": validated_source_plan(entries, event, actor, run),
            "admission_contract_digest": contract_digest,
            "journal_digest": entries[-1]["digest"],
            **({"budget_review": budget} if budget is not None else {}),
        }
        if index_semantics is not None:
            value["index_semantics"] = index_semantics
        self.r.store.reject_credentials(value)
        self._assert_review_current(value)
        return {**value, "digest": digest(value)}

    async def candidate(self, run_id: str, parent_request: str, previous_catalog_path: str) -> Data:
        identifier(parent_request)
        actor = await self.r._operator(run_id)
        snapshot = self.service._actor(actor)
        async with self.r._locked(run_id, snapshot) as row:
            value = await self._review(row.data, parent_request, snapshot, previous_catalog_path)
        if await self.r._operator(run_id) != actor:
            raise AccessDenied("The native human readmission administrator changed")
        return value

    @staticmethod
    def _existing(entries: list[Data], parent_request: str, request_id: str, actor: Data) -> Data | None:
        intent = next(
            (
                e
                for e in entries
                if e["kind"] == "workspace_reconciliation_intent" and e["idempotency_key"] == request_id
            ),
            None,
        )
        if intent is None:
            return None
        value = intent["payload"]
        if (
            value.get("actor") != actor
            or value.get("candidate", {}).get("human_readmission", {}).get("parent_marker", {}).get("request_id")
            != parent_request
        ):
            raise AccessDenied("The native human readmission identity belongs to another scope")
        event = next(
            (
                e
                for e in entries
                if e["kind"] == "workspace_reconciliation_receipt" and e["idempotency_key"] == request_id
            ),
            None,
        )
        if event is None:
            raise ValueError("An unknown native human admission cannot be replayed")
        receipt: Data = event["payload"]
        if receipt.get("intent_digest") != intent["digest"] or any(
            receipt.get(k) != value[k] for k in ("request", "actor", "candidate", "previous_task")
        ):
            raise ValueError("The native human receipt does not bind its exact intent")
        return copy.deepcopy(receipt)

    async def get(self, run_id: str, parent_request: str, request_id: str) -> Data:
        identifier(parent_request)
        identifier(request_id)
        actor = await self.r._operator(run_id)
        async with self.r._locked(run_id, self.service._actor(actor)):
            value = self._existing(
                await self.r._entries(run_id), parent_request, request_id, self.service._actor(actor)
            )
            if value is None:
                raise KeyError("The human source readmission was not found")
            return value

    @staticmethod
    def _queued_task(review: Data) -> Data:
        return {**copy.deepcopy(review["previous_task"]), "status": "queued", "attempt": review["link"]["to_attempt"]}

    def _admission_lock(self, run_id: str, actor: Data) -> Any:
        return self.r._locked(run_id, actor)

    async def admit(self, run_id: str, parent_request: str, body: SourceHumanReadmissionInput) -> Data:
        from firefly_forge.configuration import redact_secrets
        from firefly_forge.engine import redact
        from firefly_forge.workspace_reconciliations import _source_patch

        body = self._input_model.model_validate(body.model_dump())
        identifier(parent_request)
        actor = await self.r._operator(run_id)
        snapshot = self.service._actor(actor)
        async with self.r._locked(run_id, snapshot):
            prior = self._existing(await self.r._entries(run_id), parent_request, body.request_id, snapshot)
            if prior is not None:
                if prior["human_request"] != body.model_dump():
                    raise ValueError("The human admission identity already contains different consent")
                return prior
        review = await self.candidate(run_id, parent_request, body.previous_catalog_path)
        if (
            body.request_id == parent_request
            or body.candidate_digest != review["digest"]
            or body.admission_contract_digest != review["admission_contract_digest"]
        ):
            raise ValueError("Human readmission requires the exact fresh candidate and contract")
        if body.valid_for_seconds > review["source_edit"]["provider_timeout"]:
            raise ValueError("Human readmission cannot exceed the retained provider deadline allowance")
        run = await self.r.store.get_run(run_id)
        patch = _source_patch(self.r.engine.workspaces / run_id, run["base_revision"])
        settings = self.r.store.configuration.load_snapshot(run["config_revision"])
        if redact_secrets(redact(patch), settings) != patch:
            raise ValueError("Human source contains a configured credential")
        await Store.admit_source_patch(self.r.store, SourcePatch(patch))
        if await self.r._operator(run_id) != actor:
            raise AccessDenied("The human administrator changed during patch admission")
        async with self._admission_lock(run_id, snapshot) as row:
            current = await self._review(row.data, parent_request, snapshot, body.previous_catalog_path)
            if (
                current != review
                or _source_patch(self.r.engine.workspaces / run_id, run["base_revision"]) != patch
                or CURRENT_PRINCIPAL.get() != actor
            ):
                raise ValueError("The exact human admission changed during credential admission")
            human = {
                "parent_marker": copy.deepcopy(row.data["source_edit_continuation"]),
                "link": review["link"],
                "previous_task": review["previous_task"],
                "admission_contract_digest": review["admission_contract_digest"],
                "source_plan_receipt_digest": review["source_plan"]["receipt_digest"],
                **(
                    {"budget_review": review["budget_review"], "review_digest": review["digest"]}
                    if "budget_review" in review
                    else {}
                ),
            }
            if "index_semantics" in review:
                human["index_semantics"] = review["index_semantics"]
            task = self._queued_task(review)
            candidate = {
                "version": 1,
                "run_id": run_id,
                "actor": snapshot,
                "previous_run": copy.deepcopy(row.data),
                "previous_run_digest": digest(row.data),
                "previous_task": task,
                "cut": review["cut"],
                "plan": copy.deepcopy(review["source_plan"]["plan"]),
                "source_edit": review["source_edit"],
                self._kind: human,
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
            payload = {
                "version": 1,
                "request": request,
                "actor": snapshot,
                "candidate": candidate,
                "previous_task": task,
                "human_request": body.model_dump(),
                "expires_at_ms": clock_ms() + body.valid_for_seconds * 1000,
                "public_patch_sha256": hashlib.sha256(patch.encode()).hexdigest(),
            }
            await self.r.store.assert_ownership()
            self._assert_review_current(review)
            intent = await self.r.journal._append(
                run_id, "implement", body.request_id, "workspace_reconciliation_intent", payload
            )
            self._assert_review_current(review)
            await self.r.journal.resume(run_id, **{self._resume_argument: body.request_id})
            self._assert_review_current(review)
            queued = await self.r.journal._run(run_id)
            self._assert_review_current(review)
            if (
                self.r._task_view(await self.r.journal._task(run_id, "implement")) != task
                or self.r._cut(queued.data) != review["cut"]
            ):
                raise ValueError("The queued human source attempt differs from its admitted exact cut")
            receipt = {
                **payload,
                "intent_digest": intent["digest"],
                "completed_cut": self.r._cut(queued.data),
                "effect": {"version": 1, self._link_effect: review["link"], "no_environment_effect": True},
            }
            await self._assert_admission_current(run_id, snapshot, review)
            self.r.store.reject_credentials(receipt)
            event = await self.r.journal._append(
                run_id, "implement", body.request_id, "workspace_reconciliation_receipt", receipt
            )
            self._assert_review_current(review)
            queued.data = {
                **queued.data,
                "source_edit_continuation": {"request_id": body.request_id, "receipt_digest": event["digest"]},
            }
            await self.r.store.run_records.save(queued)
            await self.r.store.assert_ownership()
            self._assert_review_current(review)
            if await self.r._operator(run_id) != actor or CURRENT_PRINCIPAL.get() != actor:
                raise AccessDenied("The native human administrator changed before commit")
            if (
                self.r._cut(queued.data) != review["cut"]
                or _source_patch(self.r.engine.workspaces / run_id, run["base_revision"]) != patch
                or clock_ms() >= receipt["expires_at_ms"]
            ):
                raise ValueError("The source or finite human admission changed before commit")
            self._assert_review_current(review)
            await self._assert_admission_current(run_id, snapshot, review)
            return copy.deepcopy(receipt)
