# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Stored source frontiers do not authorize restoration of their failed graphs."""

from __future__ import annotations

import copy
import hashlib
import json
from typing import Any

from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
from openai.types.chat import ChatCompletion

from firefly_forge.agent_checkpoint_models import CheckpointPayloadV2, ConsumePayload
from firefly_forge.agent_checkpoints import AgentCheckpoints
from firefly_forge.agentic_checkpoint_boundary import BoundaryCheckpoint
from firefly_forge.agentic_checkpoint_invocation import STATIC_TOOLS, invocation_cut, runtime_contract
from firefly_forge.bridge_completion_models import CompletionIntentPayload, CompletionResponsePayload
from firefly_forge.bridge_completions import _cut
from firefly_forge.git_partial_source_models import (
    PartialSourceAdmissionV1,
    PartialSourceCandidateV1,
    PartialSourceFrontierV1,
)
from firefly_forge.journal import Journal, digest

Data = dict[str, Any]
READ_ONLY = frozenset({"list_files", "read_file", "search_text", "git_diff"})
KIND = "git_partial_source_checkpoint"


def stored_frontier_proof(run: Data, task: Data, entries: list[Data]) -> Data:
    """Validate the complete original HTTP custody; never expose or consume its results."""
    if (
        run.get("agent") != "bridge"
        or run.get("status") != "failed"
        or run.get("active_task") != "implement"
        or task.get("id") != "implement"
        or task.get("status") not in {"running", "needs_reconciliation"}
        or task.get("attempt") != run.get("attempt")
        or not run.get("implementation_pending")
        or not run.get("bridge_completion_hold")
        or not entries
        or not Journal.verify_chain(entries)
    ):
        raise ValueError("Partial source requires the exact failed, held native implementation")
    kinds = ("bridge_request_intent", "agent_checkpoint", "bridge_request_response", "agent_consume")
    grouped = {kind: [e for e in entries if e["kind"] == kind] for kind in kinds}
    keys = [e["idempotency_key"] for e in grouped[kinds[0]]]
    if (
        not keys
        or len(keys) != len(set(keys))
        or any([e["idempotency_key"] for e in grouped[kind]] != keys for kind in kinds[1:])
    ):
        raise ValueError("The original completion closure is incomplete, duplicated or unknown")
    if run["bridge_completion_hold"] != {
        "version": 1,
        "reason": "graph_checkpoint_unavailable",
        "request_id": keys[0],
        "intent_digest": grouped["bridge_request_intent"][0]["digest"],
    }:
        raise ValueError("The held graph does not identify its original native completion")
    current_contract = runtime_contract()
    previous: CheckpointPayloadV2 | None = None
    invocation_id = None
    for parts in zip(*(grouped[kind] for kind in kinds), strict=True):
        intent, checkpoint, response, consume = parts
        if any(e["run_id"] != run["id"] or e["task_id"] != "implement" for e in parts):
            raise ValueError("Completion closure belongs to another run or task")
        original_intent = CompletionIntentPayload.model_validate(intent["payload"])
        payload = CheckpointPayloadV2.model_validate(checkpoint["payload"])
        bound = payload.invocation
        if bound.contract != current_contract:
            raise ValueError("The stored frontier has an unsupported runtime contract")
        if (
            payload.request_id != intent["idempotency_key"]
            or payload.intent_digest != intent["digest"]
            or original_intent.run_id != run["id"]
            or original_intent.descriptor.request_id != intent["idempotency_key"]
            or original_intent.profile_id != (run.get("model_selection") or {}).get("profile_id")
            or original_intent.project_id != run.get("project_id")
            or original_intent.repository_id != run.get("repository_id")
            or original_intent.config_field != "config_revision"
            or original_intent.config_revision != run.get("config_revision")
            or original_intent.scope_digest != _cut(run, "config_revision")
            or (previous is None and (bound.ordinal != 1 or bound.predecessor is not None))
            or bound.run_cut != invocation_cut(run)
            or bound.round != task["attempt"]
            or not bound.grants
            or not set(bound.grants) <= STATIC_TOOLS
            or intent["payload"].get("task_key") != task.get("idempotency_key")
            or intent["payload"].get("task_attempt") != task["attempt"]
            or (invocation_id is not None and bound.invocation_id != invocation_id)
            or (
                previous is not None
                and (
                    bound.ordinal != previous.invocation.ordinal + 1 or bound.predecessor != previous.checkpoint_digest
                )
            )
            or not intent["sequence"] < checkpoint["sequence"] < response["sequence"] < consume["sequence"]
        ):
            raise ValueError("The completion closure has a different invocation, task or frozen scope")
        AgentCheckpoints._validate_checkpoint(checkpoint, intent)
        body = CompletionResponsePayload.model_validate(response["payload"])
        consumed = ConsumePayload.model_validate(consume["payload"])
        raw = body.response.encode()
        if (
            body.intent_digest != intent["digest"]
            or body.request_id != intent["idempotency_key"]
            or body.response_sha256 != hashlib.sha256(raw).hexdigest()
            or body.response_bytes != len(raw)
            or consumed.request_id != intent["idempotency_key"]
            or consumed.intent_digest != intent["digest"]
            or consumed.checkpoint_record_digest != checkpoint["digest"]
            or consumed.response_record_digest != response["digest"]
            or consumed.response_sha256 != body.response_sha256
            or consumed.response_bytes != body.response_bytes
        ):
            raise ValueError("The original response and consumed record have different custody")
        completion = ChatCompletion.model_validate_json(raw, strict=True)
        if (
            completion.model != intent["payload"]["descriptor"]["model"]
            or len(completion.choices) != 1
            or completion.choices[0].finish_reason not in {"stop", "tool_calls"}
        ):
            raise ValueError("The completion does not identify one exact original model response")
        previous, invocation_id = payload, bound.invocation_id
    frontier = grouped["agent_checkpoint"][-1]
    captured = BoundaryCheckpoint.decode(
        frontier["payload"]["snapshot"].encode(), frontier["payload"]["checkpoint_digest"]
    )
    schemas = {
        item["name"]: item["parameters_json_schema"]
        for item in captured.record.contract["parameters"]["function_tools"]
    }
    names = []
    for response in grouped["bridge_request_response"]:
        if response["sequence"] <= frontier["sequence"]:
            continue
        completion = ChatCompletion.model_validate_json(response["payload"]["response"], strict=True)
        choice = completion.choices[0]
        calls = choice.message.tool_calls
        if choice.finish_reason != "tool_calls" or not calls:
            raise ValueError("Partial source needs only read-only calls after the stored frontier")
        for call in calls:
            if call.type != "function" or call.function.name not in READ_ONLY or call.function.name not in schemas:
                raise ValueError("Only supported read-only source calls may follow the stored frontier")
            arguments = json.loads(call.function.arguments)
            if not isinstance(arguments, dict) or not Draft202012Validator(schemas[call.function.name]).is_valid(
                arguments
            ):
                raise ValueError("Read-only source arguments differ from the frozen schema")
            names.append(call.function.name)
    return PartialSourceFrontierV1.model_validate(
        {
            "version": 1,
            "kind": KIND,
            "frontier_record_digest": frontier["digest"],
            "frontier_checkpoint_digest": frontier["payload"]["checkpoint_digest"],
            "invocation": {
                key: value
                for key, value in previous.invocation.model_dump(mode="json").items()
                if key not in {"brief", "backend", "frontend", "deadline_ms"}
            }
            if previous is not None
            else None,
            "invocation_digest": digest(previous.invocation.model_dump(mode="json")) if previous is not None else None,
            "completion_count": len(keys),
            "closure_digest": digest({"records": [e["digest"] for group in grouped.values() for e in group]}),
            "journal_head": entries[-1]["digest"],
            "later_tool_names": names,
            "task": copy.deepcopy({k: task.get(k) for k in ("id", "status", "attempt", "idempotency_key")}),
            "verification_status": "partial_unverified",
        }
    ).model_dump(mode="json")


class GitPartialSourceCheckpoints:
    """A source-only owner path, separate from failed feedback and graph restoration."""

    def __init__(self, deliveries: Any, *, tool_records: Any = None) -> None:
        from firefly_forge.tool_persistence import ForgeToolInvocationRepository

        self.d, self.store, self.journal = deliveries, deliveries.store, deliveries.journal
        self.tools = tool_records if tool_records is not None else ForgeToolInvocationRepository()

    async def _entries(self, run_id: str) -> list[Data]:
        from firefly_forge.store import identifier

        async with self.store._unit():
            rows = await self.journal.records.find_for_run(identifier(run_id))
            entries = [copy.deepcopy(row.data) for row in rows]
            for row, entry in zip(rows, entries, strict=True):
                scalar = dict(
                    run_id=row.run_id.hex,
                    task_id=row.task_id,
                    kind=row.kind,
                    previous_hash=row.previous_hash,
                    sequence=row.sequence,
                    idempotency_key=row.idempotency_key,
                    digest=row.digest,
                )
                if any(entry.get(key) != value for key, value in scalar.items()):
                    raise ValueError("Native partial-source scalar identity differs from its journal envelope")
        if (
            len(entries) > 10000
            or len(json.dumps(entries).encode()) > 16 * 1024 * 1024
            or not self.journal.verify_chain(entries)
        ):
            raise ValueError("Partial source requires its complete intact native journal")
        return entries

    async def _authority(self, run: Data, actor: Data) -> Data:
        from firefly_forge.git_delivery_models import GitDeliveryPolicyV1
        from firefly_forge.git_delivery_policy import GitDeliveryPolicies
        from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied, ProjectAccess

        user = CURRENT_PRINCIPAL.get()
        if user is not None and ProjectAccess.actor_snapshot(user) != actor:
            raise AccessDenied("Partial source identity changed")
        await self.d._authorize(run, actor, "execute")
        policy = GitDeliveryPolicyV1.model_validate(run.get("git_delivery_policy") or {})
        saved = await GitDeliveryPolicies(self.store).snapshot(run.get("project_id"))
        if (
            not policy.enabled
            or not saved
            or saved["policy"] != run.get("git_delivery_policy")
            or saved["digest"] != run.get("git_delivery_policy_digest")
        ):
            raise ValueError("The original enabled Git consent is no longer current")
        if policy.remote == "draft_pr":
            await self.d._authorize(run, actor, "release")
        await self.store.assert_ownership()
        return saved

    def _quiescent(self, run: Data) -> None:
        active = self.d.engine.active.get(run["id"])
        if (
            self.d.engine.stopping
            or (active is not None and not active.done())
            or run.get("cancel_request", {}).get("status") == "requested"
            or (run.get("provider_recovery") or {}).get("status")
            in {"reserved", "executing", "unknown", "needs_reconciliation"}
            or (run.get("worker_request") or {}).get("status") == "pending"
        ):
            raise ValueError("A live, cancelled or unknown owner prevents partial source settlement")

    async def _effects(self, run: Data, entries: list[Data], frontier_sequence: int) -> str:
        from firefly_forge.runner_factory_models import validate_factory_pool
        from firefly_forge.sandbox_command_slots import COMMAND_CONTRACT, _canonical, _complete
        from firefly_forge.sandbox_models import capture_sandbox, validate_sandbox_snapshot
        from firefly_forge.store import identifier

        async with self.store._unit():
            commands = await self.journal.sandboxes.for_run(identifier(run["id"]))
            tools = await self.tools.for_run(identifier(run["id"]))
        if {
            e["payload"].get("execution_id")
            for e in entries
            if e["kind"] in {"sandbox_intent", "sandbox_receipt", "sandbox_reconcile"}
        } != {r.id.hex for r in commands}:
            raise ValueError("Orphan command custody prevents partial source settlement")
        snapshot = run.get("sandbox_snapshot") or capture_sandbox(None)
        policy = (
            validate_factory_pool(snapshot) if snapshot.get("mode") == "pool" else validate_sandbox_snapshot(snapshot)
        )
        values = []
        for row in commands:
            value = _canonical(row, entries, run["id"])
            effects = [
                e
                for e in entries
                if e["kind"] in {"sandbox_intent", "sandbox_receipt", "sandbox_reconcile"}
                and e["payload"].get("execution_id") == row.id.hex
            ]
            if (
                value["request"].get("executor_contract") != COMMAND_CONTRACT
                or row.snapshot_digest != snapshot["digest"]
                or row.ordinal > policy.command_limit
                or not _complete(row, value)
                or max(e["sequence"] for e in effects) >= frontier_sequence
            ):
                raise ValueError("Incomplete or later command effects prevent partial source settlement")
            values.append(value)
        if {
            e["idempotency_key"]
            for e in entries
            if e["kind"] in {"tool_intent", "tool_receipt", "tool_unknown", "tool_reconciled"}
        } != {r.correlation_id for r in tools}:
            raise ValueError("Orphan tool custody prevents partial source settlement")
        tool_values = []
        for row in tools:
            intents = [e for e in entries if e["kind"] == "tool_intent" and e["idempotency_key"] == row.correlation_id]
            outcomes = [
                e
                for e in entries
                if e["kind"] in {"tool_receipt", "tool_unknown", "tool_reconciled"}
                and e["idempotency_key"] == row.correlation_id
            ]
            evidence = {k: copy.deepcopy(row.data[k]) for k in ("result", "error", "observation") if k in row.data}
            if (
                row.run_id.hex != run["id"]
                or row.status not in {"succeeded", "rejected"}
                or len(intents) != 1
                or len(outcomes) != 1
                or intents[0]["task_id"] != row.task_id
                or outcomes[0]["task_id"] != row.task_id
                or row.data.get("admission") != intents[0]["payload"]
                or any(
                    intents[0]["payload"].get(key) != run.get(key)
                    for key in ("repository_id", "project_id", "component_id")
                )
                or intents[0]["payload"].get("run_id") != run["id"]
                or intents[0]["payload"].get("tool") != row.tool
                or intents[0]["payload"].get("task_id") != row.task_id
                or intents[0]["payload"].get("correlation_id") != row.correlation_id
                or row.data.get("request_digest") != digest(intents[0]["payload"])
                or outcomes[0]["kind"] != "tool_receipt"
                or outcomes[0]["payload"]
                != {"correlation_id": row.correlation_id, "tool": row.tool, "status": row.status, **evidence}
                or not intents[0]["sequence"] < outcomes[0]["sequence"] < frontier_sequence
            ):
                raise ValueError("Unsettled or later tool effects prevent partial source settlement")
            tool_values.append({"id": row.id.hex, "status": row.status, "data": row.data})
        return digest({"commands": values, "tools": tool_values})

    @staticmethod
    def _run_cut(run: Data) -> str:
        # Only native source-delivery projections may advance; all failed graph data remains bound.
        value = {
            key: item
            for key, item in run.items()
            if key
            not in {
                "events",
                "updated_at",
                "git_delivery_head",
                "git_delivery_summary",
                "git_delivery_draft",
                "git_partial_source_checkpoint",
            }
        }
        value["checkpoint_revision"] = run.get("checkpoint_revision")
        return digest(value)

    async def candidate(self, run_id: str, actor: Data) -> Data:
        from firefly_forge.agentic_checkpoint_invocation import workspace_cut
        from firefly_forge.source_snapshot import git_index_identity
        from firefly_forge.verification_models import verification_source

        run = await self.store.get_run(run_id)
        if (
            run.get("git_partial_source_checkpoint")
            or run.get("git_failed_unit_adoption")
            or run.get("git_delivery_head")
            or run.get("git_delivery_summary") is not None
            or run.get("git_delivery_draft") is not None
        ):
            raise ValueError("Inspect the retained partial source request; do not replace its custody")
        self._quiescent(run)
        saved = await self._authority(run, actor)
        entries = await self._entries(run_id)
        if any(e["kind"].startswith("git_delivery_") or e["kind"] == KIND for e in entries):
            raise ValueError("An existing Git effect must be inspected rather than bypassed")
        task = next(t for t in (await self.journal.plan(run_id))["tasks"] if t["id"] == "implement")
        proof = stored_frontier_proof(run, task, entries)
        frontier_sequence = next(e["sequence"] for e in entries if e["digest"] == proof["frontier_record_digest"])
        effects = await self._effects(run, entries, frontier_sequence)
        # Scan decoded histories as well as encoded WAL; compression must never conceal credentials.
        scanner = await self.store.source_patch_scanner()
        for entry in entries:
            documents = [json.dumps(entry, ensure_ascii=False).encode()]
            if entry["kind"] == "agent_checkpoint":
                documents.append(
                    BoundaryCheckpoint.decode(
                        entry["payload"]["snapshot"].encode(), entry["payload"]["checkpoint_digest"]
                    ).canonical_payload
                )
            for document in documents:
                for start in range(0, len(document), 65536):
                    await scanner(document[start : start + 65536], False)
                scanner.finish_document()
        await scanner(b"", True)
        root = self.d.workspace(run)
        cut = await workspace_cut(self.store, root)
        if (
            cut != proof["invocation"]["workspace"]
            or cut["head"] != (run.get("checkpoint_revision") or run.get("base_revision"))
            or cut["metadata"] != run.get("git_metadata_digest")
        ):
            raise ValueError("Current source differs from the latest stored native frontier")
        index = git_index_identity(root).sha256
        source = verification_source(root)
        value = dict(
            version=1,
            run_id=run_id,
            actor=copy.deepcopy(actor),
            policy_digest=saved["digest"],
            expected_run_digest=self._run_cut(run),
            journal_digest=entries[-1]["digest"],
            source_digest=source,
            index_digest=index,
            expected_head=cut["head"],
            workspace=cut,
            frontier_record_digest=proof["frontier_record_digest"],
            proof=proof,
            effects_digest=effects,
            original_checkpoint_revision=run.get("checkpoint_revision"),
        )
        await self.store.admit_credentials(value)
        await scanner.revalidate()
        await self.revalidate(run_id, actor, value, staged_tree=None)
        return PartialSourceCandidateV1.model_validate({**value, "digest": digest(value)}).model_dump(mode="json")

    @staticmethod
    def compare(candidate: Data, body: Any) -> None:
        if any(
            getattr(body, key) != candidate[field]
            for key, field in {
                "candidate_digest": "digest",
                "policy_digest": "policy_digest",
                "expected_run_digest": "expected_run_digest",
                "journal_digest": "journal_digest",
                "source_digest": "source_digest",
                "frontier_record_digest": "frontier_record_digest",
            }.items()
        ):
            raise ValueError("The reviewed partial source frontier changed")

    async def revalidate(self, run_id: str, actor: Data, candidate: Data, *, staged_tree: str | None) -> None:
        from firefly_forge.git_workspace import metadata_digest
        from firefly_forge.source_snapshot import git_index_identity
        from firefly_forge.verification_models import verification_source
        from firefly_forge.workspace_reconciliation import _git

        run = await self.store.get_run(run_id)
        self._quiescent(run)
        await self._authority(run, actor)
        if self._run_cut(run) != candidate["expected_run_digest"]:
            raise ValueError("Failed graph, task, grants or source scope changed")
        entries = await self._entries(run_id)
        if not entries or entries[-1]["digest"] != candidate["journal_digest"]:
            raise ValueError("The native source-frontier journal changed")
        task = next(t for t in (await self.journal.plan(run_id))["tasks"] if t["id"] == "implement")
        if {k: task.get(k) for k in candidate["proof"]["task"]} != candidate["proof"]["task"]:
            raise ValueError("The retained failed task changed")
        sequence = next(e["sequence"] for e in entries if e["digest"] == candidate["frontier_record_digest"])
        if await self._effects(run, entries, sequence) != candidate["effects_digest"]:
            raise ValueError("The retained command or tool inventory changed")
        root = self.d.workspace(run)
        if (
            str(root.resolve()) != candidate["workspace"]["path"]
            or metadata_digest(root) != candidate["workspace"]["metadata"]
            or _git(root, "rev-parse", "HEAD").decode().strip() != candidate["expected_head"]
            or _git(root, "symbolic-ref", "--short", "HEAD").decode().strip() != run["branch"]
            or verification_source(root) != candidate["source_digest"]
            or (staged_tree is None and git_index_identity(root).sha256 != candidate["index_digest"])
            or (staged_tree is not None and _git(root, "write-tree").decode().strip() != staged_tree)
        ):
            raise ValueError("The owned Git/source frontier changed")
        await self._authority(await self.store.get_run(run_id), actor)

    async def admit(self, run_id: str, request_id: str, actor: Data, body: Any) -> None:
        from firefly_forge.git_diff_models import GitDiffBudget
        from firefly_forge.source_snapshot import SourceDiffSnapshotV1

        candidate = await self.candidate(run_id, actor)
        self.compare(candidate, body)
        run = await self.store.get_run(run_id)
        settings = self.store.configuration.load_snapshot(run["config_revision"])
        budget = GitDiffBudget.model_validate(run["git_delivery_policy"].get("diff_budget") or {})
        _, source, tree = await self.d.engine._snapshot(
            self.d.workspace(run),
            candidate["expected_head"],
            secrets=settings,
            expected_head=candidate["expected_head"],
            budget=budget,
        )
        if not isinstance(source, SourceDiffSnapshotV1) or source.artifact.file_count == 0:
            raise ValueError("Partial source requires exact nonempty native diff evidence")
        async with self.d._owned(run_id, actor):
            await self.revalidate(run_id, actor, candidate, staged_tree=tree)
            event = await self.journal._append(
                run_id,
                "checkpoint",
                request_id,
                KIND,
                PartialSourceAdmissionV1.model_validate(
                    dict(
                        version=1,
                        request_id=request_id,
                        actor=copy.deepcopy(actor),
                        request=body.model_dump(mode="json"),
                        candidate=candidate,
                        staged_tree=tree,
                        source_evidence=source.artifact.model_dump(mode="json"),
                        verification_status="partial_unverified",
                    )
                ).model_dump(mode="json"),
            )
            await self.store.update(
                run_id, git_partial_source_checkpoint={"request_id": request_id, "receipt_digest": event["digest"]}
            )

    async def guard(self, run: Data, actor: Data) -> None:
        """Validate the owned staging/commit phase without reopening native graph history."""
        from firefly_forge.git_partial_source_models import GitPartialSourceConsentV1
        from firefly_forge.git_workspace import metadata_digest
        from firefly_forge.verification_models import verification_source
        from firefly_forge.workspace_reconciliation import _git

        marker = run.get("git_partial_source_checkpoint") or {}
        entries = await self._entries(run["id"])
        matches = [
            e
            for e in entries
            if e["kind"] == KIND
            and e["idempotency_key"] == marker.get("request_id")
            and e["digest"] == marker.get("receipt_digest")
        ]
        if len(matches) != 1:
            raise ValueError("Partial source lacks its exact server-owned settlement receipt")
        admission = matches[0]
        payload = PartialSourceAdmissionV1.model_validate(admission["payload"]).model_dump(mode="json")
        candidate = payload["candidate"]
        if payload["request_id"] != marker.get("request_id") or candidate["run_id"] != run["id"]:
            raise ValueError("Partial source receipt belongs to another request or run")
        self.compare(candidate, GitPartialSourceConsentV1.model_validate(payload["request"]))
        if (
            candidate["digest"] != digest({k: v for k, v in candidate.items() if k != "digest"})
            or payload["actor"] != actor
            or candidate["actor"] != actor
        ):
            raise ValueError("Partial source proof or original actor differs")
        before = [e for e in entries if e["sequence"] < admission["sequence"]]
        if (
            not before
            or before[-1]["digest"] != candidate["journal_digest"]
            or any(
                e["kind"]
                not in {
                    "git_delivery_intent",
                    "git_delivery_receipt",
                    "git_delivery_remote_intent",
                    "git_delivery_remote_receipt",
                    "git_delivery_observation",
                    "git_delivery_draft",
                    "git_delivery_hold",
                }
                or e["payload"].get("request_id") != payload["request_id"]
                for e in entries
                if e["sequence"] > admission["sequence"]
            )
        ):
            raise ValueError("A later effect changed the partial source custody")
        receipts = self.d._records(entries)
        expected_projection = receipts[-1]["revision"] if receipts else candidate["original_checkpoint_revision"]
        if run.get("checkpoint_revision") != expected_projection or run.get("git_delivery_head") != (
            receipts[-1]["revision"] if receipts else None
        ):
            raise ValueError("Source delivery projections differ from their native checkpoint receipt")
        # Projections are mutable only when their exact native delivery records account for them.
        if len(receipts) > 1:
            raise ValueError("Partial source has more than its one owned checkpoint receipt")
        summaries: list[Any] = [None]
        remote_intents = [e for e in entries if e["kind"] == "git_delivery_remote_intent"]
        remote = self.d._remote_state(entries, allow_pending=True)
        if receipts:
            receipt = receipts[-1]
            local = {key: receipt[key] for key in ("revision", "source_tree", "sequence", "remote_status", "pr_url")}
            summaries.append(local)
            if remote_intents:
                if len(remote_intents) != 1 or any(
                    remote_intents[0]["payload"].get(key) != receipt[key]
                    for key in ("request_id", "revision", "checkpoint_digest")
                ):
                    raise ValueError("Remote projection lacks its exact owned checkpoint intent")
                summaries = [local, {**local, "remote_status": "publishing"}]
            if remote:
                if remote["remote_status"] != "draft" or remote["revision"] != receipt["revision"]:
                    raise ValueError("Partial source projection cannot claim final-ready publication")
                summaries.append({**local, "remote_status": "draft", "pr_url": remote["reference"]["url"]})
        elif remote_intents or remote:
            raise ValueError("Remote projection has no owned source checkpoint receipt")
        if run.get("git_delivery_summary") not in summaries or run.get("git_delivery_draft") != (
            remote["reference"] if remote else None
        ):
            raise ValueError("Delivery projections differ from their exact native source receipts")
        normalized = copy.deepcopy(run)
        normalized["checkpoint_revision"] = candidate["original_checkpoint_revision"]
        self._quiescent(run)
        if self._run_cut(normalized) != candidate["expected_run_digest"]:
            raise ValueError("Partial source must preserve its failed graph and original counters")
        await self._authority(run, actor)
        task = next(t for t in (await self.journal.plan(run["id"]))["tasks"] if t["id"] == "implement")
        if {k: task.get(k) for k in candidate["proof"]["task"]} != candidate["proof"]["task"]:
            raise ValueError("The failed task was changed by source settlement")
        sequence = next(e["sequence"] for e in before if e["digest"] == candidate["frontier_record_digest"])
        if await self._effects(run, before, sequence) != candidate["effects_digest"]:
            raise ValueError("Tool or command custody changed after partial-source admission")
        root = self.d.workspace(run)
        head = _git(root, "rev-parse", "HEAD").decode().strip()
        if head != candidate["expected_head"] and (
            _git(root, "rev-parse", "HEAD^").decode().strip() != candidate["expected_head"]
            or _git(root, "rev-parse", "HEAD^{tree}").decode().strip() != payload["staged_tree"]
        ):
            raise ValueError("Partial source committed a different parent or tree")
        checks = {
            "path": str(root.resolve()) == candidate["workspace"]["path"],
            "metadata": metadata_digest(root) == candidate["workspace"]["metadata"],
            "staged tree": _git(root, "write-tree").decode().strip() == payload["staged_tree"],
            "branch": _git(root, "symbolic-ref", "--short", "HEAD").decode().strip() == run["branch"],
            "source": verification_source(root) == candidate["source_digest"],
        }
        if not all(checks.values()):
            raise ValueError(
                "The admitted partial source changed during its Git effect: "
                + ", ".join(k for k, v in checks.items() if not v)
            )
        await self._authority(await self.store.get_run(run["id"]), actor)
