# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""One explicitly published owner review delegates one fresh native verification."""

from __future__ import annotations

import copy
import hashlib
import json
import math
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel
from pyfly.data.relational.sqlalchemy import LockMode
from pyfly.data.transaction import current_unit_of_work

from firefly_forge.execution_requests import digest as request_digest
from firefly_forge.identity import Principal
from firefly_forge.journal import digest
from firefly_forge.linked_verification_models import (
    LinkedVerificationCandidateV1,
    LinkedVerificationInputV1,
    LinkedVerificationReceiptV1,
)
from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied, ProjectAccess
from firefly_forge.scm_conversations import SCMConversations, observation_cut
from firefly_forge.scm_linked_verification_models import (
    SCMLinkedVerificationAdmissionV1,
    SCMLinkedVerificationScopeV1,
)
from firefly_forge.scm_linked_verification_records import (
    NativeHostAdministratorV1,
    SCMVerificationBaseV1,
    SCMVerificationConfirmationV1,
    SCMVerificationGrantV1,
    SCMVerificationIntentV1,
    SCMVerificationRecordV1,
    SCMVerificationReviewV1,
)
from firefly_forge.scm_natural_effects import intake_digest
from firefly_forge.scm_natural_questions import confirmed_publication
from firefly_forge.store import identifier

Data = dict[str, Any]
KEY = "natural_verification"


def sealed[Contract: BaseModel](model: type[Contract], value: Data) -> Contract:
    return model.model_validate({**value, "digest": digest(value)})


def immutable(record: SCMVerificationRecordV1) -> Data:
    # Progress is append-only independently of the authority to run the consented commands.
    return record.model_dump(mode="json", exclude={"intent"})


@dataclass
class PreparedVerificationGrant:
    authority: SCMLinkedVerifications
    actor: Principal
    record: SCMVerificationRecordV1
    seal: SCMLinkedVerificationAdmissionV1
    cut: Any = None

    async def current(self) -> None:
        from firefly_forge.scm_natural_tasks import natural_policy_digest

        store = self.authority.store
        owner, generation, reader = (
            store.execution_ownership_token,
            store.configuration.write_generation,
            store.credential_registry,
        )
        policy = natural_policy_digest()
        record, rows = await self.authority._current(self.actor, self.record.base.run_id, grant=True)
        if (
            immutable(record) != immutable(self.record)
            or self.authority._seal(record) != self.seal
            or owner is not store.execution_ownership_token
            or generation is not store.configuration.write_generation
            or reader is not store.credential_registry
            or policy != natural_policy_digest()
        ):
            raise AccessDenied("The exact native verification grant or admission view changed")
        self.cut = (
            current_unit_of_work(),
            owner,
            generation,
            reader,
            policy,
            [(row, copy.deepcopy(row.data)) for row in rows],
        )

    def fence(self) -> None:
        if self.cut is None:
            raise AccessDenied("Native verification authority has not been checked")
        from firefly_forge.scm_natural_tasks import natural_policy_digest

        unit, owner, generation, reader, policy, rows = self.cut
        store = self.authority.store
        if (
            (current_unit_of_work() is not None and current_unit_of_work() is not unit)
            or owner is not store.execution_ownership_token
            or generation is not store.configuration.write_generation
            or reader is not store.credential_registry
            or policy != natural_policy_digest()
            or datetime.now(UTC).timestamp() >= self.record.base.host_administrator.expires_at
            or any(row.data != data for row, data in rows)
        ):
            raise AccessDenied("The locked native verification authority or owner changed")


class SCMLinkedVerifications:
    def __init__(self, collaboration: Any, verifications: Any) -> None:
        self.collaboration, self.verifications = collaboration, verifications
        self.store, self.polling = collaboration.store, collaboration.polling
        self.conversations = SCMConversations(collaboration)

    async def _load(self, proposal: str, *, lock: bool = False) -> tuple[Any, SCMVerificationRecordV1]:
        row = await self.collaboration.inbox.find_by_id(
            identifier(proposal), **({"lock": LockMode.PESSIMISTIC_WRITE} if lock else {})
        )
        if row is None or not row.data.get(KEY):
            raise AccessDenied("The separate native verification proposal is unavailable")
        return row, SCMVerificationRecordV1.model_validate(row.data[KEY])

    @staticmethod
    def _host_admin(user: Any) -> NativeHostAdministratorV1:
        issued = getattr(user, "api_roles_issued_at", None)
        expiry = getattr(user, "api_roles_expires_at", None)
        audience = getattr(user, "api_roles_audience", None)
        roles = getattr(user, "api_roles", None)
        clock = datetime.now(UTC)
        if (
            not user.enabled
            or not audience
            or not isinstance(issued, (float, int))
            or isinstance(issued, bool)
            or not math.isfinite(issued)
            or issued > clock.timestamp()
            or not isinstance(expiry, datetime)
            or expiry.tzinfo is None
            or expiry <= clock
            or not isinstance(roles, list)
            or any(not isinstance(role, str) for role in roles)
            or "forge-admin" not in roles
        ):
            raise AccessDenied("Fresh host verification requires the mapped owner's current native factory-admin grant")
        return NativeHostAdministratorV1(
            native_user_id=user.id.hex,
            issuer=user.issuer,
            subject=user.subject,
            role_audience=audience,
            roles_digest=digest({"roles": sorted(roles or [])}),
            issued_at=float(issued),
            expires_at=expiry.timestamp(),
        )

    async def _current(
        self, actor: Principal, run_id: str, *, grant: bool = False
    ) -> tuple[SCMVerificationRecordV1, list[Any]]:
        if actor.authentication_method != "scm_mapping" or actor.api_roles or actor.api_audience is not None:
            raise AccessDenied("Verification mapping cannot confer native administrator privileges")
        scope = SCMLinkedVerificationScopeV1.model_validate(actor.scm_delegation)
        if run_id != scope.run_id or actor.id.hex != scope.native_user_id:
            raise AccessDenied("Verification actor belongs to another exact proposal")
        async with self.store._unit():
            _, initial = await self._load(scope.proposal_id)
        if initial.base.actor_scope() != scope:
            raise AccessDenied("Verification actor differs from its stable native proposal")
        async with self.conversations._locked([initial.base.read_scope.model_dump(mode="json")], scope.repository_id):
            row, record = await self._load(scope.proposal_id, lock=True)
            base = record.base
            delivery = copy.deepcopy(row.data)
            from firefly_forge.scm_natural_tasks import natural_policy_digest

            if (
                base != initial.base
                or delivery["digest"] != base.source_digest
                or delivery.get("source_conflict")
                or (delivery.get("natural_turn") or {}).get("policy_digest") != natural_policy_digest()
            ):
                raise AccessDenied("The original verification source changed")
            if (delivery.get("verification") or {}).get("transport") != "github_rest_poll_v1":
                raise AccessDenied("Natural linked verification currently supports authenticated GitHub polling only")
            current_scope = await self.polling.conversation_scope(delivery, remote=False)
            if current_scope != base.read_scope.model_dump(mode="json"):
                raise AccessDenied("The exact project, repository, identity mapping or source changed")
            binding = await self.polling._head(scope.project_id, scope.repository_id, scope.binding_key)
            if "respond" not in binding["request"]["allowed_actions"]:
                raise AccessDenied("Current explicit owner-response delegation is required")
            user = await self.polling.users.find_by_id(actor.id, lock=LockMode.PESSIMISTIC_WRITE)
            if user is None or (user.issuer, user.subject) != (actor.issuer, actor.subject):
                raise AccessDenied("The mapped native identity changed")
            if self._host_admin(user) != base.host_administrator:
                raise AccessDenied("The separate current native host-administrator proof changed")
            members = await self.polling.memberships.for_user(actor.id)
            if not any(
                m.project_id.hex == scope.project_id and m.active and ProjectAccess.permits(m.role, "execute")
                for m in members
            ):
                raise AccessDenied("The mapped owner lacks current project execute permission")
            run = await self.store.get_run(run_id)
            if any(run.get(key) != getattr(scope, key) for key in ("project_id", "repository_id", "component_id")):
                raise AccessDenied("Verification parent moved outside the consented native scope")
            rows = [row]
            if grant:
                if record.review is None or record.grant is None:
                    raise AccessDenied(
                        "A separately published and explicitly confirmed verification review is required"
                    )
                if confirmed_publication(delivery) != record.grant.publication.model_dump(mode="json"):
                    raise AccessDenied("The exact verification review publication changed or is unknown")
                confirmation = record.grant.confirmation
                answer_row = await self.collaboration.inbox.find_by_id(
                    identifier(confirmation.receipt_id), lock=LockMode.PESSIMISTIC_WRITE
                )
                if answer_row is None:
                    raise AccessDenied("The exact owner confirmation is unavailable")
                answer = copy.deepcopy(answer_row.data)
                answer_scope = await self.polling.conversation_scope(answer, remote=False)
                if (
                    answer["digest"] != confirmation.source_digest
                    or answer.get("source_conflict")
                    or answer.get("actor") != base.external_actor_id
                    or answer.get("issue") != {"kind": base.issue_kind, "number": base.issue_number}
                    or any(
                        answer_scope[k] != current_scope[k]
                        for k in (
                            "project_id",
                            "repository_id",
                            "native_user_id",
                            "binding_key",
                            "binding_digest",
                            "binding_request_id",
                            "project_binding_digest",
                        )
                    )
                    or (answer.get("natural_turn") or {}).get("policy_digest") != natural_policy_digest()
                    or digest((answer.get("natural_turn") or {}).get("decision") or {})
                    != confirmation.interpretation_digest
                    or (answer.get("natural_turn") or {}).get("human_message") != confirmation.human_message
                    or intake_digest(answer) != confirmation.intake_digest
                ):
                    raise AccessDenied("The exact same owner's verified confirmation changed")
                rows.append(answer_row)
            return record, rows

    async def actor(self, record: SCMVerificationRecordV1) -> Principal:
        async with self.store._unit():
            user = await self.polling.users.find_by_id(identifier(record.base.read_scope.native_user_id))
            if user is None or not user.enabled:
                raise AccessDenied("The mapped native verification owner is unavailable")
            return Principal(
                user.id,
                user.issuer,
                user.subject,
                authentication_method="scm_mapping",
                scm_delegation=record.base.actor_scope().model_dump(mode="json"),
            )

    async def review(self, actor: Principal, run_id: str) -> None:
        await self._current(actor, run_id)

    async def authorize_snapshot(self, actor: Data, project_id: str | None, permission: str) -> None:
        scope = SCMLinkedVerificationScopeV1.model_validate(actor.get("scm_delegation"))
        if (
            actor.get("global_admin") is not False
            or actor.get("api_audience") is not None
            or actor.get("authentication_method") != "scm_mapping"
            or actor.get("user_id") != scope.native_user_id
            or project_id != scope.project_id
            or permission not in {"read", "execute"}
        ):
            raise AccessDenied("Separate verification consent cannot grant coding, publication or foreign access")
        async with self.store._unit():
            _, record = await self._load(scope.proposal_id)
        principal = await self.actor(record)
        if actor != ProjectAccess.actor_snapshot(principal):
            raise AccessDenied("The current mapped verification actor changed")
        await self.review(principal, scope.run_id)

    @staticmethod
    def _seal(record: SCMVerificationRecordV1) -> SCMLinkedVerificationAdmissionV1:
        review, grant = record.review, record.grant
        if review is None or grant is None:
            raise AccessDenied("No confirmed native verification grant exists")
        return sealed(
            SCMLinkedVerificationAdmissionV1,
            dict(
                version=1,
                scope=record.base.actor_scope().model_dump(mode="json"),
                candidate_digest=review.candidate.digest,
                request_id=grant.request.request_id,
                request_digest=request_digest(grant.request.model_dump(mode="json")),
                review_digest=review.digest,
                publication_digest=digest(grant.publication.model_dump(mode="json")),
                confirmation_digest=grant.confirmation.digest,
                host_administrator_digest=digest(record.base.host_administrator.model_dump(mode="json")),
                grant_digest=grant.digest,
            ),
        )

    async def admit(
        self,
        actor: Principal,
        run_id: str,
        candidate: LinkedVerificationCandidateV1,
        request: LinkedVerificationInputV1,
    ) -> PreparedVerificationGrant:
        record, _ = await self._current(actor, run_id, grant=True)
        if (
            record.review is None
            or record.grant is None
            or candidate != record.review.candidate
            or request != record.grant.request
        ):
            raise AccessDenied("The native request differs from the owner's one exact reviewed candidate")
        prepared = PreparedVerificationGrant(self, actor, record, self._seal(record))
        await prepared.current()
        return prepared

    async def worker(self, saved_actor: Data, identity: Data, operation_data: Data) -> PreparedVerificationGrant:
        scope = SCMLinkedVerificationScopeV1.model_validate(saved_actor.get("scm_delegation"))
        async with self.store._unit():
            _, record = await self._load(scope.proposal_id)
        actor = await self.actor(record)
        if saved_actor != ProjectAccess.actor_snapshot(actor) or identity != {
            "issuer": actor.issuer,
            "subject": actor.subject,
        }:
            raise AccessDenied("The retained native verification identity changed")
        prepared = await self.admit(
            actor,
            scope.run_id,
            LinkedVerificationCandidateV1.model_validate(operation_data["candidate"]),
            LinkedVerificationInputV1.model_validate(operation_data["request"]),
        )
        if SCMLinkedVerificationAdmissionV1.model_validate(operation_data.get("scm_authority")) != prepared.seal:
            raise AccessDenied("The queued verification does not retain its exact native consent seal")
        return prepared

    async def _save(
        self, receipt: str, previous: SCMVerificationRecordV1 | None, current: SCMVerificationRecordV1
    ) -> None:
        current = SCMVerificationRecordV1.model_validate(current.model_dump(mode="json"))
        async with self.conversations._locked(
            [current.base.read_scope.model_dump(mode="json")], current.base.read_scope.repository_id
        ):
            row = await self.collaboration.inbox.find_by_id(identifier(receipt), lock=LockMode.PESSIMISTIC_WRITE)
            if row is None or row.data.get(KEY) != (previous.model_dump(mode="json") if previous else None):
                raise AccessDenied("The native verification proposal changed during admission")
            self.store.reject_credentials(current.model_dump(mode="json"))
            await self.collaboration._write_row(row, **{KEY: current.model_dump(mode="json")})

    async def propose(self, delivery: Data, run_id: str) -> SCMVerificationRecordV1:
        if (delivery.get("verification") or {}).get("transport") != "github_rest_poll_v1":
            raise AccessDenied("Natural linked verification currently supports authenticated GitHub polling only")
        if delivery.get(KEY):
            return SCMVerificationRecordV1.model_validate(delivery[KEY])
        scope = await self.polling.conversation_scope(delivery, remote=True)
        run = await self.store.get_run(run_id)
        if (
            run.get("project_id") != scope["project_id"]
            or run.get("repository_id") != scope["repository_id"]
            or run.get("component_id") not in scope["component_ids"]
        ):
            raise AccessDenied("Select the parent's exact native project and component")
        async with self.store._unit():
            user = await self.polling.users.find_by_id(identifier(scope["native_user_id"]))
            if user is None:
                raise AccessDenied("The native verification owner is unavailable")
            proof = self._host_admin(user)
        base = sealed(
            SCMVerificationBaseV1,
            dict(
                version=1,
                proposal_id=delivery["id"],
                run_id=run_id,
                component_id=run["component_id"],
                source_digest=delivery["digest"],
                issue_kind=delivery["issue"]["kind"],
                issue_number=delivery["issue"]["number"],
                external_actor_id=delivery["actor"],
                read_scope=scope,
                host_administrator=proof.model_dump(mode="json"),
            ),
        )
        record = SCMVerificationRecordV1(version=1, base=base)
        await self._save(delivery["id"], None, record)
        actor = await self.actor(record)
        token = CURRENT_PRINCIPAL.set(actor)
        try:
            candidate = LinkedVerificationCandidateV1.model_validate(await self.verifications.candidate(run_id))
        finally:
            CURRENT_PRINCIPAL.reset(token)
        from firefly_forge.scm_natural_runtime import catalog

        language = delivery["natural_turn"]["decision"].get("language") or "en"
        question = json.loads(
            catalog()
            .text(
                "scm-natural/verification-review",
                count=str(candidate.effect_budget),
                source=candidate.source_tree,
                original_count=str(candidate.original_effect_count),
                original_budget=str(candidate.original_effect_budget),
            )
            .strip()
        )[language]
        review = sealed(
            SCMVerificationReviewV1,
            dict(
                version=1,
                base_digest=base.digest,
                candidate=candidate.model_dump(mode="json"),
                language=language,
                question=question,
            ),
        )
        result = record.model_copy(update={"review": review})
        await self.review(actor, run_id)
        await self._save(delivery["id"], record, result)
        return result

    async def confirm(self, delivery: Data, proposal_id: str) -> LinkedVerificationReceiptV1 | None:
        async with self.store._unit():
            original, record = await self._load(proposal_id)
            publication = confirmed_publication(original.data)
        actor = await self.actor(record)
        await self.review(actor, record.base.run_id)
        scopes = await self.conversations.scopes(delivery, remote=True)
        base, review = record.base, record.review
        decision, human = delivery["natural_turn"]["decision"], delivery["natural_turn"]["human_message"]
        from firefly_forge.scm_natural_models import NaturalDecision, resolve_task
        from firefly_forge.scm_natural_questions import natural_questions

        turn = delivery["natural_turn"]
        context = turn["context"]
        resolved = resolve_task(
            context["scope"],
            context["candidates"],
            NaturalDecision.model_validate(decision),
            answer=human,
            previous=context["previous"],
        )
        projected = natural_questions(copy.deepcopy(original.data))
        if (
            resolved.model_dump(mode="json") != turn["resolution"]
            or resolved.state != "verification_confirm"
            or projected is None
            or resolved.question_id != projected["question_id"]
            or resolved.question_digest != projected["question_digest"]
        ):
            raise AccessDenied("The owner affirmation does not select this exact published native review")
        span = decision.get("verification_affirmative_span")
        if (
            review is None
            or len(scopes) != 1
            or delivery["actor"] != base.external_actor_id
            or delivery["issue"] != {"kind": base.issue_kind, "number": base.issue_number}
            or any(
                scopes[0][k] != base.read_scope.model_dump(mode="json")[k]
                for k in ("project_id", "repository_id", "native_user_id", "binding_digest", "binding_key")
            )
            or decision.get("verification_consent") != "approve"
            or not isinstance(span, str)
            or not span.strip()
            or span not in human
        ):
            raise AccessDenied("A clear affirmative from the same verified review owner is required")
        confirmation = sealed(
            SCMVerificationConfirmationV1,
            dict(
                version=1,
                review_digest=review.digest,
                receipt_id=delivery["id"],
                source_digest=delivery["digest"],
                human_message=human,
                human_sha256=hashlib.sha256(human.encode()).hexdigest(),
                affirmative_span=span,
                interpretation_digest=digest(decision),
                intake_digest=intake_digest(delivery),
            ),
        )
        request = LinkedVerificationInputV1(
            version=1,
            request_id=base.proposal_id,
            candidate_digest=review.candidate.digest,
            acknowledge_all_fresh_checks=True,
            acknowledge_no_coding_or_repair=True,
            acknowledge_original_history_and_budget_preserved=True,
        )
        grant = sealed(
            SCMVerificationGrantV1,
            dict(
                version=1,
                review_digest=review.digest,
                publication=publication,
                confirmation=confirmation.model_dump(mode="json"),
                request=request.model_dump(mode="json"),
            ),
        )
        if record.grant is not None:
            if record.grant != grant:
                raise AccessDenied("This review already has another immutable owner confirmation")
        else:
            current = SCMVerificationRecordV1.model_validate(
                {
                    **record.model_dump(mode="json"),
                    "grant": grant.model_dump(mode="json"),
                    "intent": SCMVerificationIntentV1(
                        version=1,
                        request_id=request.request_id,
                        request_digest=request_digest(request.model_dump(mode="json")),
                        state="admitting",
                    ).model_dump(mode="json"),
                }
            )
            from firefly_forge.scm_natural_tasks import SCMNaturalTasks

            async with self.conversations._locked(scopes, base.read_scope.repository_id):
                answer = await self.collaboration.inbox.find_by_id(
                    identifier(delivery["id"]), lock=LockMode.PESSIMISTIC_WRITE
                )
                if (
                    answer is None
                    or observation_cut(answer.data) != observation_cut(delivery)
                    or intake_digest(answer.data) != confirmation.intake_digest
                    or await self.conversations.scopes(answer.data, remote=False) != scopes
                ):
                    raise AccessDenied("The exact owner confirmation changed before native consent")
                # Serialize publication state with this grant, including other-purpose questions.
                for peer in sorted(
                    await self.collaboration.inbox.conversation_context(answer.data), key=lambda row: row.data["id"]
                ):
                    await self.collaboration.inbox.find_by_id(
                        identifier(peer.data["id"]), lock=LockMode.PESSIMISTIC_WRITE
                    )
                if await SCMNaturalTasks(self.collaboration)._context(answer.data, scopes) != context:
                    raise AccessDenied("The pending conversation changed; clarify this verification review again")
                await self.review(actor, base.run_id)
                await self._save(proposal_id, record, current)
            record = current
            # Once this durable intent exists, an uncertain POST is read back, never repeated.
            token = CURRENT_PRINCIPAL.set(actor)
            value: LinkedVerificationReceiptV1 | None
            try:
                try:
                    value = LinkedVerificationReceiptV1.model_validate(
                        await self.verifications.request(base.run_id, request)
                    )
                except Exception:
                    value = await self.status(record, missing_ok=True)
            finally:
                CURRENT_PRINCIPAL.reset(token)
            assert record.intent is not None
            observed = record.model_copy(
                update={
                    "intent": record.intent.model_copy(
                        update={"state": "queued" if value is not None else "needs_reconciliation"}
                    )
                }
            )
            await self._save(proposal_id, record, observed)
            return value
        return await self.status(record, missing_ok=True)

    async def status(
        self, record: SCMVerificationRecordV1, *, missing_ok: bool = False
    ) -> LinkedVerificationReceiptV1 | None:
        if record.grant is None:
            return None
        assert record.review is not None
        actor = await self.actor(record)
        token = CURRENT_PRINCIPAL.set(actor)
        try:
            try:
                value = LinkedVerificationReceiptV1.model_validate(
                    await self.verifications.get(record.base.run_id, record.base.proposal_id)
                )
            except KeyError:
                if missing_ok:
                    return None
                raise
            if value.candidate != record.review.candidate or value.request != record.grant.request:
                raise AccessDenied("The current linked operation differs from the exact consented review")
            return value
        finally:
            CURRENT_PRINCIPAL.reset(token)

    async def context(self, delivery: Data, scopes: list[Data]) -> list[Data]:
        if len(scopes) != 1:
            return []
        scope = scopes[0]
        async with self.store._unit():
            rows = await self.collaboration.inbox.conversation_context(delivery)
            values = [copy.deepcopy(row.data) for row in rows if row.data.get(KEY)]
        result = []
        for original in values:
            record = SCMVerificationRecordV1.model_validate(original[KEY])
            base = record.base
            if (
                base.external_actor_id == delivery["actor"]
                and base.read_scope.native_user_id == scope["native_user_id"]
                and base.read_scope.binding_digest == scope["binding_digest"]
                and record.review is not None
            ):
                result.append(
                    dict(
                        proposal_id=base.proposal_id,
                        run_id=base.run_id,
                        candidate=record.review.candidate.model_dump(mode="json"),
                        confirmed=record.grant is not None,
                        admission_intent=record.intent.state if record.intent else None,
                    )
                )
        return result

    async def apply(self, receipt_id: str) -> Data:
        try:
            return await self._apply(receipt_id)
        except (AccessDenied, ValueError, KeyError):
            async with self.store._unit():
                row = await self.collaboration.inbox.find_by_id(identifier(receipt_id), lock=LockMode.PESSIMISTIC_WRITE)
                if row is None or not row.data.get("natural_turn"):
                    raise
                turn = row.data["natural_turn"]
                await self.collaboration._write_row(
                    row,
                    natural_turn={
                        **turn,
                        "state": "verification_held",
                        "verification_hold": "current_authority_or_review_changed",
                    },
                )
            return dict(await self.collaboration.receipt(receipt_id))

    async def _apply(self, receipt_id: str) -> Data:
        delivery = await self.collaboration.receipt(receipt_id)
        turn = delivery["natural_turn"]
        resolution = turn["resolution"]
        proposal, state = None, None
        if resolution["state"] == "verification_request":
            record = await self.propose(delivery, resolution["run_id"])
            proposal, state = record.base.proposal_id, "verification_review"
        elif resolution["state"] == "verification_confirm":
            proposal = resolution["question_id"].removeprefix("natural-")
            value = await self.confirm(delivery, proposal)
            state = "verification_current" if value is not None else "verification_unknown"
        elif resolution["state"] == "status":
            records = [
                item
                for item in turn["context"].get("linked_verifications", [])
                if item["run_id"] == resolution["run_id"] and item["confirmed"]
            ]
            if len(records) > 1:
                async with self.store._unit():
                    row = await self.collaboration.inbox.find_by_id(
                        identifier(receipt_id), lock=LockMode.PESSIMISTIC_WRITE
                    )
                    if row is None or row.data.get("natural_turn") != turn:
                        raise AccessDenied("The exact interpreted status request changed")
                    await self.collaboration._write_row(
                        row,
                        natural_turn={
                            **turn,
                            "state": "verification_held",
                            "verification_hold": "operation_selection_ambiguous",
                        },
                    )
                return dict(await self.collaboration.receipt(receipt_id))
            if len(records) == 1:
                proposal, state = records[0]["proposal_id"], "verification_current"
        if proposal is None:
            return dict(delivery)
        async with self.store._unit():
            row = await self.collaboration.inbox.find_by_id(identifier(receipt_id), lock=LockMode.PESSIMISTIC_WRITE)
            if row is None or row.data.get("natural_turn") != turn:
                raise AccessDenied("The exact interpreted owner request changed")
            await self.collaboration._write_row(
                row, natural_turn={**turn, "state": state, "linked_verification_proposal": proposal}
            )
        return dict(await self.collaboration.receipt(receipt_id))

    async def communication(self, delivery: Data) -> Any:
        from firefly_forge.models import Command, NewWorkCommand
        from firefly_forge.scm_linked_verification_records import SCMVerificationCommunicationV1

        turn = delivery.get("natural_turn") or {}
        if turn.get("verification_hold"):
            return SCMVerificationCommunicationV1(phase="held", hold_reason=turn["verification_hold"])
        resolution = turn.get("resolution") or {}
        proposal = turn.get("linked_verification_proposal")
        if proposal is None and resolution.get("state") == "verification_request":
            proposal = delivery["id"]
        if proposal is None and resolution.get("state") == "verification_confirm":
            proposal = (resolution.get("question_id") or "").removeprefix("natural-")
        if proposal is None:
            return None
        try:
            async with self.store._unit():
                _, record = await self._load(proposal)
        except AccessDenied:
            if resolution.get("state") not in {"verification_request", "verification_confirm"}:
                raise
            return SCMVerificationCommunicationV1(phase="held", hold_reason="native_review_unconfirmed")
        if record.review is None:
            return SCMVerificationCommunicationV1(phase="held", hold_reason="native_review_unconfirmed")
        base = record.base
        scope = turn["context"]["scope"]
        if (
            delivery["actor"] != base.external_actor_id
            or delivery["repository_id"] != base.read_scope.repository_id
            or delivery["issue"] != {"kind": base.issue_kind, "number": base.issue_number}
            or scope["native_user_id"] != base.read_scope.native_user_id
            or scope["binding_digest"] != base.read_scope.binding_digest
        ):
            raise AccessDenied("The verification readback belongs to another owner or conversation")
        await self.review(await self.actor(record), base.run_id)
        if turn["state"] == "verification_review" or resolution.get("state") == "verification_request":
            return SCMVerificationCommunicationV1(
                phase="review",
                candidate=record.review.candidate,
                question=record.review.question,
                request_id=base.proposal_id,
            )
        if (
            record.grant is not None
            and resolution.get("state") == "verification_confirm"
            and (
                record.grant.confirmation.receipt_id != delivery["id"]
                or record.grant.confirmation.source_digest != delivery["digest"]
            )
        ):
            raise AccessDenied("The durable native consent belongs to another exact confirmation")
        receipt = await self.status(record, missing_ok=True)
        command = None
        if receipt and receipt.last_failure:
            async with self.store._unit():
                row = await self.verifications.requests.records.find_by_id(identifier(record.base.proposal_id))
                if row is None:
                    raise AccessDenied("The actual failed command record is unavailable")
                data = copy.deepcopy(row.data)
            ordinal = receipt.last_failure.ordinal
            if data.get("candidate") != receipt.candidate.model_dump(mode="json") or not 1 <= ordinal <= len(
                data.get("commands", [])
            ):
                raise AccessDenied("The actual failed command scope changed")
            command_value = data["commands"][ordinal - 1]
            command_type = NewWorkCommand if command_value.get("timeout") is None else Command
            command = command_type.model_validate(command_value)
            self.store.reject_credentials(command.model_dump(mode="json"))
        return SCMVerificationCommunicationV1(
            phase="current" if receipt else "unknown",
            candidate=record.review.candidate,
            receipt=receipt,
            failure_command=command,
            request_id=record.base.proposal_id,
        )
