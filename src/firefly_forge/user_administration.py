# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Least-privileged Keycloak provisioning with durable, nonreplayed remote outcomes."""

from __future__ import annotations

import asyncio
import copy
import json
import os
import re
from typing import Any, Literal
from uuid import UUID, uuid4

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator
from pyfly.container import service
from pyfly.data.relational.sqlalchemy import LockMode
from pyfly.kernel.lifecycle import CONSUMER_PHASE
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert

from firefly_forge.identity import IdentityService, Principal
from firefly_forge.identity_persistence import ForgeIdentityUserEntity
from firefly_forge.product_provenance import content_digest
from firefly_forge.project_access import ProjectAccess
from firefly_forge.secrets import contains_known_credential
from firefly_forge.store import Store, identifier, now
from firefly_forge.user_administration_persistence import (
    ForgeAdminSessionRepository,
    ForgeIdentityAdminEventEntity,
    ForgeIdentityAdminEventRepository,
    ForgeIdentityAdminOperationEntity,
    ForgeIdentityAdminOperationRepository,
)

Data = dict[str, Any]


class KeycloakAdministrationConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    client_id: str = Field(default="", max_length=200)
    client_secret: SecretStr = SecretStr("")

    @classmethod
    def from_environment(cls) -> KeycloakAdministrationConfiguration:
        return cls(
            client_id=os.getenv("FORGE_KEYCLOAK_ADMIN_CLIENT_ID", ""),
            client_secret=SecretStr(os.getenv("FORGE_KEYCLOAK_ADMIN_CLIENT_SECRET", "")),
        )


class UserProvisionInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    request_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    username: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.@-]{0,149}$")
    email: str | None = Field(default=None, max_length=320)
    first_name: str | None = Field(default=None, max_length=100)
    last_name: str | None = Field(default=None, max_length=100)

    @field_validator("email")
    @classmethod
    def email_address(cls, value: str | None) -> str | None:
        if value is not None and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value):
            raise ValueError("Supply a bounded email address")
        return value


class UserInviteInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: str = Field(pattern=r"^[a-f0-9]{32}$")


class UserEnabledInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool


class AdministrationReconciliation(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    outcome: Literal["created", "not_created", "provider_accepted", "not_accepted"]
    subject: str | None = Field(default=None, pattern=r"^[a-f0-9-]{36}$")
    note: str = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def created_subject(self) -> AdministrationReconciliation:
        if self.outcome == "created" and self.subject is None:
            raise ValueError("Created reconciliation requires the exact provider subject")
        if self.subject:
            UUID(self.subject)
        return self


class AdministrationRecovery(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    request_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    owner_stopped: Literal[True]
    note: str = Field(min_length=1, max_length=2000)


class ProviderRejection(Exception):
    def __init__(self, status: int) -> None:
        self.status = status


@service
class UserAdministration:
    phase = CONSUMER_PHASE + 3

    def __init__(
        self,
        store: Store,
        identity: IdentityService,
        operations: ForgeIdentityAdminOperationRepository | None = None,
        events: ForgeIdentityAdminEventRepository | None = None,
        config: KeycloakAdministrationConfiguration | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.store, self.identity = store, identity
        self.operations, self.events = (
            operations or ForgeIdentityAdminOperationRepository(),
            events or ForgeIdentityAdminEventRepository(),
        )
        self.config = config or KeycloakAdministrationConfiguration.from_environment()
        self.client = client or httpx.AsyncClient(timeout=10, follow_redirects=False, trust_env=False)
        self.owns_client = client is None

    async def stop(self) -> None:
        if self.owns_client:
            await self.client.aclose()

    async def start(self) -> None:
        if self.store.runtime_roles and self.store.runtime_roles.role != "combined":
            return
        await self.recover()

    async def recover(self) -> int:
        await self.store.assert_ownership()
        count = 0
        while True:
            async with self.store._unit():
                rows = await self.operations.unfinished()
                if not rows:
                    return count
                for row in rows:
                    row.status = "needs_reconciliation"
                    row.data = {
                        **row.data,
                        "status": "needs_reconciliation",
                        "updated_at": now(),
                        "detail": "Execution owner restarted; reconcile the provider outcome before further action",
                    }
                    actor = Principal(
                        identifier(row.data["actor_id"]), "internal:recovery", "historical_initiator", kind="service"
                    )
                    await self._event(
                        "remote_interrupted",
                        actor,
                        {
                            "cause": "execution_owner_restart",
                            "historical_initiator_id": row.data["actor_id"],
                            "remote_replayed": False,
                        },
                        operation=row,
                    )
                    count += 1

    def redaction_settings(self) -> dict[str, str]:
        return {"FORGE_KEYCLOAK_ADMIN_CLIENT_SECRET": self.config.client_secret.get_secret_value()}

    def catalog(self) -> Data:
        return {
            "provider": "keycloak",
            "configured": bool(
                self.config.client_id
                and self.config.client_secret.get_secret_value()
                and self.identity.config.mode == "oidc"
            ),
            "client_id": self.config.client_id,
            "secret_configured": bool(self.config.client_secret.get_secret_value()),
            "issuer": self.identity.config.issuer,
            "provisioning_verified": False,
            "mail_delivery_verified": False,
            "required_permissions": ["realm-management:view-users", "realm-management:manage-users"],
            "local_passwords_supported": False,
        }

    async def _actor(self, actor: Principal) -> None:
        if not await ProjectAccess(self.store).current_administrator(actor):
            raise PermissionError("Identity administration requires a verified factory administrator")

    def _guard(self, value: Any) -> None:
        self.store.reject_credentials(value)
        if contains_known_credential(value, {**self.identity.redaction_settings(), **self.redaction_settings()}):
            raise ValueError("Remove configured credentials from identity metadata")

    def _frozen_config(self) -> Data:
        if not self.catalog()["configured"]:
            raise ValueError("Keycloak administration is not configured")
        issuer = self.identity.config.issuer
        base, separator, realm = issuer.rpartition("/realms/")
        if not separator or not re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9_.-]{0,99}", realm):
            raise ValueError("Configure an exact Keycloak realm issuer")
        return {
            "issuer": issuer,
            "admin_base": base + "/admin/realms/" + realm,
            "client_id": self.config.client_id,
            "client_secret": self.config.client_secret.get_secret_value(),
        }

    async def _event(
        self,
        kind: str,
        actor: Principal,
        payload: Data,
        *,
        operation: ForgeIdentityAdminOperationEntity | None = None,
        target: UUID | None = None,
    ) -> None:
        data = {"kind": kind, "actor_id": actor.id.hex, "at": now(), "payload": payload}
        if operation:
            data.update(
                sequence=operation.data.get("journal_sequence", 0) + 1,
                previous=operation.data.get("journal_head", "0" * 64),
            )
        data["digest"] = content_digest(data)
        await self.events.save(
            ForgeIdentityAdminEventEntity(
                operation_id=operation.id if operation else None, actor_id=actor.id, target_user_id=target, data=data
            )
        )
        if operation:
            operation.data = {**operation.data, "journal_sequence": data["sequence"], "journal_head": data["digest"]}
            await self.operations.save(operation)

    async def set_enabled(self, user_id: str, enabled: bool, actor: Principal) -> Data:
        await self._actor(actor)
        target = identifier(user_id)
        if not enabled and target == actor.id:
            raise ValueError("You cannot suspend yourself")
        async with self.store._unit():
            user = await self.identity.users.find_by_id(target, lock=LockMode.PESSIMISTIC_WRITE)
            if user is None:
                raise KeyError("Registered identity not found")
            if user.issuer == "local:bootstrap":
                raise ValueError("The explicit local bootstrap identity cannot be modified")
            previous, user.enabled = user.enabled, enabled
            revoked = (
                await ForgeAdminSessionRepository().revoke_for_user(user.id, self.identity._encrypt({}))
                if not enabled
                else 0
            )
            await self.identity.users.save(user)
            result = {
                "user_id": user.id.hex,
                "enabled": enabled,
                "previous_enabled": previous,
                "revoked_sessions": revoked,
            }
            await self._event("identity_enabled" if enabled else "identity_suspended", actor, result, target=user.id)
            return result

    async def _claim(
        self, kind: str, request: Data, actor: Principal
    ) -> tuple[ForgeIdentityAdminOperationEntity, bool]:
        self._guard(request)
        request_digest = content_digest({"kind": kind, "request": request})
        async with self.store._unit() as session:
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": self.store.schema + ":identity-administration:" + request["request_id"]},
            )
            row = await self.operations.request(request["request_id"])
            if row:
                if row.data["request_digest"] != request_digest:
                    raise ValueError("Identity administration replay conflicts with the original request")
                return row, False
            config = self._frozen_config()
            row = ForgeIdentityAdminOperationEntity(
                id=uuid4(),
                request_id=request["request_id"],
                kind=kind,
                status="executing",
                encrypted_config=self.identity._encrypt(config),
                data={
                    "id": uuid4().hex,
                    "kind": kind,
                    "status": "executing",
                    "request": request,
                    "request_digest": request_digest,
                    "actor_id": actor.id.hex,
                    "created_at": now(),
                    "project_memberships_granted": False,
                    "invitation_status": "pending" if kind == "invite" else "not_requested",
                    "mail_delivery_verified": False,
                },
            )
            row.data = {**row.data, "id": row.id.hex}
            await self.operations.save(row)
            await self._event("remote_intent", actor, {"kind": kind, "request_digest": request_digest}, operation=row)
            return row, True

    async def _http(
        self,
        method: str,
        url: str,
        *,
        token: str | None = None,
        form: Data | None = None,
        body: Any = None,
        expected: int = 200,
    ) -> tuple[Data, str | None]:
        headers = {"Accept-Encoding": "identity"}
        if token:
            headers["Authorization"] = "Bearer " + token
        async with self.client.stream(
            method, url, headers=headers, data=form, json=body, timeout=10, follow_redirects=False
        ) as response:
            if response.status_code != expected:
                raise ProviderRejection(response.status_code)
            if response.headers.get("content-encoding", "identity") != "identity":
                raise ValueError("Compressed identity provider responses are not accepted")
            raw = bytearray()
            async for chunk in response.aiter_bytes():
                raw.extend(chunk)
                if len(raw) > 100000:
                    raise ValueError("Identity provider response exceeds its limit")
            document = json.loads(raw) if raw else {}
            if not isinstance(document, dict):
                raise ValueError("Identity provider response is invalid")
            return document, response.headers.get("location")

    async def _token(self, config: Data) -> str:
        document, _ = await self._http(
            "POST",
            config["issuer"] + "/protocol/openid-connect/token",
            form={
                "grant_type": "client_credentials",
                "client_id": config["client_id"],
                "client_secret": config["client_secret"],
            },
        )
        token = document.get("access_token")
        if not isinstance(token, str):
            raise ValueError("Administration access token is unavailable")
        claims = await self.identity._claims(token, "realm-management")
        roles = claims.get("resource_access", {}).get("realm-management", {}).get("roles", [])
        if (
            claims.get("azp") != config["client_id"]
            or claims.get("typ") != "Bearer"
            or not isinstance(roles, list)
            or not {"view-users", "manage-users"}.issubset(roles)
            or set(roles) - {"view-users", "manage-users", "query-users", "query-groups"}
        ):
            raise ValueError("Configure a dedicated least-privileged administration service client")
        return token

    async def _local_user(self, subject: str, remote: Data) -> UUID:
        self._guard({key: remote.get(key) for key in ("username", "email", "firstName", "lastName")})
        full_name = " ".join(
            value for key in ("firstName", "lastName") if isinstance(value := remote.get(key), str)
        ).strip()[:200]
        username = remote.get("username")
        name = full_name or (username[:200] if isinstance(username, str) else None)
        contact = remote.get("email")
        if contact is not None:
            if not isinstance(contact, str) or len(contact) > 320:
                raise ValueError("Provider contact email must be a bounded email address")
            contact = UserProvisionInput.email_address(contact)
        email = contact if remote.get("emailVerified") is True else None
        async with self.store._unit() as session:
            await session.execute(
                insert(ForgeIdentityUserEntity)
                .values(
                    id=uuid4(),
                    issuer=self.identity.config.issuer,
                    subject=subject,
                    display_name=name,
                    email=email,
                    contact_email=contact,
                    enabled=True,
                )
                .on_conflict_do_nothing(index_elements=["issuer", "subject"])
            )
            user = await self.identity.users.subject(self.identity.config.issuer, subject)
            assert user is not None
            user.display_name = name or user.display_name
            user.contact_email = contact
            if email is not None:
                user.email = email
            await self.identity.users.save(user)
            return user.id

    def _matches(self, remote: Data, request: Data, request_digest: str, subject: str) -> None:
        if (
            remote.get("id") != subject
            or remote.get("username") != request["username"]
            or request.get("email")
            and remote.get("email") != request["email"]
        ):
            raise ValueError("Provider account does not match the requested identity")
        attributes = remote.get("attributes", {})
        if (
            not isinstance(attributes, dict)
            or attributes.get("forge_request_id") != [request["request_id"]]
            or attributes.get("forge_request_digest") != [request_digest]
        ):
            raise ValueError("Provider request marker does not match the original provisioning intent")

    async def _finish(self, operation_id: UUID, actor: Principal, status: str, evidence: Data) -> Data:
        async with self.store._unit():
            row = await self.operations.find_by_id(operation_id, lock=LockMode.PESSIMISTIC_WRITE)
            assert row is not None
            if row.status != "executing":
                return copy.deepcopy(row.data)
            row.status = status
            row.data = {**row.data, **evidence, "status": status, "updated_at": now()}
            await self._event(
                "remote_receipt" if status == "completed" else "remote_error",
                actor,
                {"status": status, **evidence},
                operation=row,
                target=identifier(evidence["user_id"]) if evidence.get("user_id") else None,
            )
            return copy.deepcopy(row.data)

    async def provision(self, request: UserProvisionInput, actor: Principal) -> Data:
        await self._actor(actor)
        row, execute = await self._claim("provision", request.model_dump(), actor)
        if not execute:
            return copy.deepcopy(row.data)
        effect_started = False
        created_admitted = False
        try:
            config = self.identity._decrypt(row.encrypted_config)
            token = await self._token(config)
            payload = {
                "username": request.username,
                "enabled": True,
                "emailVerified": False,
                "requiredActions": ["UPDATE_PASSWORD"] + (["VERIFY_EMAIL"] if request.email else []),
                "attributes": {
                    "forge_request_id": [request.request_id],
                    "forge_request_digest": [row.data["request_digest"]],
                },
            }
            for field, name in (("email", "email"), ("first_name", "firstName"), ("last_name", "lastName")):
                value = getattr(request, field)
                if value is not None:
                    payload[name] = value
            await self._actor(actor)
            effect_started = True
            _, location = await self._http(
                "POST", config["admin_base"] + "/users", token=token, body=payload, expected=201
            )
            created_admitted = True
            prefix = config["admin_base"] + "/users/"
            if not location or not location.startswith(prefix):
                raise ValueError("Provider returned an unexpected account location")
            subject = str(UUID(location[len(prefix) :]))
            if location != prefix + subject:
                raise ValueError("Provider returned a noncanonical account location")
            remote, _ = await self._http("GET", prefix + subject, token=token)
            self._matches(remote, request.model_dump(), row.data["request_digest"], subject)
            user_id = await self._local_user(subject, remote)
            return await self._finish(
                row.id,
                actor,
                "completed",
                {
                    "subject": subject,
                    "user_id": user_id.hex,
                    "provider_created": True,
                    "provider_outcome_evidence": "http_201_and_exact_provider_request_marker",
                    "project_memberships_granted": False,
                },
            )
        except ProviderRejection as exc:
            return await self._finish(
                row.id,
                actor,
                "needs_reconciliation" if created_admitted or effect_started and exc.status >= 500 else "failed",
                {"detail": f"Identity provider rejected the request (HTTP {exc.status}); no body retained"},
            )
        except asyncio.CancelledError:
            await asyncio.shield(
                self._finish(
                    row.id,
                    actor,
                    "needs_reconciliation",
                    {"detail": "Interrupted provider outcome; reconcile explicitly"},
                )
            )
            raise
        except Exception:
            return await self._finish(
                row.id,
                actor,
                "needs_reconciliation" if effect_started else "failed",
                {"detail": "Provider outcome unavailable; no automatic repeat is permitted"},
            )

    async def invite(self, user_id: str, request: UserInviteInput, actor: Principal) -> Data:
        await self._actor(actor)
        async with self.store._unit():
            user = await self.identity.users.find_by_id(identifier(user_id))
            if user is None or not user.enabled or user.issuer != self.identity.config.issuer:
                raise ValueError("Invite requires an enabled identity in the configured realm")
            subject = str(UUID(user.subject))
        row, execute = await self._claim(
            "invite", {**request.model_dump(), "user_id": user_id, "subject": subject}, actor
        )
        if not execute:
            return copy.deepcopy(row.data)
        effect_started = False
        try:
            config = self.identity._decrypt(row.encrypted_config)
            token = await self._token(config)
            remote, _ = await self._http("GET", config["admin_base"] + "/users/" + subject, token=token)
            if remote.get("id") != subject or not remote.get("email"):
                raise ValueError("Provider account needs an email address before invitation")
            await self._actor(actor)
            effect_started = True
            await self._http(
                "PUT",
                config["admin_base"] + "/users/" + subject + "/execute-actions-email",
                token=token,
                body=["UPDATE_PASSWORD", "VERIFY_EMAIL"],
                expected=204,
            )
            return await self._finish(
                row.id,
                actor,
                "completed",
                {"user_id": user_id, "invitation_status": "provider_accepted", "mail_delivery_verified": False},
            )
        except ProviderRejection as exc:
            return await self._finish(
                row.id,
                actor,
                "failed",
                {"detail": f"Invitation rejected (HTTP {exc.status}); SMTP delivery is not verified"},
            )
        except asyncio.CancelledError:
            await asyncio.shield(
                self._finish(
                    row.id, actor, "needs_reconciliation", {"detail": "Interrupted invitation; reconcile explicitly"}
                )
            )
            raise
        except Exception:
            return await self._finish(
                row.id,
                actor,
                "needs_reconciliation" if effect_started else "failed",
                {"detail": "Invitation outcome unavailable; no automatic repeat is permitted"},
            )

    async def recover_operation(self, operation_id: str, request: AdministrationRecovery, actor: Principal) -> Data:
        await self._actor(actor)
        self._guard(request.model_dump())
        request_digest = content_digest(request.model_dump())
        async with self.store._unit():
            row = await self.operations.find_by_id(identifier(operation_id), lock=LockMode.PESSIMISTIC_WRITE)
            if row is None:
                raise KeyError("Administration operation not found")
            previous = row.data.get("owner_stopped_confirmation")
            if previous:
                if previous["request_digest"] != request_digest:
                    raise ValueError("This operation already has another recovery confirmation")
                return copy.deepcopy(row.data)
            if row.status != "executing":
                raise ValueError("Only an executing operation requires owner-stopped confirmation")
            proof = {"request_digest": request_digest, "actor_id": actor.id.hex, **request.model_dump(), "at": now()}
            row.status = "needs_reconciliation"
            row.data = {
                **row.data,
                "status": row.status,
                "owner_stopped_confirmation": proof,
                "detail": "Operator confirmed the original owner stopped; provider outcome remains unknown",
                "updated_at": now(),
            }
            await self._event("owner_stopped_confirmation", actor, proof, operation=row)
            return copy.deepcopy(row.data)

    async def reconcile(self, operation_id: str, request: AdministrationReconciliation, actor: Principal) -> Data:
        await self._actor(actor)
        self._guard(request.model_dump())
        async with self.store._unit():
            row = await self.operations.find_by_id(identifier(operation_id))
            if row is None:
                raise KeyError("Administration operation not found")
            if row.status == "executing":
                raise ValueError("An in-flight operation cannot be reconciled")
            if row.status != "needs_reconciliation":
                raise ValueError("Only uncertain operations can be reconciled")
            data, kind = copy.deepcopy(row.data), row.kind
            config = self.identity._decrypt(row.encrypted_config)
        evidence: Data = {
            "reconciliation": {"outcome": request.outcome, "note": request.note, "actor_id": actor.id.hex},
            "mail_delivery_verified": False,
        }
        if kind == "provision" and request.outcome == "created":
            token = await self._token(config)
            remote, _ = await self._http("GET", config["admin_base"] + "/users/" + str(request.subject), token=token)
            self._matches(remote, data["request"], data["request_digest"], str(request.subject))
            evidence.update(
                subject=request.subject,
                user_id=(await self._local_user(str(request.subject), remote)).hex,
                provider_created=True,
                provider_outcome_evidence="exact_provider_request_marker",
            )
            status = "completed"
        elif kind == "provision" and request.outcome == "not_created":
            status = "closed_not_created"
            evidence["provider_created"] = False
            evidence["provider_outcome_evidence"] = "operator_attestation"
        elif kind == "invite" and request.outcome in {"provider_accepted", "not_accepted"}:
            status = "completed" if request.outcome == "provider_accepted" else "closed_not_accepted"
            evidence["invitation_status"] = (
                "operator_attested_accepted" if status == "completed" else "operator_attested_not_accepted"
            )
            evidence["provider_outcome_evidence"] = "operator_attestation"
        else:
            raise ValueError("Reconciliation outcome does not match this operation")
        async with self.store._unit():
            current = await self.operations.find_by_id(row.id, lock=LockMode.PESSIMISTIC_WRITE)
            if current is None or current.status != "needs_reconciliation":
                raise ValueError("Administration operation changed during reconciliation")
            current.status = status
            current.data = {**current.data, **evidence, "status": status, "updated_at": now()}
            await self._event(
                "remote_reconciled",
                actor,
                evidence,
                operation=current,
                target=identifier(evidence["user_id"]) if evidence.get("user_id") else None,
            )
            return copy.deepcopy(current.data)

    async def receipts(self) -> list[Data]:
        async with self.store._unit():
            results = []
            for row in await self.operations.latest():
                previous, valid = "0" * 64, True
                records = await self.events.for_operation(row.id)
                for index, event in enumerate(records, 1):
                    entry = event.data
                    valid = valid and entry.get("sequence") == index and entry.get("previous") == previous
                    valid = valid and entry.get("digest") == content_digest(
                        {key: value for key, value in entry.items() if key != "digest"}
                    )
                    previous = entry.get("digest", "")
                valid = (
                    valid
                    and previous == row.data.get("journal_head")
                    and len(records) == row.data.get("journal_sequence")
                )
                results.append(
                    {
                        **copy.deepcopy(row.data),
                        "journal_valid": valid,
                        "journal": [copy.deepcopy(event.data) for event in records],
                    }
                )
            return results
