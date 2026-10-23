# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Finite functional polling authority with durable model-turn admission."""

from __future__ import annotations

import copy
import hashlib
import json
import stat
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from pyfly.data.relational.sqlalchemy import LockMode
from pyfly.data.transaction import current_unit_of_work
from sqlalchemy import Text, cast, func, select, text

from firefly_forge.access_persistence import ForgeAccessEventEntity
from firefly_forge.channel_actions import ChannelCommand
from firefly_forge.channel_products import ChannelProducts
from firefly_forge.identity import Principal
from firefly_forge.identity_persistence import ForgeIdentityUserRepository
from firefly_forge.journal import digest
from firefly_forge.product_provenance import content_digest
from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied, Permission, ProjectAccess
from firefly_forge.project_persistence import ForgeProjectRepository
from firefly_forge.scm_poll_functional_models import SCMPollFunctionalGrantInputV1, SCMPollFunctionalGrantReceiptV1
from firefly_forge.scm_poll_models import GitHubPollReferenceV1
from firefly_forge.scm_polling import BINDING_KIND, SCMPolling, poll_key
from firefly_forge.scm_resume_service import grant_state
from firefly_forge.store import identifier, now

Data = dict[str, Any]
STATE_KEY = "_scm_poll_functional_v1"
USAGE_KEY = "_scm_poll_functional_usage_v1"


def functional_policy_digest() -> str:
    root = Path(__file__).parent
    names = (
        "scm_poll_functional.py",
        "scm_poll_functional_models.py",
        "scm_functional_address.py",
        "product_assistant.py",
        "product_assistant_runtime.py",
        "channel_products.py",
        "scm_polling.py",
        "collaboration.py",
        "prompts.py",
        "providers.py",
        "scm_actor_authority.py",
        "project_access.py",
        "resources/prompts/v1/assistant/system.yaml",
        "resources/prompts/v1/assistant/request.yaml",
        "resources/prompts/v1/assistant/frozen-skills.yaml",
        "resources/prompts/v1/manifest.json",
        "resources/prompts/v1/assistant/functional-policy.yaml",
    )
    hashes = {}
    for name in names:
        path = root / name
        for parent in path.parents:
            if parent == root:
                break
            if parent.is_symlink():
                raise AccessDenied("The package-owned functional policy parent changed")
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1 or metadata.st_size > 2 * 1024 * 1024:
            raise AccessDenied("The package-owned functional policy file changed")
        hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    from firefly_forge.scm_natural_tasks import natural_policy_digest

    hashes["natural_product_projection_policy"] = natural_policy_digest()
    return digest(hashes)


def functional_state(data: Data) -> Data:
    value = copy.deepcopy(data.get(USAGE_KEY, {"version": 1, "turns": {}, "conversations": {}}))
    if (
        not isinstance(value, dict)
        or set(value) != {"version", "turns", "conversations"}
        or type(value["version"]) is not int
        or value["version"] != 1
        or not isinstance(value["turns"], dict)
        or not isinstance(value["conversations"], dict)
        or len(value["turns"]) > 5000
        or len(value["conversations"]) > 5000
        or len(json.dumps(value).encode()) > 16 * 1024 * 1024
    ):
        raise ValueError("The functional allowance ledger is invalid or full")
    return value


def reserve_turn(state: Data, grant_digest: str, delivery_id: str, maximum: int) -> bool:
    if grant_digest not in state["turns"]:
        raise ValueError("The durable functional allowance ledger is missing")
    uses = state["turns"][grant_digest]
    if not isinstance(uses, list) or any(not isinstance(item, str) for item in uses) or len(set(uses)) != len(uses):
        raise ValueError("The functional turn ledger is invalid")
    if delivery_id in uses:
        return False
    if type(maximum) is not int or not 1 <= maximum <= 3 or len(uses) >= maximum:
        raise ValueError("The separate assistant allowance is exhausted")
    uses.append(delivery_id)
    return True


def validate_expiration(request: Data, clock: datetime, *, admission: bool = True) -> None:
    expiration = datetime.fromisoformat(request["expires_at"])
    if expiration.utcoffset() is None or expiration <= clock:
        raise ValueError("The separate assistant allowance expired")
    if admission and expiration > clock + timedelta(minutes=60):
        raise ValueError("Assistant consent may last at most 60 minutes")


class SCMPollFunctionalAuthority:
    actor_transport = "github_rest_poll_functional_v1"
    state_key = STATE_KEY
    event_kind = "scm_poll_functional_grant_v1"
    grant_version = 1
    request_type = SCMPollFunctionalGrantInputV1
    receipt_type = SCMPollFunctionalGrantReceiptV1

    def __init__(self, store: Any, polling: SCMPolling) -> None:
        self.store, self.polling, self.authority = store, polling, polling.authority
        self.settings, self.inbox, self.events = polling.settings, polling.inbox, polling.events
        self._active: ContextVar[tuple[Principal, Data] | None] = ContextVar("poll_functional", default=None)

    async def _record(self, event_id: str, project: str, repository: str) -> Data:
        async with self.store._unit() as session:
            size = await session.scalar(
                select(func.octet_length(cast(ForgeAccessEventEntity.data, Text))).where(
                    ForgeAccessEventEntity.id == identifier(event_id)
                )
            )
            if size is None:
                raise KeyError("Functional grant not found")
            if size > 65536:
                raise ValueError("Functional grant exceeds 64 KiB")
            row = await self.events.find_by_id(identifier(event_id))
            if (
                row is None
                or row.project_id.hex != project
                or set(row.data) != {"kind", "receipt"}
                or row.data["kind"] != self.event_kind
            ):
                raise ValueError("Functional grant event scope is invalid")
            receipt = self.receipt_type.model_validate(row.data["receipt"]).model_dump(mode="json")
        if set(receipt) != {
            "version",
            "event_id",
            "project_id",
            "repository_id",
            "binding_key",
            "native_user_id",
            "revision",
            "previous_digest",
            "prepared_at",
            "administering_user_id",
            "admin_snapshot",
            "request",
            "request_digest",
            "digest",
        }:
            raise ValueError("Functional grant envelope is invalid")
        request = self.request_type.model_validate(receipt["request"]).model_dump(mode="json")
        if (
            receipt["version"] != self.grant_version
            or type(receipt["version"]) is not int
            or receipt["event_id"] != event_id
            or receipt["project_id"] != project
            or receipt["repository_id"] != repository
            or receipt["admin_snapshot"].get("user_id") != receipt["administering_user_id"]
            or receipt["admin_snapshot"].get("authentication_method") == "scm_mapping"
            or request != receipt["request"]
            or digest(request) != receipt["request_digest"]
            or receipt["previous_digest"] != request["expected_previous_digest"]
            or digest({k: v for k, v in receipt.items() if k != "digest"}) != receipt["digest"]
            or type(receipt["revision"]) is not int
            or not 1 <= receipt["revision"] <= 50
        ):
            raise ValueError("Functional grant immutable binding is invalid")
        return receipt

    async def _grant(
        self, project: str, repository: str, *, key: str | None = None, request_id: str | None = None
    ) -> Data | None:
        async with self.store._unit() as session:
            row = await self.settings.find_for_repository(identifier(repository))
            state = grant_state(row.data if row else {}, self.state_key, self.grant_version)
            pointer = state["heads"].get(key) if key else state["requests"].get(project + ":" + str(request_id))
            latest = None
            if key:
                latest = await session.scalar(
                    select(ForgeAccessEventEntity.id)
                    .where(
                        ForgeAccessEventEntity.project_id == identifier(project),
                        ForgeAccessEventEntity.data["kind"].as_string() == self.event_kind,
                        ForgeAccessEventEntity.data["receipt"]["repository_id"].as_string() == repository,
                        ForgeAccessEventEntity.data["receipt"]["binding_key"].as_string() == key,
                    )
                    .order_by(ForgeAccessEventEntity.data["receipt"]["revision"].as_integer().desc())
                    .limit(1)
                )
        if pointer is None:
            if latest is not None:
                raise ValueError("Functional history has no consistent current head")
            return None
        if not isinstance(pointer, dict) or set(pointer) != {"event_id", "digest"}:
            raise ValueError("Functional grant pointer is invalid")
        retained = await self._record(pointer["event_id"], project, repository)
        if (
            retained["digest"] != pointer["digest"]
            or key
            and (latest is None or latest.hex != retained["event_id"] or retained["binding_key"] != key)
            or request_id
            and retained["request"]["request_id"] != request_id
        ):
            raise ValueError("Functional grant pointer does not bind immutable history")
        return retained

    async def _return(self, actor: Any, receipt: Data, *, admin: bool) -> Data:
        await self.store.admit_credentials(receipt)
        await self.authority._reader(actor, receipt["project_id"], receipt["repository_id"], admin=admin)
        self.store.reject_credentials(receipt)
        return copy.deepcopy(receipt)

    async def get(self, project: str, repository: str, request_id: str) -> Data:
        if current_unit_of_work() is not None:
            raise ValueError("Read functional grants outside a caller transaction")
        actor = CURRENT_PRINCIPAL.get()
        await self.authority._reader(actor, project, repository, admin=False)
        receipt = await self._grant(project, repository, request_id=request_id)
        if receipt is None:
            raise KeyError("Functional grant request not found; absence never permits another write")
        return await self._return(actor, receipt, admin=False)

    async def current(self, project: str, repository: str, binding_request: str) -> Data:
        if current_unit_of_work() is not None:
            raise ValueError("Read functional grants outside a caller transaction")
        actor = CURRENT_PRINCIPAL.get()
        await self.authority._reader(actor, project, repository, admin=False)
        binding = await self._current_binding(project, repository, binding_request)
        if binding is None:
            raise KeyError("Polling V2 binding not found")
        receipt = await self._grant(project, repository, key=binding["binding_key"])
        if receipt is None:
            raise KeyError("No separately consented functional grant exists")
        return await self._return(actor, receipt, admin=False)

    async def _native_readiness(self, project: str, user_id: str) -> Data:
        async with self.store._unit():
            user = await self.polling.users.find_by_id(identifier(user_id))
            memberships = await self.polling.memberships.for_user(identifier(user_id))
            enabled = user is not None and user.enabled
            permissions = {
                permission: enabled
                and any(
                    item.project_id.hex == project and item.active and ProjectAccess.permits(item.role, permission)
                    for item in memberships
                )
                for permission in ("read", "edit")
            }
        return dict(
            native_user_id=user_id,
            enabled=enabled,
            actions={
                action: dict(permission=permission, ready=permissions[permission])
                for action, permission in (
                    ("assistant_start", "edit"),
                    ("assistant_status", "read"),
                    ("assistant_turn", "edit"),
                )
            },
        )

    async def _require_native_actions(
        self, project: str, binding: Data, request: SCMPollFunctionalGrantInputV1
    ) -> None:
        if not request.active:
            return
        readiness = await self._native_readiness(project, binding["request"]["native_user_id"])
        if any(not readiness["actions"][action]["ready"] for action in request.allowed_actions):
            raise AccessDenied(
                "The mapped native user needs an enabled account and current project membership: "
                "read for assistant status, edit for assistant start or turn. Review Project access before consenting."
            )

    async def configure(self, project: str, repository: str, request: SCMPollFunctionalGrantInputV1) -> Data:
        if current_unit_of_work() is not None:
            raise ValueError("Admit functional consent outside a caller transaction")
        request = self.request_type.model_validate(request.model_dump(mode="json"))
        actor = CURRENT_PRINCIPAL.get()
        await self.authority._reader(actor, project, repository, admin=True)
        assert actor is not None
        incoming = request.model_dump(mode="json")
        validate_expiration(incoming, datetime.now(UTC))
        await self.store.admit_credentials(incoming)
        await self.authority._reader(actor, project, repository, admin=True)
        retained = await self._grant(project, repository, request_id=request.request_id)
        if retained is not None:
            if retained["request"] != incoming or retained["administering_user_id"] != actor.id.hex:
                raise AccessDenied("The retained request belongs to another input or administrator")
            return await self._return(actor, retained, admin=True)
        binding = await self._binding(project, repository, request)
        await self._require_native_actions(project, binding, request)
        await self._preflight({"binding": binding})
        async with self.store._unit() as session:
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": self.store.schema + ":members:" + project},
            )
            for user in sorted({actor.id.hex, binding["request"]["native_user_id"]}):
                identity = await ForgeIdentityUserRepository().find_by_id(
                    identifier(user), lock=LockMode.PESSIMISTIC_WRITE
                )
                if identity is None or not identity.enabled:
                    raise AccessDenied("Both administrator and delegated user must remain enabled")
            await ForgeProjectRepository().find_by_id(identifier(project), lock=LockMode.PESSIMISTIC_WRITE)
            await self.store.repository_records.find_by_id(identifier(repository), lock=LockMode.PESSIMISTIC_WRITE)
            await self.authority._reader(actor, project, repository, admin=True)
            if await self._binding(project, repository, request) != binding:
                raise AccessDenied("The reviewed authority cut changed during admission")
            await self._require_native_actions(project, binding, request)
            existing = await self._grant(project, repository, request_id=request.request_id)
            if existing is not None:
                if existing["request"] != incoming or existing["administering_user_id"] != actor.id.hex:
                    raise AccessDenied("The retained request belongs to another input or administrator")
                retained = existing
            else:
                row = await self.settings.find_for_repository(identifier(repository))
                if row is None:
                    raise AccessDenied("Configure collaboration before consenting to functional")
                state = grant_state(row.data, self.state_key, self.grant_version)
                previous = await self._grant(project, repository, key=binding["binding_key"])
                if request.expected_previous_digest != (previous["digest"] if previous else None):
                    raise AccessDenied("Functional grant revision changed; preserve the reviewed request")
                revision = previous["revision"] + 1 if previous else 1
                if (
                    revision > 50
                    or len(state["requests"]) >= 5000
                    or binding["binding_key"] not in state["heads"]
                    and len(state["heads"]) >= 100
                ):
                    raise ValueError("Functional grant retention limit reached")
                retained = dict(
                    version=self.grant_version,
                    event_id=uuid4().hex,
                    project_id=project,
                    repository_id=repository,
                    binding_key=binding["binding_key"],
                    native_user_id=binding["request"]["native_user_id"],
                    revision=revision,
                    previous_digest=request.expected_previous_digest,
                    prepared_at=now(),
                    administering_user_id=actor.id.hex,
                    admin_snapshot=ProjectAccess.actor_snapshot(actor),
                    request=incoming,
                    request_digest=digest(incoming),
                )
                retained["digest"] = digest(retained)
                self.receipt_type.model_validate(retained)
                envelope = {"kind": self.event_kind, "receipt": retained}
                total = await session.scalar(
                    select(
                        func.coalesce(func.sum(func.octet_length(cast(ForgeAccessEventEntity.data, Text))), 0)
                    ).where(
                        ForgeAccessEventEntity.data["kind"].as_string() == self.event_kind,
                        ForgeAccessEventEntity.data["receipt"]["repository_id"].as_string() == repository,
                    )
                )
                if (
                    len(json.dumps(envelope, ensure_ascii=False).encode()) > 65536
                    or int(total or 0) + len(json.dumps(envelope, ensure_ascii=False).encode()) > 16 * 1024 * 1024
                ):
                    raise ValueError("Functional grant history exceeds its byte budget")
                self.store.reject_credentials(retained)
                await self.events.save(
                    ForgeAccessEventEntity(
                        id=identifier(retained["event_id"]), project_id=identifier(project), data=envelope
                    )
                )
                pointer = {"event_id": retained["event_id"], "digest": retained["digest"]}
                state["heads"][binding["binding_key"]] = pointer
                state["requests"][project + ":" + request.request_id] = pointer
                usage = functional_state(row.data)
                usage["turns"][retained["digest"]] = []
                row.data = {**row.data, self.state_key: state, USAGE_KEY: usage}
                functional_state(row.data)
                await self.settings.save(row)
        return await self._return(actor, retained, admin=True)

    def _model(self, request: SCMPollFunctionalGrantInputV1) -> Data:
        if functional_policy_digest() != request.expected_functional_policy_digest:
            raise AccessDenied("The consented functional runtime policy changed")
        router = self.store.task_router
        if router is None:
            raise AccessDenied("Functional polling requires an explicit named product model profile")
        routing = router.current()
        record = router.profiles._read(request.model_profile_id)
        cut = dict(
            profile_id=request.model_profile_id,
            provider=record["provider"],
            profile_revision=record["revision"],
            profile_digest=content_digest({"provider": record["provider"], "settings": record["settings"]}),
        )
        if (
            routing["bindings"].get("product") != request.model_profile_id
            or routing["digest"] != request.expected_routing_digest
            or cut["profile_revision"] != request.expected_model_profile_revision
            or cut["profile_digest"] != request.expected_model_profile_digest
            or cut["provider"] not in {"bridge", "agentic"}
        ):
            raise AccessDenied("The consented assistant profile or routing changed")
        return cut

    async def _current_binding(self, project: str, repository: str, request_id: str) -> Data | None:
        async with self.store._unit():
            _, state = await self.polling._state(repository)
        pointer = state["requests"].get(project + ":" + request_id)
        return await self.polling._event(pointer, BINDING_KIND, project, repository) if pointer else None

    async def _binding(self, project: str, repository: str, request: SCMPollFunctionalGrantInputV1) -> Data:
        binding = await self._current_binding(project, repository, request.expected_binding_request_id)
        if binding is None:
            raise AccessDenied("Select an exact current polling V2 actor binding")
        policy, state = await self.polling._policy(repository)
        await self.polling._scope(repository, policy)
        project_cut = await self.authority._project(project, repository, request.component_ids)
        observed = await self.polling._observation(project, repository, binding["request"]["observed_receipt_id"])
        if (
            await self.polling._head(project, repository, binding["binding_key"]) != binding
            or policy["project_id"] != project
            or not state["cursor"]["ready"]
            or binding["digest"] != request.expected_binding_digest
            or not binding["request"]["active"]
            or not set(request.component_ids) <= set(binding["request"]["component_ids"])
            or binding["project_binding_digest"] != digest(project_cut)
            or digest(project_cut) != request.expected_project_binding_digest
            or observed.get("source_conflict")
            or observed["verification"]["issue_number"] != request.issue_number
            or any(binding["verified_reference"].get(k) != v for k, v in policy["scope"].items())
        ):
            raise AccessDenied("The functional polling binding or exact issue/project cut changed")
        self._model(request)
        validate_expiration(request.model_dump(mode="json"), datetime.now(UTC), admission=False)
        return binding

    async def _preflight(self, context: Data) -> None:
        binding = context["binding"]
        delivery = context.get("delivery") or await self.polling._observation(
            binding["project_id"], binding["repository_id"], binding["request"]["observed_receipt_id"]
        )
        await self.polling._fresh(delivery, await self.store.repository(binding["repository_id"]))

    async def _after_admission(self, context: Data) -> None:
        await self._preflight(context)

    async def review(self, project: str, repository: str, binding_request: str) -> Data:
        actor = CURRENT_PRINCIPAL.get()
        await self.authority._reader(actor, project, repository, admin=True)
        binding = await self._current_binding(project, repository, binding_request)
        if binding is None:
            raise KeyError("Polling actor binding not found")
        cut = await self.authority._project(project, repository, binding["request"]["component_ids"])
        router = self.store.task_router
        if router is None or not router.current()["bindings"].get("product"):
            raise AccessDenied("Choose an explicit product task model profile first")
        name = router.current()["bindings"]["product"]
        record = router.profiles._read(name)
        value = dict(
            binding=binding,
            project_binding_digest=digest(cut),
            model_profile_id=name,
            model_profile_revision=record["revision"],
            model_profile_digest=content_digest({"provider": record["provider"], "settings": record["settings"]}),
            provider=record["provider"],
            routing_digest=router.current()["digest"],
            functional_policy_digest=functional_policy_digest(),
            max_model_turns=3,
            max_duration_minutes=60,
        )
        await self._preflight({"binding": binding})
        value["native_user_readiness"] = await self._native_readiness(project, binding["request"]["native_user_id"])
        return await self.polling._return(actor, project, repository, value, admin=True)

    async def _context(self, delivery: Data, *, remote: bool = False) -> Data:
        from firefly_forge.collaboration import RepositoryCommand
        from firefly_forge.scm_functional_address import AddressedFunctionalV1

        raw = delivery.get("command") or {}
        product_projection = delivery.get("natural_product_parent") is not None
        if product_projection:
            from firefly_forge.scm_natural_product import authorize_product_projection

            await authorize_product_projection(self, delivery, remote=remote)
        else:
            parsed = RepositoryCommand.parse(
                delivery["source_snapshot"]["body"],
                bot_addresses=self.polling.scm.bot_addresses("github"),
                functional_dialogue=True,
            )
            if parsed.model_dump(exclude_none=True) != raw:
                raise AccessDenied("The retained functional command differs from its observed source")
        natural = AddressedFunctionalV1.model_validate(raw) if raw.get("adapter") == "addressed_functional_v1" else None
        command = None if natural else ChannelCommand.model_validate(raw)
        operation: str | None
        repository = delivery["repository_id"]
        if natural:
            operation = natural.operation
            policy, _ = await self.polling._policy(repository)
            project = policy["project_id"]
        else:
            assert command is not None
            operation = command.operation
            if (
                command.action != "assistant"
                or command.operation not in {"start", "status", "turn"}
                or not command.project_id
            ):
                raise AccessDenied("Functional polling only permits typed project assistant actions")
            project = command.project_id
        reference = GitHubPollReferenceV1.model_validate(delivery.get("verification")).model_dump(mode="json")
        if (
            await self.polling._observation(project, repository, delivery["id"]) != delivery
            or delivery.get("source_conflict")
            or delivery["actor"] != reference["external_actor_id"]
            or delivery["digest"] != reference["payload_digest"]
            or delivery["remote_id"] != reference["remote_repository_id"]
        ):
            raise AccessDenied("The API-observed functional command source changed")
        key = poll_key(project, reference)
        grant = await self._grant(project, repository, key=key)
        if grant is None:
            raise AccessDenied("A separate functional polling grant is required")
        request = self.request_type.model_validate(grant["request"])
        prepared = datetime.fromisoformat(grant["prepared_at"])
        if not product_projection and (
            datetime.fromisoformat(delivery["created_at"]) < prepared
            or datetime.fromisoformat(reference["observed_at"]) < prepared
        ):
            raise AccessDenied("The functional command predates this explicit allowance")
        if delivery.get("issue") != {"kind": "issue", "number": reference["issue_number"]}:
            raise AccessDenied("Functional dialogue requires its exact observed issue")
        async with self.store._unit():
            administrator = await self.polling.users.find_by_id(identifier(grant["administering_user_id"]))
            if (
                administrator is None
                or not administrator.enabled
                or not await self.polling.access.authorize_actor(grant["admin_snapshot"], project, "members")
            ):
                raise AccessDenied("The functional grant administrator no longer has current authority")
        binding = await self._binding(project, repository, request)
        if (
            not request.active
            or "assistant_" + str(operation) not in request.allowed_actions
            or reference["issue_number"] != request.issue_number
            or grant["native_user_id"] != binding["request"]["native_user_id"]
            or any(
                reference[k] != binding["verified_reference"][k]
                for k in (
                    "provider",
                    "api_origin",
                    "installation_id",
                    "remote_repository_id",
                    "external_actor_id",
                    "connection_revision",
                    "connection_scope_digest",
                    "transport",
                )
            )
        ):
            raise AccessDenied("The functional grant does not authorize this account, issue or action")
        async with self.store._unit():
            user = await self.polling.users.find_by_id(identifier(grant["native_user_id"]))
            memberships = await self.polling.memberships.for_user(identifier(grant["native_user_id"]))
            row, _ = await self.polling._state(repository)
            if (
                user is None
                or not user.enabled
                or not row.data.get("enabled")
                or delivery["actor"] not in row.data.get("trusted_actors", [])
                or "assistant" not in row.data.get("channel_commands", [])
                or not any(
                    item.project_id.hex == project
                    and item.active
                    and ProjectAccess.permits(item.role, "read" if operation == "status" else "edit")
                    for item in memberships
                )
            ):
                raise AccessDenied("Current native assistant permission is required")
            usage = functional_state(row.data)
            if grant["digest"] not in usage["turns"]:
                raise AccessDenied("The durable functional allowance ledger is missing")
        if natural:
            expected = dict(
                project_id=project,
                repository_id=repository,
                issue_number=request.issue_number,
                binding_key=key,
                model=self._model(request),
            )
            candidates = [name for name, scope in usage["conversations"].items() if scope == expected]
            if len(candidates) != 1:
                raise AccessDenied(
                    "Natural dialogue requires one exact issue-bound conversation; use a typed conversation ID"
                )
            command = ChannelCommand(
                action="assistant",
                operation="turn",
                project_id=project,
                conversation_id=candidates[0],
                request_id=digest({"delivery_id": delivery["id"], "source_digest": delivery["digest"]})[:32],
                message=natural.message,
            )
        assert command is not None
        project_cut = await self.authority._project(project, repository, request.component_ids)
        products = ChannelProducts(self.store, self.polling.collaboration)
        members = {}
        for member_id in sorted(
            {repository, *(item["repository_id"] for item in project_cut["definition"]["components"])}
        ):
            member = await self.store.repository(member_id)
            settings = await self.polling.collaboration.settings(member_id)
            if (
                not settings["enabled"]
                or delivery["actor"] not in settings["trusted_actors"]
                or "assistant" not in settings["channel_commands"]
                or command.operation != "status"
                and delivery["actor"] not in settings["configuration_actors"]
            ):
                raise AccessDenied("Current member-repository assistant authority is required")
            members[member_id] = dict(repository=member, scope=self.polling.scm.delegation_scope(member))
        if remote:
            await products._members(
                await self.store.repository(repository),
                command,
                delivery,
                [project_cut["definition"]],
                configure=command.operation != "status",
            )
        if self.store.execution_ownership_acquired:
            await self.store.assert_ownership()
        model = self._model(request)
        if project_cut["definition"]["product_repository_id"] != repository:
            raise AccessDenied("Functional polling must originate in the exact product repository")
        context = dict(
            binding=binding,
            grant=grant,
            request=request,
            command=command,
            project=project_cut,
            model=model,
            user=user,
            delivery=delivery,
            members=members,
        )
        if command.operation != "start":
            if command.conversation_id is None:
                raise AccessDenied("Select the exact issue-bound conversation")
            expected = dict(
                project_id=project,
                repository_id=repository,
                issue_number=request.issue_number,
                binding_key=key,
                model=model,
            )
            if usage["conversations"].get(command.conversation_id) != expected:
                raise AccessDenied("The conversation is not bound to this exact issue/account/model")
            manager = self.polling.collaboration.product_assistant
            async with self.store._unit():
                session = await manager.records.find_by_id(identifier(command.conversation_id))
                if (
                    session is None
                    or session.repository_id.hex != repository
                    or session.data.get("project_snapshot") != project_cut
                    or any((session.data.get("model_selection") or {}).get(k) != v for k, v in model.items())
                ):
                    raise AccessDenied("The frozen functional conversation or model cut changed")
        if remote:
            await self._preflight(context)
        return context

    def model_contract(self) -> str:
        active = self._active.get()
        if active is None or CURRENT_PRINCIPAL.get() != active[0] or active[1]["command"].operation != "turn":
            raise AccessDenied("A current reserved functional model turn is required")
        return "functional_intent_v1"

    async def authorize_snapshot(self, actor: Data, project: str | None, permission: Permission) -> None:
        active = self._active.get()
        if active is None or CURRENT_PRINCIPAL.get() != active[0] or actor != ProjectAccess.actor_snapshot(active[0]):
            raise AccessDenied("Functional authority exists only in its current verified action")
        original = active[1]
        allowed = {"read"} if original["command"].operation == "status" else {"read", "edit"}
        if permission not in allowed or project != original["grant"]["project_id"]:
            raise AccessDenied("Functional polling does not grant execution, publication or unrelated edits")
        current = await self._context(
            await self.polling.collaboration.receipt(original["delivery"]["id"]), remote=current_unit_of_work() is None
        )
        if (
            current["grant"] != original["grant"]
            or current["binding"] != original["binding"]
            or current["members"] != original["members"]
        ):
            raise AccessDenied("The functional allowance changed during the action")

    async def authorize_feedback(self, receipt_id: str, *, remote: bool = True) -> None:
        delivery = await self.polling.collaboration.receipt(receipt_id)
        context = await self._context(delivery, remote=remote)
        retained = delivery.get("functional_authority")
        if retained != dict(grant_digest=context["grant"]["digest"], binding_digest=context["binding"]["digest"]):
            raise AccessDenied("Functional feedback requires the original exact authority")

    async def process(self, receipt_id: str) -> Data:
        collaboration = self.polling.collaboration
        delivery = await collaboration.receipt(receipt_id)
        if delivery["status"] != "received":
            return dict(delivery)
        context = await self._context(delivery, remote=True)
        user, grant = context["user"], context["grant"]
        principal = Principal(
            user.id,
            user.issuer,
            user.subject,
            authentication_method="scm_mapping",
            scm_delegation=dict(
                transport=self.actor_transport,
                repository_id=delivery["repository_id"],
                project_id=grant["project_id"],
                receipt_id=receipt_id,
            ),
        )
        async with self.polling._locked(grant["project_id"], delivery["repository_id"], [user.id.hex]):
            current = await self._context(await collaboration.receipt(receipt_id))
            if current["grant"] != grant or current["binding"] != context["binding"]:
                raise AccessDenied("The functional admission cut changed")
            row = await self.inbox.find_by_id(identifier(receipt_id), lock=LockMode.PESSIMISTIC_WRITE)
            assert row is not None
            if row.status != "received":
                return copy.deepcopy(row.data)
            if context["command"].operation == "turn":
                settings, _ = await self.polling._state(delivery["repository_id"])
                usage = functional_state(settings.data)
                if not reserve_turn(usage, grant["digest"], receipt_id, context["request"].max_model_turns):
                    raise AccessDenied("The model turn was already reserved; reconcile its original outcome")
                settings.data = {**settings.data, USAGE_KEY: usage}
                functional_state(settings.data)
                await self.settings.save(settings)
            await collaboration._write_row(
                row,
                status="needs_reconciliation",
                action_status="writing",
                reason="functional_action_pending",
                functional_authority=dict(grant_digest=grant["digest"], binding_digest=context["binding"]["digest"]),
            )
            admitted = copy.deepcopy(row.data)
        token = CURRENT_PRINCIPAL.set(principal)
        active = self._active.set((principal, context))
        try:
            await self.authorize_snapshot(ProjectAccess.actor_snapshot(principal), grant["project_id"], "read")
            result = await ChannelProducts(self.store, collaboration).execute_functional(
                await self.store.repository(delivery["repository_id"]),
                context["command"],
                await collaboration.settings(delivery["repository_id"]),
                receipt_id,
            )
            await self.authorize_snapshot(ProjectAccess.actor_snapshot(principal), grant["project_id"], "read")
            if context["command"].operation == "turn" and result.get("status") != "completed":
                async with self.store._unit():
                    row = await self.inbox.find_by_id(identifier(receipt_id), lock=LockMode.PESSIMISTIC_WRITE)
                    assert row is not None
                    if row.status != admitted["status"] or row.data != admitted:
                        return copy.deepcopy(row.data)
                    await collaboration._write_row(
                        row,
                        reason="functional_outcome_unconfirmed",
                        action_status="needs_reconciliation",
                        feedback_status="held",
                        action_result={
                            "status": result.get("status"),
                            "conversation_id": context["command"].conversation_id,
                            "turn_id": result.get("turn_id"),
                        },
                    )
                return dict(await collaboration.receipt(receipt_id))
            async with self.polling._locked(grant["project_id"], delivery["repository_id"], [user.id.hex]):
                await self.authorize_snapshot(ProjectAccess.actor_snapshot(principal), grant["project_id"], "read")
                row = await self.inbox.find_by_id(identifier(receipt_id), lock=LockMode.PESSIMISTIC_WRITE)
                assert row is not None
                if row.status != admitted["status"] or row.data != admitted:
                    return copy.deepcopy(row.data)
                if context["command"].operation == "start":
                    settings, _ = await self.polling._state(delivery["repository_id"])
                    usage = functional_state(settings.data)
                    usage["conversations"][result["conversation_id"]] = dict(
                        project_id=grant["project_id"],
                        repository_id=delivery["repository_id"],
                        issue_number=context["request"].issue_number,
                        binding_key=grant["binding_key"],
                        model=context["model"],
                    )
                    settings.data = {**settings.data, USAGE_KEY: usage}
                    functional_state(settings.data)
                    await self.settings.save(settings)
                await collaboration._write_row(
                    row,
                    status="processed",
                    reason=None,
                    action_status="completed",
                    action_result=result,
                    feedback_status="pending",
                    feedback_stage="functional_assistant",
                )
        except BaseException:
            async with self.store._unit():
                row = await self.inbox.find_by_id(identifier(receipt_id), lock=LockMode.PESSIMISTIC_WRITE)
                assert row is not None
                if row.status == admitted["status"] and row.data == admitted:
                    await collaboration._write_row(
                        row, reason="functional_outcome_unknown", action_status="needs_reconciliation"
                    )
            raise
        finally:
            self._active.reset(active)
            CURRENT_PRINCIPAL.reset(token)
        return dict(await collaboration.receipt(receipt_id))
