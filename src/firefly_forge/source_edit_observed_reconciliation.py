# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Explicit native readmission of a proven existing environment effect, never its replay."""

from __future__ import annotations

import copy
import hashlib
from pathlib import Path
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator

from firefly_forge.journal import digest
from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied
from firefly_forge.source_edit_models import SourceEditContinuationInput
from firefly_forge.source_patch_admission import SourcePatch
from firefly_forge.store import Store, identifier
from firefly_forge.workspace_reconciliation import IGNORE_POLICY, _git, _regular

Data = dict[str, Any]


class ObservedSourceReadmissionInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    version: Literal[1]
    request_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    original_intent_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    observed_candidate_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    admission_contract_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    previous_catalog_path: str = Field(min_length=1, max_length=4096)
    valid_for_seconds: int = Field(ge=1, le=1800)
    acknowledge_observed_effect_without_replay: Literal[True]
    acknowledge_same_task_and_original_budgets: Literal[True]
    acknowledge_new_finite_activation_deadline: Literal[True]
    acknowledge_original_scm_outcome_remains_unknown: Literal[True]

    @field_validator(
        "acknowledge_observed_effect_without_replay",
        "acknowledge_same_task_and_original_budgets",
        "acknowledge_new_finite_activation_deadline",
        "acknowledge_original_scm_outcome_remains_unknown",
        mode="before",
    )
    @classmethod
    def exact_acknowledgement(cls, value: Any) -> bool:
        if value is not True:
            raise ValueError("Explicit native observed-effect acknowledgements are required")
        return value

    @field_validator("version", mode="before")
    @classmethod
    def exact_version(cls, value: Any) -> int:
        if type(value) is not int or value != 1:
            raise ValueError("Observed-effect readmission version is invalid")
        return value


def admission_contract_digest() -> str:
    root = Path(__file__).parent
    return digest(
        {
            name: hashlib.sha256((root / name).read_bytes()).hexdigest()
            for name in (
                "source_patch_admission.py",
                "source_index_sanitation.py",
                "oauth_credential_fingerprints.py",
                "connection_oauth.py",
                "store.py",
                "engine.py",
                "provider_recoveries.py",
                "workspace_reconciliations.py",
                "source_edit_observed_reconciliation.py",
                "workspace_reconciliation_controllers.py",
                "git_workspace.py",
            )
        }
    )


def _configuration_digest(profile: Any, permissions: Data, catalog_path: str) -> str:
    from firefly_forge.source_edit_cli_policy import DISABLED_NATIVE_AGENTS
    from firefly_forge.source_edit_filesystem_policy import EMPTY_NATIVE_HOOKS

    return digest(
        {
            "features": dict(profile.configuration_features),
            "tools": {"web_search": None},
            "model": profile.model,
            "model_provider": profile.native_provider,
            "model_catalog_path": catalog_path,
            "model_catalog_sha256": profile.model_catalog_sha256,
            "permissions": permissions,
            "approval_policy": "never",
            "agents": DISABLED_NATIVE_AGENTS,
            "hooks": EMPTY_NATIVE_HOOKS,
        }
    )


def _configuration_relocation(recoveries: Any, run: Data, old: Data, current: Data, previous_path: str) -> Data:
    from firefly_forge.source_edit_filesystem_policy import build_named_permissions, source_private_roots
    from firefly_forge.source_edit_model_catalog import model_catalog_path
    from firefly_forge.source_edit_qualification import qualified_profile

    if (
        type(previous_path) is not str
        or not 1 <= len(previous_path) <= 4096
        or any(c in previous_path for c in "\x00\n\r")
    ):
        raise ValueError("The previous package-owned catalog path is invalid")
    old_path = Path(previous_path)
    suffix = Path("resources/native/codex-0.163.0/source-only-model.json")
    if not old_path.is_absolute() or ".." in old_path.parts or old_path.parts[-len(suffix.parts) :] != suffix.parts:
        raise ValueError("The previous package-owned catalog path is required")
    if {k: v for k, v in old.items() if k != "configuration_proof"} != {
        k: v for k, v in current.items() if k != "configuration_proof"
    }:
        raise ValueError("Observed readmission cannot change the original capability or budgets")
    before, after = old["configuration_proof"], current["configuration_proof"]
    if {k: v for k, v in before.items() if k != "configuration_digest"} != {
        k: v for k, v in after.items() if k != "configuration_digest"
    }:
        raise ValueError("Observed readmission cannot change native permissions or managed controls")
    profile = qualified_profile(
        current["executable"], recoveries.store.configuration.load_snapshot(run["config_revision"])["FORGE_CODEX_MODEL"]
    )
    permissions = build_named_permissions(
        recoveries.engine.workspaces / run["id"], source_private_roots(recoveries.store.configuration.directory)
    )
    current_path = str(model_catalog_path(profile.model))
    if (
        _configuration_digest(profile, permissions, previous_path) != before["configuration_digest"]
        or _configuration_digest(profile, permissions, current_path) != after["configuration_digest"]
    ):
        raise ValueError("The configuration proof differs beyond the exact catalog path relocation")
    return {
        "previous_catalog_path": previous_path,
        "current_catalog_path": current_path,
        "catalog_sha256": profile.model_catalog_sha256,
        "previous_configuration_digest": before["configuration_digest"],
        "current_configuration_digest": after["configuration_digest"],
    }


def _observed_environment(recoveries: Any, run: Data, candidate: Data) -> tuple[Path, str]:
    from firefly_forge.git_workspace import metadata_digest, metadata_digest_before_exclude
    from firefly_forge.verification_models import verification_source

    workspace = recoveries.engine.workspaces / run["id"]
    plan = candidate["plan"]
    metadata = metadata_digest(workspace)
    exclude = _regular(workspace / ".git/info/exclude", 65536)
    if (
        not exclude.endswith(IGNORE_POLICY)
        or hashlib.sha256(exclude[: -len(IGNORE_POLICY)]).hexdigest() != plan["exclude_before"]
        or _git(workspace, "rev-parse", "HEAD").strip().decode() != plan["base_revision"]
        or verification_source(workspace) != plan["source_before"]
        or _git(workspace, "ls-files", "-z", "--", *plan["aliases"])
    ):
        raise ValueError("The original environment effect is not completely observed")
    if metadata_digest_before_exclude(workspace, exclude[: -len(IGNORE_POLICY)]) != plan["metadata_before"]:
        raise ValueError("Protected Git metadata changed beyond the exact reviewed exclude transition")
    for name, expected in plan["alias_identities"].items():
        info = (workspace / name).lstat()
        if expected != {
            "device": info.st_dev,
            "inode": info.st_ino,
            "mode": info.st_mode,
            "size": info.st_size,
            "mtime_ns": info.st_mtime_ns,
        }:
            raise ValueError("An observed dependency alias differs from the original intent")
    return workspace, metadata


def _observed_cut(recoveries: Any, run: Data, candidate: Data, metadata: str) -> Data:
    from firefly_forge.git_workspace import metadata_digest

    workspace = recoveries.engine.workspaces / run["id"]
    cut = recoveries._cut({**run, "git_metadata_digest": metadata})
    if {k: v for k, v in cut.items() if k not in {"metadata_digest", "index_digest"}} != {
        k: v for k, v in candidate["cut"].items() if k not in {"metadata_digest", "index_digest"}
    } or metadata_digest(workspace) != metadata:
        raise ValueError("Observed source, Git identity or execution authority changed")
    return cast(Data, cut)


def _observed_effect(recoveries: Any, run: Data, candidate: Data) -> tuple[Data, Data]:
    from firefly_forge.source_index_sanitation import inspect_public_index

    workspace, metadata = _observed_environment(recoveries, run, candidate)
    plan = candidate["plan"]
    inspect_public_index(workspace, plan["base_revision"])
    cut = _observed_cut(recoveries, run, candidate, metadata)
    return {
        "version": 1,
        "metadata_before": plan["metadata_before"],
        "metadata_after": metadata,
        "index_after": cut["index_digest"],
        "source_digest": plan["source_before"],
        "aliases": plan["aliases"],
    }, cut


class ObservedSourceReconciliation:
    def __init__(self, reconciliations: Any) -> None:
        self.service = reconciliations
        self.r = reconciliations.recoveries

    async def _review(
        self,
        run: Data,
        original_request: str,
        actor: Data,
        previous_catalog_path: str,
        *,
        index_sanitation: bool = False,
        sanitation_reservation: str | None = None,
    ) -> tuple[Data, Data]:
        from firefly_forge.source_edit_continuations import source_edit_scope
        from firefly_forge.workspace_reconciliation_models import queued_environment_scope

        entries = await self.r._entries(run["id"])
        original = next(
            (
                e
                for e in entries
                if e["kind"] == "workspace_reconciliation_intent" and e["idempotency_key"] == original_request
            ),
            None,
        )
        if (
            original is None
            or original["payload"]["actor"] != actor
            or "source_edit" not in original["payload"]["candidate"]
        ):
            raise AccessDenied("Only the original current native source administrator can readmit this effect")
        if any(
            e["kind"] in {"workspace_reconciliation_receipt", "workspace_reconciliation_queued"}
            and e["idempotency_key"] == original_request
            or e["kind"] == "workspace_observed_readmission"
            and e["payload"].get("original_intent_digest") == original["digest"]
            for e in entries
        ):
            raise ValueError("The observed native intent already has a completion or readmission")
        candidate = original["payload"]["candidate"]
        original_body = SourceEditContinuationInput.model_validate(original["payload"]["request"])
        if (
            original["run_id"] != run["id"]
            or original_body.request_id != original_request
            or original_body.candidate_digest != candidate["digest"]
            or original["payload"]["previous_task"] != candidate["previous_task"]
            or candidate["plan"]["metadata_before"] != run.get("git_metadata_digest")
        ):
            raise ValueError("The original native intent does not bind this exact failed source transition")
        if (
            candidate["digest"] != digest({k: v for k, v in candidate.items() if k != "digest"})
            or digest(self.service._original(run)) != candidate["previous_run_digest"]
        ):
            raise ValueError("The original reviewed candidate or failed run changed")
        await self.r._settled(run, idle=True, index_sanitation_reservation=sanitation_reservation)
        task = self.r._task_view(await self.r.journal._task(run["id"], "implement"))
        if task != candidate["previous_task"]:
            raise ValueError("The original existing implementation task changed")
        if any(
            entry["kind"] == "provider_dispatch_intent" and entry["sequence"] > original["sequence"]
            for entry in entries
        ):
            raise ValueError("An observed effect cannot be readmitted after any new provider dispatch")
        scope = await source_edit_scope(self.r, run)
        relocation = _configuration_relocation(self.r, run, candidate["source_edit"], scope, previous_catalog_path)
        budget = queued_environment_scope(
            run,
            task,
            entries,
            verification_allowance=await self.r.journal.verification_continuation_allowance(run["id"]),
            human_allowance=await self.r.journal.human_clarification_continuation_allowance(run["id"]),
            remaining=scope["remaining_commands"],
            final_checks=scope["final_checks"],
        )
        index_plan = None
        if index_sanitation:
            from firefly_forge.source_index_sanitation import sanitation_plan

            workspace, metadata = _observed_environment(self.r, run, candidate)
            index_plan = sanitation_plan(
                workspace, candidate["plan"]["base_revision"], component=candidate["plan"]["component"]
            )
            cut = _observed_cut(self.r, run, candidate, metadata)
            if (
                index_plan["index_before"] != cut["index_digest"]
                or index_plan["source_before"] != candidate["plan"]["source_before"]
            ):
                raise ValueError("The reviewed generated index does not bind the original public source")
            effect = {
                "version": 1,
                "metadata_before": candidate["plan"]["metadata_before"],
                "metadata_after": metadata,
                "index_after": cut["index_digest"],
                "source_digest": candidate["plan"]["source_before"],
                "aliases": candidate["plan"]["aliases"],
            }
        else:
            effect, cut = _observed_effect(self.r, run, candidate)
        value = {
            "version": 1,
            "run_id": run["id"],
            "original_request_id": original_request,
            "original_intent_digest": original["digest"],
            "original_candidate_digest": candidate["digest"],
            "actor": actor,
            "previous_run_digest": candidate["previous_run_digest"],
            "previous_task": task,
            "effect": effect,
            "cut": cut,
            "budget": budget,
            "source_scope_digest": digest(scope),
            "configuration_relocation": relocation,
            "admission_contract_digest": admission_contract_digest(),
            "scm_provenance_digest": digest(original["payload"].get("scm_source_activation")),
            "original_expires_at_ms": original["payload"]["expires_at_ms"],
        }
        if index_plan is not None:
            value.update(kind="index_sanitation_candidate_v1", index_plan=index_plan)
        return {**value, "digest": digest(value)}, {"original": original, "source_scope": scope}

    async def candidate(self, run_id: str, original_request: str, previous_catalog_path: str) -> Data:
        identifier(original_request)
        actor = await self.r._operator(run_id)
        snapshot = self.service._actor(actor)
        async with self.r._locked(run_id, snapshot) as row:
            review, _ = await self._review(row.data, original_request, snapshot, previous_catalog_path)
        if await self.r._operator(run_id) != actor:
            raise AccessDenied("The observed-effect administrator changed")
        return review

    async def admit(self, run_id: str, original_request: str, body: ObservedSourceReadmissionInput) -> Data:
        from firefly_forge.configuration import redact_secrets
        from firefly_forge.engine import redact
        from firefly_forge.source_edit_continuations import clock_ms
        from firefly_forge.workspace_reconciliations import _source_patch

        body = ObservedSourceReadmissionInput.model_validate(body.model_dump())
        actor = await self.r._operator(run_id)
        snapshot = self.service._actor(actor)
        review = await self.candidate(run_id, original_request, body.previous_catalog_path)
        if (
            body.request_id == original_request
            or body.observed_candidate_digest != review["digest"]
            or body.original_intent_digest != review["original_intent_digest"]
            or body.admission_contract_digest != review["admission_contract_digest"]
        ):
            raise ValueError("Readmission requires a distinct request and the exact observed effect")
        run = await self.r.store.get_run(run_id)
        patch = _source_patch(self.r.engine.workspaces / run_id, run["base_revision"])
        settings = self.r.store.configuration.load_snapshot(run["config_revision"])
        if redact_secrets(redact(patch), settings) != patch:
            raise ValueError("Observed source contains a configured credential")
        await Store.admit_source_patch(self.r.store, SourcePatch(patch))
        if await self.r._operator(run_id) != actor:
            raise AccessDenied("The observed-effect administrator changed during admission")
        async with self.r._locked(run_id, snapshot) as row:
            current, retained = await self._review(row.data, original_request, snapshot, body.previous_catalog_path)
            if (
                current != review
                or CURRENT_PRINCIPAL.get() != actor
                or _source_patch(self.r.engine.workspaces / run_id, run["base_revision"]) != patch
            ):
                raise ValueError("The observed effect or current authority changed during admission")
            entries = await self.r._entries(run_id)
            if any(e["idempotency_key"] == body.request_id for e in entries):
                raise ValueError("Readmission request identity is already retained")
            old = retained["original"]
            previous = old["payload"]["candidate"]
            scope = retained["source_scope"]
            if body.valid_for_seconds > scope["provider_timeout"]:
                raise ValueError("Readmission cannot exceed the original provider deadline allowance")
            deadline = clock_ms() + body.valid_for_seconds * 1000
            candidate = {
                **copy.deepcopy(previous),
                "actor": snapshot,
                "source_edit": scope,
                "cut": review["cut"],
                "budget": review["budget"],
                "journal_digest": entries[-1]["digest"],
                "observed_readmission": {
                    "original_intent_digest": old["digest"],
                    "observed_candidate_digest": review["digest"],
                    "admission_contract_digest": review["admission_contract_digest"],
                    "scm_provenance_digest": review["scm_provenance_digest"],
                },
            }
            candidate.pop("digest")
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
            renewal = {
                "version": 1,
                "request": body.model_dump(),
                "actor": snapshot,
                "original_intent_digest": old["digest"],
                "observed_effect": review,
                "new_candidate_digest": candidate["digest"],
                "previous_expires_at_ms": old["payload"]["expires_at_ms"],
                "new_expires_at_ms": deadline,
                "no_provider_dispatch": True,
            }
            self.r.store.reject_credentials(renewal)
            await self.r.store.assert_ownership()
            event = await self.r.journal._append(
                run_id, "implement", body.request_id, "workspace_observed_readmission", renewal
            )
            payload = {
                **copy.deepcopy(old["payload"]),
                "request": request,
                "candidate": candidate,
                "expires_at_ms": deadline,
                "readmission_digest": event["digest"],
            }
            payload.pop("scm_source_activation", None)
            intent = await self.r.journal._append(
                run_id, "implement", body.request_id, "workspace_reconciliation_intent", payload
            )
            receipt = {
                **payload,
                "intent_digest": intent["digest"],
                "effect": review["effect"],
                "completed_cut": review["cut"],
            }
            self.r.store.reject_credentials(receipt)
            await self.r.journal._append(
                run_id, "implement", body.request_id, "workspace_reconciliation_receipt", receipt
            )
            await self.r.journal.queue_environment_reconciled(run_id, body.request_id)
            return copy.deepcopy(receipt)

    @staticmethod
    def _sanitation_existing(entries: list[Data], original_request: str, request_id: str, actor: Data) -> Data | None:
        from firefly_forge.source_index_sanitation import SANITATION_INTENT, SANITATION_RECEIPT

        intent = next(
            (e for e in entries if e["kind"] == SANITATION_INTENT and e["idempotency_key"] == request_id), None
        )
        if intent is None:
            return None
        value = intent["payload"]
        if value["actor"] != actor or value["original_request_id"] != original_request:
            raise AccessDenied("Only the original current native administrator can read this index transition")
        receipt = next(
            (e for e in entries if e["kind"] == SANITATION_RECEIPT and e["idempotency_key"] == request_id), None
        )
        if receipt is None:
            return {**copy.deepcopy(value), "status": "needs_reconciliation", "intent_digest": intent["digest"]}
        completed = cast(Data, receipt["payload"])
        if completed.get("intent_digest") != intent["digest"] or any(
            completed.get(k) != value[k]
            for k in ("request", "actor", "original_intent_digest", "original_request_id", "candidate", "expires_at_ms")
        ):
            raise ValueError("Native index sanitation completion does not bind its exact intent")
        return copy.deepcopy(completed)

    async def index_sanitation_get(self, run_id: str, original_request: str, request_id: str) -> Data:
        identifier(original_request)
        identifier(request_id)
        actor = await self.r._operator(run_id)
        snapshot = self.service._actor(actor)
        async with self.r._locked(run_id, snapshot):
            value = self._sanitation_existing(await self.r._entries(run_id), original_request, request_id, snapshot)
            if value is None:
                raise KeyError("The native index sanitation was not found")
            self.r.store.reject_credentials(value)
            return value

    async def index_sanitation_candidate(self, run_id: str, original_request: str, previous_catalog_path: str) -> Data:
        identifier(original_request)
        actor = await self.r._operator(run_id)
        snapshot = self.service._actor(actor)
        async with self.r._locked(run_id, snapshot) as row:
            review, _ = await self._review(
                row.data, original_request, snapshot, previous_catalog_path, index_sanitation=True
            )
        if await self.r._operator(run_id) != actor:
            raise AccessDenied("The native index sanitation administrator changed")
        self.r.store.reject_credentials(review)
        return review

    async def sanitize_index(self, run_id: str, original_request: str, body: Any) -> Data:
        from firefly_forge.configuration import redact_secrets
        from firefly_forge.engine import redact
        from firefly_forge.source_edit_continuations import clock_ms
        from firefly_forge.source_index_sanitation import (
            SANITATION_INTENT,
            SANITATION_RECEIPT,
            IndexSanitationInput,
            apply_sanitation,
            assert_retained_artifacts,
        )
        from firefly_forge.workspace_reconciliations import _source_patch

        identifier(original_request)
        body = IndexSanitationInput.model_validate(body.model_dump())
        if body.request_id == original_request:
            raise ValueError("Index sanitation requires a distinct native request identity")
        actor = await self.r._operator(run_id)
        snapshot = self.service._actor(actor)
        request = body.model_dump()
        await self.r.store.admit_credentials(request)
        async with self.r._locked(run_id, snapshot):
            prior = self._sanitation_existing(
                await self.r._entries(run_id), original_request, body.request_id, snapshot
            )
            if prior is not None:
                if prior["request"] != request:
                    raise ValueError("The native sanitation identity already binds another request")
                if prior.get("status") == "needs_reconciliation":
                    raise ValueError("The native index sanitation outcome is unknown; never replay its effect")
                return prior
        review = await self.index_sanitation_candidate(run_id, original_request, body.previous_catalog_path)
        if (
            review["original_intent_digest"] != body.original_intent_digest
            or review["digest"] != body.candidate_digest
            or review["admission_contract_digest"] != body.admission_contract_digest
        ):
            raise ValueError("Native sanitation requires the exact reviewed generated index")
        run = await self.r.store.get_run(run_id)
        workspace = self.r.engine.workspaces / run_id
        names = [item["path"] for item in review["index_plan"]["artifacts"]]
        patch = _source_patch(workspace, run["base_revision"], names)
        settings = self.r.store.configuration.load_snapshot(run["config_revision"])
        if redact_secrets(redact(patch), settings) != patch:
            raise ValueError("Observed public source contains a configured credential")
        await Store.admit_source_patch(self.r.store, SourcePatch(patch))
        if await self.r._operator(run_id) != actor:
            raise AccessDenied("The native sanitation administrator changed during patch admission")
        async with self.r._locked(run_id, snapshot) as row:
            current, retained = await self._review(
                row.data, original_request, snapshot, body.previous_catalog_path, index_sanitation=True
            )
            if (
                current != review
                or CURRENT_PRINCIPAL.get() != actor
                or _source_patch(workspace, run["base_revision"], names) != patch
            ):
                raise ValueError("The exact native sanitation cut changed before its durable intent")
            entries = await self.r._entries(run_id)
            if any(e["idempotency_key"] == body.request_id for e in entries):
                raise ValueError("The native sanitation request identity is already retained")
            if body.valid_for_seconds > retained["source_scope"]["provider_timeout"]:
                raise ValueError("Native sanitation cannot exceed the original finite allowance")
            payload = {
                "version": 1,
                "request": request,
                "actor": snapshot,
                "original_request_id": original_request,
                "original_intent_digest": body.original_intent_digest,
                "candidate": review,
                "public_patch_sha256": hashlib.sha256(patch.encode()).hexdigest(),
                "expires_at_ms": clock_ms() + body.valid_for_seconds * 1000,
                "no_queue_or_provider_dispatch": True,
            }
            self.r.store.reject_credentials(payload)
            await self.r.store.assert_ownership()
            intent = await self.r.journal._append(run_id, "implement", body.request_id, SANITATION_INTENT, payload)
        # This intent is committed before the index-only effect; any uncertainty below remains held.
        async with self.r._locked(run_id, snapshot) as row:
            if clock_ms() > payload["expires_at_ms"]:
                raise ValueError("The native index sanitation consent expired before its effect")
            if await self.r._operator(run_id) != actor:
                raise AccessDenied("The native sanitation administrator changed before its effect")
            current, _ = await self._review(
                row.data,
                original_request,
                snapshot,
                body.previous_catalog_path,
                index_sanitation=True,
                sanitation_reservation=body.request_id,
            )
            if (
                current != review
                or CURRENT_PRINCIPAL.get() != actor
                or _source_patch(workspace, run["base_revision"], names) != patch
            ):
                raise ValueError("The durable native sanitation intent lost its exact reviewed cut")
            await self.r.store.assert_ownership()
            effect = apply_sanitation(workspace, review["index_plan"])
            after_review, _ = await self._review(
                row.data, original_request, snapshot, body.previous_catalog_path, sanitation_reservation=body.request_id
            )
        after_patch = _source_patch(workspace, run["base_revision"])
        if after_patch != patch or redact_secrets(redact(after_patch), settings) != after_patch:
            raise ValueError("Native sanitation changed its exact pre-admitted public patch")
        await Store.admit_source_patch(self.r.store, SourcePatch(after_patch))
        if await self.r._operator(run_id) != actor:
            raise AccessDenied("The native sanitation administrator changed after its effect")
        async with self.r._locked(run_id, snapshot) as row:
            current, _ = await self._review(
                row.data, original_request, snapshot, body.previous_catalog_path, sanitation_reservation=body.request_id
            )
            if (
                current != after_review
                or CURRENT_PRINCIPAL.get() != actor
                or clock_ms() > payload["expires_at_ms"]
                or _source_patch(workspace, run["base_revision"]) != patch
            ):
                raise ValueError("The native index sanitation completion lost its current cut or finite consent")
            receipt = {
                **payload,
                "intent_digest": intent["digest"],
                "effect": effect,
                "completed_cut": current["cut"],
                "observed_candidate_digest": current["digest"],
            }
            self.r.store.reject_credentials(receipt)
            await self.r.store.assert_ownership()
            await self.r.journal._append(run_id, "implement", body.request_id, SANITATION_RECEIPT, receipt)
            if await self.r._operator(run_id) != actor or CURRENT_PRINCIPAL.get() != actor:
                raise AccessDenied("The native sanitation administrator changed before completion commit")
            final, _ = await self._review(
                row.data, original_request, snapshot, body.previous_catalog_path, sanitation_reservation=body.request_id
            )
            if (
                final != current
                or clock_ms() > payload["expires_at_ms"]
                or _source_patch(workspace, run["base_revision"]) != patch
            ):
                raise ValueError("The exact native sanitation completion changed before commit")
            assert_retained_artifacts(workspace, review["index_plan"])
            return copy.deepcopy(receipt)
