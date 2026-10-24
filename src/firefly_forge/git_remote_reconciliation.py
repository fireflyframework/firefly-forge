# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""New finite consent to confirm an unknown checkpoint's desired remote state."""

from __future__ import annotations

import asyncio
import copy
import hashlib
from typing import Any, cast

from firefly_forge.engine import Engine
from firefly_forge.git_deliveries import binding, delivery_digest
from firefly_forge.git_delivery_models import GitDeliveryInputV1

Data = dict[str, Any]


def reconciliation_candidate(entries: list[Data], request_id: str, observation: Data) -> Data | None:
    receipts = [e for e in entries if e["kind"] == "git_delivery_receipt"]
    local = [e for e in receipts if e["payload"].get("request_id") == request_id]
    intents = [
        e for e in entries if e["kind"] == "git_delivery_remote_intent" and e["payload"].get("request_id") == request_id
    ]
    remote = observation.get("remote") or {}
    if len(local) != 1 or not intents or receipts[-1] != local[0]:
        return None
    intent, receipt = intents[-1], local[0]["payload"]
    if (
        intent["payload"].get("purpose") is not None
        or any(
            e["kind"] == "git_delivery_remote_receipt" and e["idempotency_key"] == intent["idempotency_key"]
            for e in entries
        )
        or receipt["revision"] != observation["observed_head"]
        or receipt["source_tree"] != observation["observed_tree"]
        or remote.get("intent_digest") != intent["digest"]
        or remote.get("observed_revision") not in {None, receipt["revision"]}
        or remote.get("reference") is not None
        and not remote.get("matches_admitted_body")
    ):
        return None
    return {
        "version": 1,
        "original_request_id": request_id,
        "checkpoint_digest": receipt["checkpoint_digest"],
        "remote_intent_digest": intent["digest"],
        "observation_digest": delivery_digest(observation),
    }


class GitRemoteReconciliations:
    def __init__(self, deliveries: Any) -> None:
        self.d, self.store = deliveries, deliveries.store

    async def prepare(self, run_id: str, body: GitDeliveryInputV1, actor: Data) -> Data:
        command = body.explicit_reconciliation
        if command is None:
            raise ValueError("Explicit finite remote-completion consent is required")
        run = await self.store.get_run(run_id)
        observed = await self.d.observations(run_id, command.original_request_id, actor=actor)
        candidate = observed.get("reconciliation_candidate")
        if candidate != {
            key: getattr(command, key)
            for key in (
                "version",
                "original_request_id",
                "checkpoint_digest",
                "remote_intent_digest",
                "observation_digest",
            )
        } or (body.expected_head, body.expected_source_tree) != (observed["observed_head"], observed["observed_tree"]):
            raise ValueError("Review the exact current checkpoint and remote observation before reconciliation")
        entries = await self.d._entries(run_id)
        receipt = next(r for r in self.d._records(entries) if r["request_id"] == command.original_request_id)
        intent = next(e for e in entries if e["digest"] == command.remote_intent_digest)
        payload = intent["payload"]
        if (
            intent["kind"] != "git_delivery_remote_intent"
            or payload["checkpoint_digest"] != receipt["checkpoint_digest"]
            or payload["revision"] != receipt["revision"]
            or payload["branch"] != run["branch"]
            or payload["repository_id"] != run["repository_id"]
            or not payload.get("body")
            or hashlib.sha256(payload["body"].encode()).hexdigest() != payload["body_sha256"]
            or not payload.get("title")
        ):
            raise ValueError("The retained remote intent no longer matches its checkpoint and exact body")
        await self.d._assert_checkpoint_source(run, receipt)
        repository = await self.store.repository(run["repository_id"])
        if payload["repository_url"] != repository["url"]:
            raise ValueError("The retained remote intent belongs to another destination")
        if run.get("git_failed_unit_adoption"):
            from firefly_forge.git_failed_unit_adoption import GitFailedUnitAdoptions

            await GitFailedUnitAdoptions(self.d)._authority(run, actor)
        await self.store.admit_credentials(
            {"title": payload["title"], "body": payload["body"], "communication": payload["communication"]}
        )
        from firefly_forge.verification_models import verification_source

        return {
            "run": run,
            "receipt": receipt,
            "intent": intent,
            "journal_head": entries[-1]["digest"],
            "source_digest": verification_source(self.d.workspace(run)),
            "publication_binding": Engine._publication_binding(run, repository),
            "observed_reference": (observed.get("remote") or {}).get("reference"),
        }

    async def guard(self, prepared: Data, actor: Data, expires_at_ms: int) -> None:
        from firefly_forge.git_failed_unit_adoption import GitFailedUnitAdoptions, clock_ms, effective_policy
        from firefly_forge.verification_models import verification_source

        run = prepared["run"]
        await self.store.assert_ownership()
        current = await self.store.get_run(run["id"])
        if current.get("git_failed_unit_adoption"):
            adoption = GitFailedUnitAdoptions(self.d)
            saved = await adoption._authority(current, actor)
            await adoption._settled(current)
            policy = await effective_policy(self.d, current)
            if saved["policy"] != policy.model_dump(mode="json") or policy.remote != "draft_pr":
                raise ValueError("Current remote-completion policy differs from the retained adoption")
        else:
            await self.d._guard(run["id"], run, actor)
            policy = await effective_policy(self.d, current)
            if policy.remote != "draft_pr":
                raise ValueError("Explicit remote completion requires the original draft delivery policy")
        from pyfly.data.transaction import current_unit_of_work

        if self.d.authorization is None and current_unit_of_work() is not None:
            from pyfly.data.relational.sqlalchemy import LockMode

            from firefly_forge.store import identifier

            row = await self.store.repository_records.find_by_id(
                identifier(run["repository_id"]), lock=LockMode.PESSIMISTIC_WRITE
            )
            if row is None:
                raise ValueError("The remote-completion repository is unavailable")
            repository = copy.deepcopy(row.data)
        else:
            repository = await self.store.repository(run["repository_id"])
        if not self.d.engine.connections:
            raise ValueError("Current explicit SCM credentials are required for remote completion")
        self.d.engine.connections.assert_snapshot(repository, current.get("scm_snapshot"))
        await self.store.assert_ownership()
        final = await self.store.get_run(run["id"])
        self.d.engine.communication_writer.assert_current(prepared["intent"]["payload"]["communication"])
        retained = prepared["intent"]["payload"]
        self.store.reject_credentials(
            {"title": retained["title"], "body": retained["body"], "communication": retained["communication"]}
        )
        if (
            binding(final) != binding(run)
            or Engine._publication_binding(final, repository) != prepared["publication_binding"]
            or verification_source(self.d.workspace(run)) != prepared["source_digest"]
            or clock_ms() >= expires_at_ms
        ):
            raise ValueError("Remote-completion source, authority or finite consent changed")

    async def complete(self, run_id: str, body: GitDeliveryInputV1, actor: Data, *, expires_at_ms: int) -> Data:
        from firefly_forge.git_delivery_remote import DraftPullRequests
        from firefly_forge.scm import push

        command = body.explicit_reconciliation
        if command is None:
            raise ValueError("Explicit finite remote-completion consent is required")
        async with self.d.locks.setdefault(run_id, asyncio.Lock()):
            prepared = await self.prepare(run_id, body, actor)
            run, receipt, old = prepared["run"], prepared["receipt"], prepared["intent"]
            await self.guard(prepared, actor, expires_at_ms)
            repository = await self.store.repository(run["repository_id"])
            authentication = await self.d.engine.connections.authentication(repository)
            environment = await self.d.engine.connections.git_environment(repository)
            await self.guard(prepared, actor, expires_at_ms)
            expected = await self.d._remote_revision(repository, run["branch"], environment)
            if expected not in {None, receipt["revision"]}:
                raise ValueError("Remote completion refuses a foreign or reset branch")
            payload = copy.deepcopy(old["payload"])
            payload["reconciliation"] = {
                "request_id": body.request_id,
                "original_intent_digest": old["digest"],
                "checkpoint_digest": receipt["checkpoint_digest"],
                "observation_digest": command.observation_digest,
                "actor": actor,
                "consent": command.model_dump(mode="json"),
                "expires_at_ms": expires_at_ms,
                "original_outcome": "unknown",
            }
            payload["actor"] = actor
            key = "git-remote-reconciliation:" + body.request_id
            async with self.d._owned(run_id, actor):
                await self.guard(prepared, actor, expires_at_ms)
                entries = await self.entries(run_id)
                if entries[-1]["digest"] != prepared["journal_head"]:
                    raise ValueError("Checkpoint history changed after explicit observation")
                intent = await self.d._append(run_id, "git_delivery_remote_intent", key, payload)
                await self.guard(prepared, actor, expires_at_ms)

            async def guard() -> None:
                await self.d.engine.connections.authentication(repository)
                await self.d.engine.connections.git_environment(repository)
                current_revision = await self.d._remote_revision(repository, run["branch"], environment)
                if current_revision not in {None, receipt["revision"]} or (
                    remote_established and current_revision is None
                ):
                    raise ValueError("The reviewed remote checkpoint branch changed during reconciliation")
                await self.d._assert_checkpoint_source(run, receipt)
                async with self.d._owned(run_id, actor):
                    await self.guard(prepared, actor, expires_at_ms)

            remote_established = expected == receipt["revision"]
            await guard()
            async with self.d.http_client(follow_redirects=False) as client:
                provider = DraftPullRequests(client, repository, authentication, guard=guard)
                reference = await provider.find(
                    run["branch"],
                    receipt["revision"],
                    expected_title=payload["title"],
                    expected_body_sha256=payload["body_sha256"],
                )
                if reference is not None and expected is None:
                    raise ValueError("A remote draft exists without its exact checkpoint branch")
                retained_reference = prepared["observed_reference"] or payload.get("prior_reference")
                if retained_reference is not None:
                    if reference is None:
                        raise ValueError("The reviewed draft identity disappeared; do not create a replacement")
                    provider._same_reference(retained_reference, reference)
                if expected is None:
                    await guard()
                    await push(
                        self.d.workspace(run),
                        run["branch"],
                        env=environment,
                        repository_url=repository["url"],
                        revision=receipt["revision"],
                        expected_remote_revision=None,
                    )
                if await self.d._remote_revision(repository, run["branch"], environment) != receipt["revision"]:
                    raise ValueError("The reconciled branch revision is unconfirmed")
                remote_established = True
                if reference is None:
                    # A fresh discovery precedes creation; an unknown write is never retried automatically.
                    reference = await provider.find(
                        run["branch"],
                        receipt["revision"],
                        expected_title=payload["title"],
                        expected_body_sha256=payload["body_sha256"],
                    )
                    if reference is None:
                        created = await provider.create(
                            run["branch"], receipt["revision"], payload["title"], payload["body"]
                        )
                    else:
                        created = reference
                    reference = await provider.find(
                        run["branch"],
                        receipt["revision"],
                        expected_title=payload["title"],
                        expected_body_sha256=payload["body_sha256"],
                    )
                    if reference is not None:
                        provider._same_reference(created, reference)
                if reference is None:
                    raise ValueError("The exact desired draft state is unconfirmed")
            await guard()
            async with self.d._owned(run_id, actor):
                await self.guard(prepared, actor, expires_at_ms)
                result = {
                    "request_id": receipt["request_id"],
                    "revision": receipt["revision"],
                    "reference": reference,
                    "remote_status": "draft",
                    "intent_digest": intent["digest"],
                    "reconciliation": payload["reconciliation"],
                }
                await self.d._append(run_id, "git_delivery_remote_receipt", key, result)
                await self.store.update(run_id, git_delivery_draft=reference)
                await self.guard(prepared, actor, expires_at_ms)
            await self.d._activity(run_id, {**receipt, "remote_status": "draft", "pr_url": reference["url"]})
            return {
                **receipt,
                "remote_status": "draft",
                "pr_url": reference["url"],
                "reconciliation": result["reconciliation"],
            }

    async def entries(self, run_id: str) -> list[Data]:
        from pyfly.data.transaction import current_unit_of_work

        if self.d.authorization is None and current_unit_of_work() is not None:
            from firefly_forge.git_failed_unit_adoption import native_entries

            return await native_entries(self.d, run_id)
        return cast(list[Data], await self.d._entries(run_id))
