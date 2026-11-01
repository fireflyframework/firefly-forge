# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Verified same-run consent settles one existing consumed claim without another worker turn."""

from __future__ import annotations

import copy
import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from pyfly.data.relational.sqlalchemy import LockMode
from pyfly.data.transaction import current_unit_of_work

from firefly_forge.identity import Principal
from firefly_forge.journal import digest
from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied, ProjectAccess
from firefly_forge.scm_consumed_continuation_models import SCMConsumedContinuationAuthorityV1
from firefly_forge.scm_natural_effects import NaturalEffects, intake_digest, validate_projection
from firefly_forge.scm_resume_models import SCMResumeCommandV2
from firefly_forge.store import identifier

Data = dict[str, Any]
KEY = "consumed_continuation_authority"


def reference(run: Data, *, known_settlement: bool = False) -> Data | None:
    marker = run.get("consumed_terminal_recovery") or {}
    if (
        run.get("status") not in ({"paused", "developing", "needs_reconciliation"} if known_settlement else {"paused"})
        or marker.get("version") != 2
        or marker.get("state") not in ({"claimed", "accepted"} if known_settlement else {"claimed"})
        or re.fullmatch(r"[a-f0-9]{32}", str(marker.get("request_id", ""))) is None
        or any(
            re.fullmatch(r"[a-f0-9]{64}", str(marker.get(key, ""))) is None for key in ("record_digest", "claim_digest")
        )
    ):
        return None
    value = {key: marker[key] for key in ("request_id", "record_digest", "claim_digest")}
    return {
        "origin": "consumed_terminal_claim",
        "question_id": "consumed-" + marker["request_id"],
        "question_digest": digest({"run_id": run["id"], "consumed_continuation": value}),
        "question": "Continue this same recorded result's local postprocessing; independent checks remain separate",
        "question_status": "pending",
        "consumed": False,
        "consumed_continuation": value,
    }


def row_stamp(row: Any) -> Any:
    value = {"data": copy.deepcopy(row.data)} if hasattr(row, "data") else {}
    value.update(
        {
            key: copy.deepcopy(getattr(row, key))
            for key in (
                "id",
                "source_key",
                "status",
                "repository_id",
                "project_id",
                "user_id",
                "role",
                "active",
                "revision",
                "issuer",
                "subject",
                "enabled",
                "api_roles",
                "api_roles_audience",
                "api_roles_issued_at",
                "api_roles_expires_at",
            )
            if hasattr(row, key)
        }
    )
    return value


def intent_key(run_id: str, request_id: str) -> str:
    return "consumed-terminal:" + run_id + ":" + request_id


@dataclass
class PreparedConsumedContinuation:
    manager: SCMConsumedContinuations
    resume: Any
    actor: Principal
    seal: SCMConsumedContinuationAuthorityV1
    _cut: Any = field(default=None, init=False)

    def __post_init__(self) -> None:
        self.seal = self.seal.model_copy(deep=True)
        store = self.manager.store
        self._captured = (
            store.execution_ownership_token,
            store.configuration.write_generation,
            store.credential_registry,
        )
        self._digest = self.seal.digest
        self._principal = self.actor

    def _view(self) -> None:
        store = self.manager.store
        if (
            self.actor is not self._principal
            or self.seal.digest != self._digest
            or digest(self.seal.model_dump(mode="json", exclude={"digest"})) != self._digest
            or self.actor.authentication_method != "scm_mapping"
            or self.actor.api_roles
            or self.actor.api_audience is not None
            or self.actor.id.hex != self.seal.scope.native_user_id
            or self.actor.scm_delegation != self.seal.scope.model_dump(mode="json")
            or CURRENT_PRINCIPAL.get() != self.actor
            or self.resume._consumed.get() is not self
            or any(
                actual is not expected
                for actual, expected in zip(
                    (store.execution_ownership_token, store.configuration.write_generation, store.credential_registry),
                    self._captured,
                    strict=True,
                )
            )
        ):
            raise AccessDenied("Consumed continuation lost its exact delegated actor, seal or native owner")

    @contextmanager
    def activate(self) -> Iterator[PreparedConsumedContinuation]:
        token = CURRENT_PRINCIPAL.set(self.actor)
        active = self.resume._consumed.set(self)
        try:
            yield self
        finally:
            self.resume._consumed.reset(active)
            CURRENT_PRINCIPAL.reset(token)

    def validate_witness(self, witness: Data, claim: Data | None = None) -> None:
        self._view()
        payload = witness["payload"]
        scope = self.seal.scope
        if (
            witness["run_id"] != scope.run_id
            or witness["digest"] != self.seal.recovery_digest
            or payload["request_id"] != self.seal.request_id
            or payload["candidate"]["actor_digest"] != self.seal.original_actor_digest
            or payload["actor"]["user_id"] != scope.native_user_id
            or any(
                payload["run"].get(key) != getattr(scope, key)
                for key in ("project_id", "repository_id", "component_id")
            )
            or claim is not None
            and claim["digest"] != self.seal.claim_digest
        ):
            raise AccessDenied("Delegated local postprocessing belongs to another original claim or native operator")

    async def current(self) -> None:
        from firefly_forge.project_persistence import ForgeProjectRepository
        from firefly_forge.scm_natural_tasks import natural_policy_digest

        self._view()
        unit = current_unit_of_work()
        child = await self.resume._delivery(self.seal.scope.verified_receipt_id)
        self._view()
        context = await self.resume._context(child)
        self._view()
        rows = []
        async with (
            self.manager.natural.conversations._locked(
                [self.seal.scope.model_dump(mode="json")], self.seal.scope.repository_id
            ),
            self.resume._locked(child, context) as row,
        ):
            self._view()
            context = await self.resume._context(row.data)
            self._view()
            if (
                self.resume._actor_context(row.data, context) != self.seal.scope
                or row.source_key != intent_key(self.seal.scope.run_id, self.seal.request_id)
                or row.data.get(KEY) != self.seal.model_dump(mode="json")
                or row.data.get("action_status") not in {"writing", "completed"}
                or row.data.get("status") not in {"needs_reconciliation", "processed"}
                or natural_policy_digest() != self.seal.policy_digest
            ):
                raise AccessDenied("The current consumed continuation grant or immutable source changed")
            parent = await self.manager.collaboration.inbox.find_by_id(
                identifier(row.data["natural_parent"]["receipt_id"]), lock=LockMode.PESSIMISTIC_WRITE
            )
            if parent is None or intake_digest(parent.data) != self.seal.intake_digest:
                raise AccessDenied("The exact natural continuation interpretation changed")
            validate_projection(row.data, parent.data)
            rows.extend([row, parent])
            settings = await self.resume.settings.find_for_repository(identifier(self.seal.scope.repository_id))
            event = await self.resume.events.find_by_id(identifier(context["grant"]["event_id"]))
            if settings is None or event is None:
                raise AccessDenied("The retained resume consent is unavailable")
            rows.extend([settings, event])
            project = await ForgeProjectRepository().find_by_id(
                identifier(self.seal.scope.project_id), lock=LockMode.PESSIMISTIC_WRITE
            )
            repository = await self.manager.store.repository_records.find_by_id(
                identifier(self.seal.scope.repository_id), lock=LockMode.PESSIMISTIC_WRITE
            )
            if project is None or repository is None:
                raise AccessDenied("The exact project or repository is no longer current")
            rows.extend([project, repository])
            for user_id in sorted({self.actor.id.hex, context["run"]["execution_actor"]["user_id"]}):
                user = await self.manager.collaboration.polling.users.find_by_id(
                    identifier(user_id), lock=LockMode.PESSIMISTIC_WRITE
                )
                if user is None or not user.enabled:
                    raise AccessDenied("Both original and delegated execution identities must remain current")
                if user_id == self.actor.id.hex and (user.issuer, user.subject) != (
                    self.actor.issuer,
                    self.actor.subject,
                ):
                    raise AccessDenied("The delegated identity no longer matches its captured native person")
                rows.append(user)
            memberships = await self.manager.collaboration.polling.memberships.for_user(self.actor.id)
            if not any(
                m.project_id.hex == self.seal.scope.project_id and m.active and ProjectAccess.permits(m.role, "execute")
                for m in memberships
            ):
                raise AccessDenied("The delegated person lacks current project execution permission")
            rows.extend(memberships)
            self._view()
            self._cut = (unit, [(item, row_stamp(item)) for item in rows])
        if current_unit_of_work() is not unit:
            raise AccessDenied("Consumed continuation changed its native transaction")
        self.fence()

    def fence(self) -> None:
        from firefly_forge.scm_natural_tasks import natural_policy_digest

        self._view()
        if self._cut is None:
            raise AccessDenied("Consumed continuation authority was not checked")
        unit, rows = self._cut
        if (
            current_unit_of_work() is not None
            and current_unit_of_work() is not unit
            or natural_policy_digest() != self.seal.policy_digest
            or any(row_stamp(row) != stamp for row, stamp in rows)
        ):
            raise AccessDenied("The locked consumed continuation authority changed before commit")

    async def authorize_snapshot(self, actor: Data, project: str | None, permission: str) -> None:
        self._view()
        if (
            actor != ProjectAccess.actor_snapshot(self.actor)
            or project != self.seal.scope.project_id
            or permission not in {"read", "execute", "release"}
        ):
            raise AccessDenied("Consumed continuation cannot authorize another actor, project or operation")
        await self.current()
        memberships = await self.manager.collaboration.polling.memberships.for_user(self.actor.id)
        if not any(
            m.project_id.hex == project and m.active and ProjectAccess.permits(m.role, permission) for m in memberships
        ):
            raise AccessDenied("The delegated continuation permission is no longer current")
        self.fence()


class SCMConsumedContinuations(NaturalEffects):
    def __init__(self, collaboration: Any, completions: Any) -> None:
        from firefly_forge.scm_natural_tasks import SCMNaturalTasks

        super().__init__(SCMNaturalTasks(collaboration))
        self.completions = completions

    async def review(self, receipt_id: str, *, remote: bool = True) -> Data:
        from firefly_forge.scm_natural_tasks import natural_policy_digest

        parent = await self.collaboration.receipt(receipt_id)
        self.natural.require_effect_transport(parent)
        turn = parent.get("natural_turn") or {}
        resolution = turn.get("resolution") or {}
        if (
            parent["status"] != "processed"
            or turn.get("classifier_outcome") != "confirmed_valid"
            or resolution.get("state") != "consumed_continue"
            or turn.get("policy_digest") != natural_policy_digest()
        ):
            raise AccessDenied("One retained verified continuation interpretation is required")
        scopes = await self.natural.conversations.scopes(parent, remote=remote)
        context = await self.natural._context(parent, scopes)
        question = next(
            (row for row in context["candidates"] if row.get("question_id") == resolution.get("question_id")), None
        )
        if question is None or any(
            question.get(key) != resolution.get(key) for key in ("run_id", "question_id", "question_digest")
        ):
            raise AccessDenied("The original consumed claim or ticket reference changed")
        resume = self.collaboration.poll_resume_authority
        if resume is None:
            raise AccessDenied("Current separately consented polling resume authority is required")
        grant = await resume._grant(scopes[0]["project_id"], parent["repository_id"], key=scopes[0]["binding_key"])
        if grant is None or not grant["request"]["active"]:
            raise AccessDenied("Current separately consented same-run continuation is required")
        binding = await resume._binding(
            scopes[0]["project_id"], parent["repository_id"], resume.request_type.model_validate(grant["request"])
        )
        run = await resume._run_data(question["run_id"])
        await resume._original(run)
        if remote:
            await resume._preflight({"binding": binding})
        cut = dict(
            version=1,
            receipt_id=receipt_id,
            intake_digest=intake_digest(parent),
            scopes=scopes,
            question=question,
            binding_digest=binding["digest"],
            resume_grant_digest=grant["digest"],
            effects=copy.deepcopy(parent.get("natural_effects", {})),
        )
        return {**cut, "digest": digest(cut)}

    async def apply(
        self, receipt_id: str, *, admission: str | None = None, explicit: bool = False, retained_only: bool = False
    ) -> Data:
        from firefly_forge.consumed_terminal_completions import find_witness
        from firefly_forge.consumed_terminal_reconciliations import candidate, readback, reconcile
        from firefly_forge.known_worker_completion_models import ConsumedTerminalReconciliationInputV1
        from firefly_forge.known_worker_completions import _entries
        from firefly_forge.scm_natural_tasks import natural_policy_digest

        parent = await self.collaboration.receipt(receipt_id)
        expected = copy.deepcopy(parent)
        try:
            if admission is not None or explicit:
                raise AccessDenied("A consumed continuation cannot replace its original claimed request")
            self.natural.require_effect_transport(parent)
            retained = []
            for child_id, marker in (parent.get("natural_effects") or {}).items():
                if marker.get("stage") == "consumed":
                    saved = await self.collaboration.receipt(child_id)
                    validate_projection(saved, parent)
                    if saved.get("action_status") in {"writing", "completed"}:
                        retained.append(saved)
            if len(retained) > 1:
                raise AccessDenied("Only one original consumed continuation intent can be read back")
            existing = bool(retained)
            if existing:
                child = retained[0]
                seal = SCMConsumedContinuationAuthorityV1.model_validate(child.get(KEY))
                marker = dict(
                    request_id=seal.request_id, record_digest=seal.recovery_digest, claim_digest=seal.claim_digest
                )
            else:
                if retained_only:
                    return dict(parent)
                cut = await self.review(receipt_id)
                marker = cut["question"]["consumed_continuation"]
                command = SCMResumeCommandV2(
                    version=2,
                    action="resume",
                    run_id=cut["question"]["run_id"],
                    expected_grant_digest=cut["resume_grant_digest"],
                    expected_question_id=cut["question"]["question_id"],
                    expected_response_digest=marker["claim_digest"],
                    expected_cut_digest=cut["question"]["question_digest"],
                ).model_dump(mode="json")
                admission = digest({"receipt_id": receipt_id, "intake_digest": cut["intake_digest"]})[:32]
                child = await self._child(parent, cut, command, "consumed", admission)
                expected = await self.collaboration.receipt(receipt_id)
            resume = self.collaboration.poll_resume_authority
            if resume is None:
                raise AccessDenied("The current polling continuation authority is unavailable")
            context = await resume._context(child)
            async with (
                self.natural.conversations._locked(
                    [{"project_id": context["run"]["project_id"], "native_user_id": context["user_id"]}],
                    child["repository_id"],
                ),
                resume._locked(child, context) as row,
            ):
                context = await resume._context(row.data)
                entries = _entries(
                    await self.completions.journal.records.find_for_run(identifier(context["run"]["id"])),
                    context["run"]["id"],
                )
                witness = find_witness(entries, marker["request_id"])
                if witness is None:
                    raise AccessDenied("The exact original consumed claim is unavailable")
                if witness["payload"]["actor"]["user_id"] != context["user_id"]:
                    raise AccessDenied("Only the original native operator can delegate this consumed claim")
                if existing:
                    if row.data.get(KEY) != seal.model_dump(mode="json") or row.data.get("action_status") not in {
                        "writing",
                        "completed",
                    }:
                        raise AccessDenied("The original continuation authority changed")
                elif row.status != "received" or row.data.get("action_status") not in {None, "not_dispatched"}:
                    raise AccessDenied("The original continuation outcome is unknown")
                else:
                    from firefly_forge.consumed_terminal_reconciliations import _stopped
                    from firefly_forge.known_worker_completions import _task

                    claim = _stopped(
                        context["run"],
                        _task(await self.completions.journal._task(context["run"]["id"], "implement")),
                        entries,
                        witness,
                    )
                    if claim["digest"] != marker["claim_digest"] or witness["digest"] != marker["record_digest"]:
                        raise AccessDenied("The original consumed claim reference changed")
                    source_key = intent_key(context["run"]["id"], marker["request_id"])
                    claimed = await self.collaboration.inbox.find_source(
                        identifier(child["repository_id"]), child["provider"], source_key
                    )
                    if claimed is not None and claimed.id != row.id:
                        raise AccessDenied("An original consumed continuation intent already owns this claim")
                    value = dict(
                        version=1,
                        scope=resume._actor_context(row.data, context).model_dump(mode="json"),
                        request_id=marker["request_id"],
                        recovery_digest=marker["record_digest"],
                        claim_digest=marker["claim_digest"],
                        original_actor_digest=witness["payload"]["candidate"]["actor_digest"],
                        intake_digest=cut["intake_digest"],
                        policy_digest=natural_policy_digest(),
                        local_postprocessing_only=True,
                    )
                    seal = SCMConsumedContinuationAuthorityV1.model_validate({**value, "digest": digest(value)})
                    row.source_key = source_key
                    await resume._write(
                        row,
                        status="needs_reconciliation",
                        action_status="writing",
                        **{KEY: seal.model_dump(mode="json")},
                    )
                user = await self.collaboration.polling.users.find_by_id(identifier(context["user_id"]))
                if user is None or not user.enabled:
                    raise AccessDenied("The delegated native person is unavailable")
                actor = Principal(
                    user.id,
                    user.issuer,
                    user.subject,
                    authentication_method="scm_mapping",
                    scm_delegation=seal.scope.model_dump(mode="json"),
                )
            prepared = PreparedConsumedContinuation(self, resume, actor, seal)
            with prepared.activate():
                await prepared.current()
                if existing:
                    result = await readback(self.completions, seal.scope.run_id, seal.request_id, _delegation=prepared)
                else:
                    reviewed = await candidate(
                        self.completions, seal.scope.run_id, seal.request_id, _delegation=prepared
                    )
                    body = ConsumedTerminalReconciliationInputV1(
                        version=1,
                        candidate_digest=reviewed["digest"],
                        acknowledge_same_claimed_action=True,
                        acknowledge_no_execution_grant=True,
                        acknowledge_local_postprocessing=True,
                    )
                    result = await reconcile(
                        self.completions, seal.scope.run_id, seal.request_id, body, _delegation=prepared
                    )
                if result["settlement"]["state"] != "accepted":
                    raise AccessDenied("The original local postprocessing is not known accepted")
                async with resume._locked(await resume._delivery(child["id"]), context) as row:
                    await prepared.current()
                    await resume._write(row, status="processed", action_status="completed", action_result=result)
                    await prepared.current()
                    from pyfly.data.transaction.synchronization import register_synchronization

                    from firefly_forge.consumed_terminal_reconciliations import ReconciliationCommitFence

                    unit = current_unit_of_work()
                    if unit is None or unit.owner_task is None:
                        raise AccessDenied("The known settlement lost its native owner transaction")
                    register_synchronization(ReconciliationCommitFence(unit, unit.owner_task, prepared.fence))
            return await self._retain_state(receipt_id, "consumed_accepted", expected=expected)
        except (AccessDenied, ValueError, KeyError, OSError):
            return await self._retain_state(receipt_id, "consumed_held", expected=expected)
