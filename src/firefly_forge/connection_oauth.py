# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""PostgreSQL-authoritative connection OAuth, separate from operator login and SCM."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import datetime, timedelta
from typing import Any, cast
from urllib.parse import urlencode, urlsplit
from uuid import UUID, uuid4

from cryptography.fernet import Fernet, InvalidToken
from pyfly.container import service
from pyfly.kernel.lifecycle import CONSUMER_PHASE
from pyfly.oauth2 import OAuth2Client, OAuth2ClientError, OAuth2Endpoints, OAuth2Tokens, generate_pkce
from sqlalchemy import func, literal, select, text, union_all
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession

from firefly_forge.connection_oauth_models import (
    OAuthChallenge,
    OAuthConnectionInput,
    OAuthConnectionSummary,
    OAuthDisposition,
    OAuthEffectReceipt,
    OAuthEffectSummary,
    OAuthPolicyBinding,
    OAuthProvider,
    OAuthSupersedeInput,
)
from firefly_forge.connection_oauth_persistence import (
    OAuthAttemptEntity,
    OAuthConnectionEntity,
    OAuthConnectionRepository,
    OAuthCredentialEvidenceEntity,
    OAuthEffectEntity,
    OAuthEventEntity,
    OAuthGrantEntity,
    OAuthRegistrationEntity,
)
from firefly_forge.identity import Principal
from firefly_forge.journal import digest as packet_digest
from firefly_forge.oauth_authority import OAuthAccessDenied as OAuthAccessDenied
from firefly_forge.oauth_authority import require_oauth_administrator
from firefly_forge.oauth_credential_fingerprints import MAX_HISTORY, OAuthCredentialMatcher
from firefly_forge.project_persistence import ForgeProjectEntity
from firefly_forge.secrets import contains_known_credential
from firefly_forge.store import Store

Data = dict[str, Any]


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


@service
class ConnectionOAuth:
    phase = CONSUMER_PHASE - 1

    def __init__(
        self,
        store: Store,
        *,
        encryption_key: str,
        browser_origin: str,
        catalog: Mapping[str, OAuthProvider] | None = None,
        records: OAuthConnectionRepository | None = None,
        allow_loopback_http: bool = False,
        policy_lookup: Callable[[AsyncSession, str], Awaitable[OAuthPolicyBinding | None]] | None = None,
        policy_directory: Callable[[Principal], Awaitable[Data]] | None = None,
    ) -> None:
        self.store, self.cipher = store, Fernet(encryption_key.encode())
        self._secrets = {"FORGE_CONNECTION_OAUTH_SECRET_ENCRYPTION_KEY": encryption_key}
        self.matcher = OAuthCredentialMatcher(encryption_key, store.schema)
        self.store.vault_credential_admission = self.admit_credentials
        self.records = records or OAuthConnectionRepository()
        self.browser_origin = browser_origin
        self.catalog = dict(catalog or {})
        self.policy_lookup, self.policy_directory = policy_lookup, policy_directory
        self.loopback = allow_loopback_http is True
        if browser_origin:
            self._endpoint(browser_origin, origin=True)
        for name, policy in self.catalog.items():
            if name != policy.id or not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", name):
                raise ValueError("Invalid admitted OAuth provider")
            for endpoint in (policy.issuer, policy.authorization_endpoint, policy.token_endpoint, policy.resource):
                self._endpoint(endpoint)
            if not policy.scopes or len(set(policy.scopes)) != len(policy.scopes) or len(policy.scopes) > 64:
                raise ValueError("Admit bounded unique OAuth scopes")
            if any(not re.fullmatch(r"[\x21\x23-\x5b\x5d-\x7e]{1,256}", scope) for scope in policy.scopes):
                raise ValueError("Invalid admitted OAuth scope")
            if len(" ".join(policy.scopes)) > 4096:
                raise ValueError("Admitted OAuth scope packet exceeds its bound")

    def _remember(self, payload: Data) -> None:
        for value in payload.values():
            if isinstance(value, str) and value:
                self._secrets["FORGE_CONNECTION_OAUTH_SECRET_" + digest(value)] = value

    def redaction_settings(self) -> dict[str, str]:
        return dict(self._secrets)

    async def start(self) -> None:
        async with self.store._unit() as session:
            await self._vault_lock(session)
            registrations = list(await session.scalars(select(OAuthRegistrationEntity).limit(1025)))
            grants = list(await session.scalars(select(OAuthGrantEntity).limit(1025)))
            if len(registrations) > 1024 or len(grants) > 1024:
                raise RuntimeError("OAuth credential history requires a reviewed retention checkpoint")
            for registration in registrations:
                payload = self._open(
                    registration.encrypted_secret,
                    self._client_context(registration.connection_id, registration.revision, registration.data),
                )
                self._remember(payload)
                await self._credential_evidence(
                    session, registration.connection_id, "registration", registration.id, registration.revision, payload
                )
            for grant in grants:
                row = await session.get(OAuthConnectionEntity, grant.connection_id)
                if row is None:
                    raise ValueError("OAuth connection history is unavailable")
                payload = self._open(grant.encrypted_data, self._grant_context(row, grant))
                self._remember(payload)
                if payload:
                    await self._credential_evidence(session, row.id, "grant", grant.id, grant.revision, payload)

    async def _vault_lock(self, session: AsyncSession) -> None:
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": self.store.schema + ":connection-oauth:credential-admission"},
        )

    async def _history(self, session: AsyncSession) -> list[OAuthCredentialEvidenceEntity]:
        rows = list(await session.scalars(select(OAuthCredentialEvidenceEntity).limit(MAX_HISTORY + 1)))
        if len(rows) > MAX_HISTORY:
            raise ValueError("OAuth credential history exceeds its bound; review retention before admission")
        for row in rows:
            expected = {
                "row_id": row.id.hex,
                "connection_id": row.connection_id.hex,
                "kind": row.kind,
                "source_id": row.source_id.hex,
                "revision": row.revision,
            }
            if not isinstance(row.data, dict) or any(row.data.get(key) != value for key, value in expected.items()):
                raise ValueError("OAuth credential history source context is invalid")
        self.matcher.contains("", [row.data for row in rows])
        return rows

    async def _credential_evidence(
        self, session: AsyncSession, connection_id: UUID, kind: str, source_id: UUID, revision: int, payload: Data
    ) -> None:
        await self._vault_lock(session)
        rows = await self._history(session)
        if any(row.kind == kind and row.source_id == source_id and row.revision == revision for row in rows):
            return
        if len(rows) == MAX_HISTORY:
            raise ValueError("OAuth credential history exceeds its bound; review retention before publication")
        evidence_id = uuid4()
        values = [value for value in payload.values() if isinstance(value, str) and value]
        session.add(
            OAuthCredentialEvidenceEntity(
                id=evidence_id,
                connection_id=connection_id,
                kind=kind,
                source_id=source_id,
                revision=revision,
                data=self.matcher.packet(evidence_id.hex, kind, source_id.hex, revision, values, connection_id.hex),
            )
        )
        await session.flush()

    async def admit_credentials(self, value: Any) -> None:
        # One statement sees evidence and source coverage at the same committed
        # snapshot, including inside an ambient unit. It acquires no advisory lock.
        evidence = OAuthCredentialEvidenceEntity
        registration, grant = OAuthRegistrationEntity, OAuthGrantEntity
        snapshot = union_all(
            select(
                literal("evidence").label("record_kind"),
                evidence.id.label("row_id"),
                evidence.connection_id.label("connection_id"),
                evidence.kind.label("source_kind"),
                evidence.source_id.label("source_id"),
                evidence.revision.label("revision"),
                evidence.data.label("data"),
            ).limit(MAX_HISTORY + 1),
            select(
                literal("registration"),
                registration.id,
                registration.connection_id,
                literal("registration"),
                registration.id,
                registration.revision,
                literal(None, type_=JSONB),
            ).limit(MAX_HISTORY + 1),
            select(
                literal("grant"),
                grant.id,
                grant.connection_id,
                literal("grant"),
                grant.id,
                grant.revision,
                literal(None, type_=JSONB),
            ).limit(MAX_HISTORY + 1),
        )
        async with self.store._unit(autoflush=False) as session:
            records = list((await session.execute(snapshot)).mappings())
            groups = {
                kind: [row for row in records if row["record_kind"] == kind]
                for kind in ("evidence", "registration", "grant")
            }
            if any(len(rows) > MAX_HISTORY for rows in groups.values()):
                raise ValueError("OAuth credential history exceeds its bound")
            packets = []
            for row in groups["evidence"]:
                expected = {
                    "row_id": row["row_id"].hex,
                    "connection_id": row["connection_id"].hex,
                    "kind": row["source_kind"],
                    "source_id": row["source_id"].hex,
                    "revision": row["revision"],
                }
                if not isinstance(row["data"], dict) or any(
                    row["data"].get(key) != item for key, item in expected.items()
                ):
                    raise ValueError("OAuth credential history source context is invalid")
                packets.append(row["data"])
            origins = {(row["source_kind"], row["source_id"], row["revision"]) for row in groups["evidence"]}
            if any(("registration", row["row_id"], row["revision"]) not in origins for row in groups["registration"]):
                raise ValueError("OAuth credential history is unavailable; initialize admission before continuing")
            for row in groups["grant"]:
                history = {
                    revision for kind, source, revision in origins if kind == "grant" and source == row["row_id"]
                }
                if not 1 <= row["revision"] <= MAX_HISTORY or history != set(range(1, row["revision"] + 1)):
                    raise ValueError("Retired OAuth credential history is incomplete; reviewed recovery is required")
            if self.matcher.contains(value, packets) or contains_known_credential(value, self._secrets):
                raise ValueError("Do not place configured credentials in public instructions, messages or tool data")

    async def stop(self) -> None:
        self._secrets.clear()

    def _endpoint(self, value: str, *, origin: bool = False) -> None:
        parsed = urlsplit(value)
        try:
            _ = parsed.port
            if (
                len(value.encode()) > 2048
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or parsed.query
                or parsed.fragment
                or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value)
                or (origin and parsed.path)
                or (
                    parsed.scheme != "https"
                    and not (self.loopback and parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "::1"})
                )
            ):
                raise ValueError
        except ValueError:
            raise ValueError("Connection endpoints require admitted HTTPS origins") from None

    def _context(self, purpose: str, connection_id: UUID, revision: int, extra: str = "") -> Data:
        return {
            "version": 1,
            "schema": self.store.schema,
            "purpose": purpose,
            "connection_id": connection_id.hex,
            "revision": revision,
            "extra": extra,
        }

    def _seal(self, context: Data, payload: Data) -> str:
        raw = json.dumps({"context": context, "payload": payload}, separators=(",", ":"), sort_keys=True).encode()
        if len(raw) > 262144:
            raise ValueError("OAuth secret packet exceeds its bound")
        return self.cipher.encrypt(raw).decode()

    def _grant_context(self, row: OAuthConnectionEntity, grant: OAuthGrantEntity) -> Data:
        context = self._context("grant", row.id, grant.registration_revision, grant.family)
        return {
            **context,
            "grant_revision": grant.revision,
            "actor_id": grant.actor_id.hex,
            "metadata_digest": digest(json.dumps(grant.data, sort_keys=True, separators=(",", ":"))),
            "expires_at": grant.expires_at.isoformat(),
        }

    def _client_context(self, connection_id: UUID, revision: int, data: Data) -> Data:
        return self._context(
            "client", connection_id, revision, digest(json.dumps(data, sort_keys=True, separators=(",", ":")))
        )

    def providers(self) -> list[Data]:
        requirements = {
            "github": "Existing GitHub App connections work. This OAuth client contract still needs admission.",
            "figma": "Figma must approve the client; its exact OAuth registration and scoped adapter are required.",
            "sentry": "An admitted issuer, OAuth registration and project-scoped adapter are required.",
            "grafana": "An approved managed HTTPS endpoint and scoped OAuth adapter are required.",
            "playwright": "Use the native browser runner; no external OAuth connection is admitted.",
        }
        catalog = {} if self.policy_lookup is not None else self.catalog
        return [
            {
                "provider": name,
                "status": "registration_available"
                if name in catalog and not catalog[name].prerequisite and self.browser_origin
                else "prerequisite_required",
                "reason": requirements.get(name, "This provider uses an explicitly admitted control-plane policy."),
                "agent_ready": False,
            }
            for name in dict.fromkeys((*requirements, *catalog))
        ]

    def _open(self, encoded: str, context: Data) -> Data:
        try:
            value = json.loads(self.cipher.decrypt(encoded.encode()))
            if value["context"] != context or not isinstance(value["payload"], dict):
                raise ValueError
            return cast(Data, value["payload"])
        except (InvalidToken, ValueError, KeyError, TypeError):
            raise ValueError("OAuth ciphertext context is invalid") from None

    @asynccontextmanager
    async def _locked(self, name: str) -> AsyncIterator[AsyncSession]:
        if name == "providers" or not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", name):
            raise ValueError("Invalid connection name")
        async with self.store._unit() as session:
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": self.store.schema + ":connection-oauth:" + name},
            )
            yield session

    async def _time(self, session: AsyncSession) -> datetime:
        return cast(datetime, await session.scalar(select(func.clock_timestamp())))

    async def _authority(self, session: AsyncSession, actor: Principal) -> None:
        await require_oauth_administrator(session, actor)

    async def _admitted_policy(self, session: AsyncSession, name: str) -> OAuthPolicyBinding | None:
        if self.policy_lookup is not None:
            return await self.policy_lookup(session, name)
        policy = self.catalog.get(name)
        return OAuthPolicyBinding(policy, 0, packet_digest(asdict(policy))) if policy else None

    async def provider_directory(self, actor: Principal) -> list[Data]:
        result = self.providers()
        if self.policy_directory is not None:
            state = await self.policy_directory(actor)
            rows = state["policies"]
            if state["local_development_available"] or rows:
                active = bool(state["local_development_available"] and rows and rows[0]["enabled"])
                result.append(
                    {
                        "provider": "local-repository-tools",
                        "status": "registration_available" if active else "prerequisite_required",
                        "reason": (
                            "Connect an explicitly configured local test service; "
                            "project tools require separate review."
                        ),
                        "agent_ready": False,
                    }
                )
        await self.store.admit_credentials(result)
        return result

    async def _stored_registration(self, session: AsyncSession, row: OAuthConnectionEntity) -> OAuthRegistrationEntity:
        result = await session.scalar(
            select(OAuthRegistrationEntity).where(
                OAuthRegistrationEntity.connection_id == row.id, OAuthRegistrationEntity.revision == row.revision
            )
        )
        if result is None:
            raise ValueError("Connection registration is unavailable")
        if (
            result.data.get("project_id")
            and await session.get(ForgeProjectEntity, UUID(result.data["project_id"])) is None
        ):
            raise KeyError("Connection project is unavailable")
        self._open(result.encrypted_secret, self._client_context(row.id, row.revision, result.data))
        return result

    def _policy_matches(self, registration: OAuthRegistrationEntity, binding: OAuthPolicyBinding | None) -> bool:
        return (
            binding is not None
            and asdict(binding.provider)
            == {**registration.data["policy"], "scopes": tuple(registration.data["policy"]["scopes"])}
            and (
                self.policy_lookup is None
                or registration.data.get("policy_revision") == binding.revision
                and registration.data.get("policy_digest") == binding.digest
            )
        )

    async def _registration(self, session: AsyncSession, row: OAuthConnectionEntity) -> OAuthRegistrationEntity:
        result = await self._stored_registration(session, row)
        if not self._policy_matches(result, await self._admitted_policy(session, result.data["provider"])):
            raise ValueError("Admitted provider policy changed; configure a new revision")
        return result

    async def _required(self, name: str) -> OAuthConnectionEntity:
        row = await self.records.named(name)
        if row is None:
            raise KeyError("Connection not found")
        return row

    async def _event(
        self, session: AsyncSession, row: OAuthConnectionEntity, kind: str, actor: Principal, **data: Any
    ) -> None:
        session.add(
            OAuthEventEntity(
                connection_id=row.id,
                data={
                    "kind": kind,
                    "actor_id": actor.id.hex,
                    "revision": row.revision,
                    "at": (await self._time(session)).isoformat(),
                    **data,
                },
            )
        )

    async def _effect_packets(
        self, session: AsyncSession, row: OAuthConnectionEntity, *, ignore: UUID | None = None
    ) -> list[Data]:
        effects = list(
            await session.scalars(
                select(OAuthEffectEntity)
                .where(
                    OAuthEffectEntity.connection_id == row.id,
                    OAuthEffectEntity.state.in_(("executing", "unknown")),
                )
                .order_by(OAuthEffectEntity.created_at, OAuthEffectEntity.id)
                .limit(101)
                .with_for_update()
            )
        )
        if len(effects) > 100:
            raise ValueError("Connection uncertainty exceeds its reviewed history bound")
        at = await self._time(session)
        return [
            OAuthEffectSummary.model_validate(
                {
                    "id": effect.id.hex,
                    "kind": effect.kind,
                    "state": "unknown" if effect.state == "unknown" or effect.lease_expires_at <= at else "executing",
                    "registration_revision": effect.data["registration_revision"],
                    "lease_expired": effect.lease_expires_at <= at,
                }
            ).model_dump()
            for effect in effects
            if effect.id != ignore
        ]

    @staticmethod
    def _effect_block(row: OAuthConnectionEntity, packets: list[Data]) -> str | None:
        if any(packet["state"] == "executing" for packet in packets):
            return "effect_executing"
        if any(packet["registration_revision"] == row.revision for packet in packets):
            return "effect_unknown_explicit_supersession_required"
        return None

    @staticmethod
    def _summary_packet(row: OAuthConnectionEntity, data: Data, secret: str | None, effects: list[Data]) -> Data:
        pending = [
            packet
            for packet in effects
            if packet["registration_revision"] == row.revision or packet["state"] == "executing"
        ]
        archived = [packet for packet in effects if packet not in pending]
        blocked = ConnectionOAuth._effect_block(row, effects)
        state = "executing" if blocked == "effect_executing" else "unknown" if blocked else "none"
        result: Data = {
            "name": row.name,
            "revision": row.revision,
            "provider": data["provider"],
            "client_id": data["client_id"],
            "project_id": data.get("project_id"),
            "status": row.status,
            "resource": data["policy"]["resource"],
            "scopes": data["policy"]["scopes"],
            "agent_ready": False,
            "remote_revocation_supported": False,
            "authentication": data["authentication"],
            "public_client": not bool(secret),
            "secret_configured": bool(secret),
            "callback_uri": data["callback"],
            "refresh_available": False,
            "grant_expires_at": None,
            "effect_state": state,
            "unresolved_effects": pending,
            "archived_unknown_effects": archived,
            "reconciliation_required": bool(effects),
            "allowed_actions": {
                "configure": not blocked,
                "authorize": not blocked and row.status != "disconnected",
                "refresh": False,
                "disconnect": row.status != "disconnected",
                "supersede": state == "unknown",
            },
        }
        return result

    async def _summary(
        self, session: AsyncSession, row: OAuthConnectionEntity, actor: Principal, *, completing: UUID | None = None
    ) -> Data:
        # Diagnostics and local revocation remain available after policy withdrawal.
        # Effect-producing paths still require _registration and current admission.
        registration = await self._stored_registration(session, row)
        binding = await self._admitted_policy(session, registration.data["provider"])
        policy_current = self._policy_matches(registration, binding)
        secret = self._open(
            registration.encrypted_secret, self._client_context(row.id, row.revision, registration.data)
        ).get("secret")
        effects = await self._effect_packets(session, row, ignore=completing)
        blocked = self._effect_block(row, effects)
        result = self._summary_packet(row, registration.data, secret, effects)
        grant = await self._grant(session, row)
        if grant is not None:
            result.update(family=grant.family, grant_revision=grant.revision, grant_status=grant.status)
            payload = self._open(grant.encrypted_data, self._grant_context(row, grant))
            result["refresh_available"] = bool(payload.get("refresh_token"))
            result["grant_expires_at"] = grant.expires_at.isoformat()
            result["allowed_actions"]["refresh"] = (
                not blocked and bool(payload.get("refresh_token")) and grant.actor_id == actor.id
            )
        result["policy_current"] = policy_current
        if not policy_current:
            result["allowed_actions"].update(authorize=False, refresh=False)
            if binding is None:
                result["allowed_actions"].update(configure=False, supersede=False)
        return OAuthConnectionSummary.model_validate(result).model_dump()

    async def _grant(self, session: AsyncSession, row: OAuthConnectionEntity) -> OAuthGrantEntity | None:
        return await session.scalar(
            select(OAuthGrantEntity)
            .where(
                OAuthGrantEntity.connection_id == row.id,
                OAuthGrantEntity.registration_revision == row.revision,
                OAuthGrantEntity.status == "authorized_unverified",
            )
            .order_by(OAuthGrantEntity.created_at.desc())
            .limit(1)
        )

    async def _effects_available(self, session: AsyncSession, row: OAuthConnectionEntity) -> str | None:
        effects = list(
            await session.scalars(
                select(OAuthEffectEntity)
                .where(
                    OAuthEffectEntity.connection_id == row.id,
                    OAuthEffectEntity.state.in_(("executing", "unknown")),
                )
                .with_for_update()
            )
        )
        now = await self._time(session)
        for effect in effects:
            if effect.state == "executing" and effect.lease_expires_at <= now:
                effect.state = "unknown"
                row.status = "reauthorization_required"
        if any(effect.state == "executing" for effect in effects):
            return "effect_executing"
        # Reauthorization opens a new registration revision. An unresolved old
        # revision remains archived and can never dispatch its rotating token again.
        if any(
            effect.state == "unknown" and effect.data["registration_revision"] == row.revision for effect in effects
        ):
            return "effect_unknown_reauthorization_required"
        return None

    async def _admit_registration(self, packet: Data, secret: str | None) -> None:
        await self.store.admit_credentials(packet)
        if secret:
            identifier = uuid4().hex
            prospective = self.matcher.packet(identifier, "registration", identifier, 1, [secret])
            if self.matcher.contains(packet, [prospective]):
                raise ValueError("Do not place configured credentials in public instructions, messages or tool data")

    async def configure(self, name: str, request: OAuthConnectionInput, actor: Principal) -> Data:
        return await self._configure(name, request, actor, admission=self._admit_registration)

    async def supersede(self, name: str, request: OAuthSupersedeInput, actor: Principal) -> Data:
        return await self._configure(
            name, request.registration, actor, recovery=request, admission=self._admit_registration
        )

    async def _configure(
        self,
        name: str,
        request: OAuthConnectionInput,
        actor: Principal,
        *,
        recovery: OAuthSupersedeInput | None = None,
        admission: Callable[[Data, str | None], Awaitable[None]] | None = None,
    ) -> Data:
        async with self._locked(name) as session:
            await self._authority(session, actor)
            row = await self.records.named(name)
            if request.expected_revision is not None and request.expected_revision != (row.revision if row else 0):
                raise ValueError("Connection changed; refresh its current configuration before saving")
            binding = await self._admitted_policy(session, request.provider)
            policy = binding.provider if binding else None
            if policy is None or policy.prerequisite or not self.browser_origin:
                raise ValueError("Provider registration prerequisites are not satisfied")
            data: Data = {
                "provider": request.provider,
                "project_id": request.project_id,
                "client_id": request.client_id,
                "authentication": request.authentication,
                "policy": asdict(policy),
                "callback": self.browser_origin + "/api/v1/connection-oauth/" + name + "/callback",
            }
            if self.policy_lookup is not None:
                assert binding is not None
                data.update(policy_revision=binding.revision, policy_digest=binding.digest)
            data = json.loads(json.dumps(data))
            if request.project_id and await session.get(ForgeProjectEntity, UUID(request.project_id)) is None:
                raise KeyError("Connection project not found")
            if recovery and row is None:
                raise KeyError("Connection not found")
            previous = None
            effects: list[Data] = []
            unknown: set[str] = set()
            if row:
                effects = await self._effect_packets(session, row)
                blocked = self._effect_block(row, effects)
                if recovery:
                    if row.revision != recovery.expected_revision:
                        raise ValueError("Connection revision changed; review its current uncertainty")
                    unknown = {
                        packet["id"]
                        for packet in effects
                        if packet["state"] == "unknown" and packet["registration_revision"] == row.revision
                    }
                    if blocked == "effect_executing" or not unknown or set(recovery.unknown_effect_ids) != unknown:
                        raise ValueError(
                            "Select the exact current unknown effects; active effects cannot be superseded"
                        )
                    if recovery.registration.client_secret is None and not recovery.registration.public_client:
                        raise ValueError("A new admission requires an explicit secret choice")
                    await self.store.admit_credentials(recovery.reason)
                elif blocked:
                    raise OAuthDisposition(blocked)
                previous = await session.scalar(
                    select(OAuthRegistrationEntity).where(
                        OAuthRegistrationEntity.connection_id == row.id,
                        OAuthRegistrationEntity.revision == row.revision,
                    )
                )
            explicit = request.client_secret is not None or request.public_client is True
            if previous and previous.data != data and not explicit:
                raise ValueError("A changed registration requires an explicit secret choice")
            if previous and previous.data == data and not explicit:
                result = await self._summary(session, cast(OAuthConnectionEntity, row), actor)
                if admission:
                    await admission(result, None)
                return result
            if previous is None and not explicit:
                raise ValueError("A new registration requires an explicit secret choice")
            secret = request.client_secret.get_secret_value() if request.client_secret else None
            if request.authentication == "client_secret_basic" and not secret:
                raise ValueError("Basic authentication requires a confidential client secret")
            if admission:
                prospective = OAuthConnectionEntity(
                    id=row.id if row else uuid4(),
                    name=name,
                    revision=row.revision + 1 if row else 1,
                    status="authorization_required",
                )
                packet = self._summary_packet(prospective, data, secret, effects)
                await admission(
                    {
                        "summary": OAuthConnectionSummary.model_validate(packet).model_dump(),
                        "registration": data,
                        "recovery_reason": recovery.reason if recovery else None,
                    },
                    secret,
                )
            if recovery:
                assert row is not None
                for effect_id in unknown:
                    uncertain = await session.get(OAuthEffectEntity, UUID(effect_id))
                    assert uncertain is not None
                    if uncertain.state == "executing":
                        uncertain.state = "unknown"
                await self._event(
                    session,
                    row,
                    "uncertain_revision_superseded",
                    actor,
                    unknown_effect_ids=sorted(unknown),
                    previous_revision=row.revision,
                    reason=recovery.reason,
                    acknowledged_unknown=True,
                )
            self._remember({"secret": secret})
            if row is None:
                row = OAuthConnectionEntity(id=uuid4(), name=name, revision=1, status="authorization_required")
                await self.records.save(row)
            else:
                row.revision += 1
                row.status = "authorization_required"
            registration = OAuthRegistrationEntity(
                id=uuid4(),
                connection_id=row.id,
                revision=row.revision,
                data=data,
                encrypted_secret=self._seal(self._client_context(row.id, row.revision, data), {"secret": secret}),
            )
            session.add(registration)
            await session.flush()
            await self._credential_evidence(
                session, row.id, "registration", registration.id, row.revision, {"secret": secret}
            )
            await self._event(session, row, "registration_configured", actor)
            return await self._summary(session, row, actor)

    async def directory(self, actor: Principal, *, project_id: str | None = None) -> list[Data]:
        if project_id is not None and not re.fullmatch(r"[a-f0-9]{32}", project_id):
            raise ValueError("Select an exact project identifier")
        async with self.store._unit() as session:
            await self._authority(session, actor)
            if project_id and await session.get(ForgeProjectEntity, UUID(project_id)) is None:
                raise KeyError("Connection project not found")
            rows = list(
                await session.scalars(
                    select(OAuthConnectionEntity).order_by(OAuthConnectionEntity.name).limit(101).with_for_update()
                )
            )
            if len(rows) > 100:
                raise ValueError("Connection directory exceeds its reviewed bound")
            result = []
            for row in rows:
                summary = await self._summary(session, row, actor)
                if project_id is None or summary["project_id"] == project_id:
                    result.append(summary)
            await self.store.admit_credentials(result)
            return result

    async def summary(self, name: str, actor: Principal) -> Data:
        async with self._locked(name) as session:
            await self._authority(session, actor)
            result = await self._summary(session, await self._required(name), actor)
            await self.store.admit_credentials(result)
            return result

    async def effect_receipt(self, name: str, request_id: str, actor: Principal) -> Data:
        if not re.fullmatch(r"[a-f0-9]{32}", request_id):
            raise ValueError("Select an exact effect request identifier")
        async with self._locked(name) as session:
            await self._authority(session, actor)
            row = await self._required(name)
            registration = await self._stored_registration(session, row)
            binding = await self._admitted_policy(session, registration.data["provider"])
            effect = await session.scalar(
                select(OAuthEffectEntity).where(
                    OAuthEffectEntity.connection_id == row.id, OAuthEffectEntity.request_id == request_id
                )
            )
            if effect is None:
                raise KeyError("Connection effect not found")
            expired = effect.lease_expires_at <= await self._time(session)
            result = OAuthEffectReceipt.model_validate(
                {
                    "id": effect.id.hex,
                    "request_id": request_id,
                    "kind": effect.kind,
                    "state": "unknown" if effect.state == "executing" and expired else effect.state,
                    "registration_revision": effect.data["registration_revision"],
                    "lease_expired": expired,
                    "observed_at": effect.data.get("observed_at"),
                    "current_context": self._policy_matches(registration, binding)
                    and effect.data.get("actor_id") == actor.id.hex
                    and effect.data["registration_revision"] == row.revision
                    and row.status != "disconnected",
                }
            ).model_dump()
            await self.store.admit_credentials(result)
            return result

    async def prepare(self, name: str, actor: Principal) -> Data:
        async with self._locked(name) as session:
            await self._authority(session, actor)
            row = await self._required(name)
            await self._registration(session, row)
            blocked = self._effect_block(row, await self._effect_packets(session, row))
            if blocked:
                raise OAuthDisposition(blocked)
            if row.status == "disconnected":
                raise ValueError("Connection is disconnected; configure a new revision")
            ticket, attempt_id = secrets.token_urlsafe(32), uuid4()
            session.add(
                OAuthAttemptEntity(
                    id=attempt_id,
                    connection_id=row.id,
                    revision=row.revision,
                    actor_id=actor.id,
                    kind="handoff",
                    digest=digest(ticket),
                    expires_at=await self._time(session) + timedelta(minutes=5),
                    encrypted_data=self._seal(
                        self._context("handoff", row.id, row.revision, attempt_id.hex),
                        {
                            "issuer": actor.issuer,
                            "subject": actor.subject,
                        },
                    ),
                )
            )
            await self._event(session, row, "browser_handoff_prepared", actor, attempt_id=attempt_id.hex)
            result = {
                "ticket": ticket,
                "continuation_url": self.browser_origin
                + "/api/v1/connection-oauth/"
                + name
                + "/continue?"
                + urlencode({"ticket": ticket}),
            }
            await self.store.admit_credentials(result)
            return result

    async def begin(self, name: str, ticket: str, actor: Principal, *, browser_authenticated: bool) -> OAuthChallenge:
        if not ticket or len(ticket) > 128:
            raise ValueError("Invalid browser handoff")
        if not browser_authenticated or actor.kind == "service" or actor.authentication_method == "oidc_bearer":
            raise ValueError("An authenticated human browser is required")
        async with self._locked(name) as session:
            await self._authority(session, actor)
            row = await self._required(name)
            registration = await self._registration(session, row)
            blocked = self._effect_block(row, await self._effect_packets(session, row))
            if blocked:
                raise OAuthDisposition(blocked)
            handoff = await session.scalar(
                select(OAuthAttemptEntity)
                .where(
                    OAuthAttemptEntity.connection_id == row.id,
                    OAuthAttemptEntity.digest == digest(ticket),
                    OAuthAttemptEntity.kind == "handoff",
                )
                .with_for_update()
            )
            if handoff is None or handoff.consumed or handoff.expires_at <= await self._time(session):
                raise ValueError("Browser handoff expired or consumed")
            if handoff.actor_id != actor.id:
                raise ValueError("Browser handoff actor does not match")
            payload = self._open(
                handoff.encrypted_data, self._context("handoff", row.id, handoff.revision, handoff.id.hex)
            )
            if payload != {"issuer": actor.issuer, "subject": actor.subject}:
                raise ValueError("Browser handoff actor does not match")
            if row.revision != handoff.revision or row.status == "disconnected":
                raise ValueError("Connection revision changed or disconnected")
            handoff.consumed = True
            state, nonce, pair, attempt_id = (
                secrets.token_urlsafe(32),
                secrets.token_urlsafe(32),
                generate_pkce(),
                uuid4(),
            )
            session.add(
                OAuthAttemptEntity(
                    id=attempt_id,
                    connection_id=row.id,
                    revision=row.revision,
                    actor_id=actor.id,
                    kind="code",
                    digest=digest(state),
                    nonce_digest=digest(nonce),
                    expires_at=await self._time(session) + timedelta(minutes=5),
                    encrypted_data=self._seal(
                        self._context("code", row.id, row.revision, attempt_id.hex),
                        {
                            "verifier": pair.verifier,
                            "issuer": actor.issuer,
                            "subject": actor.subject,
                        },
                    ),
                )
            )
            await self._event(session, row, "authorization_started", actor, attempt_id=attempt_id.hex)
            params = {
                "response_type": "code",
                "client_id": registration.data["client_id"],
                "redirect_uri": registration.data["callback"],
                "scope": " ".join(registration.data["policy"]["scopes"]),
                "resource": registration.data["policy"]["resource"],
                "state": state,
                "code_challenge": pair.challenge,
                "code_challenge_method": "S256",
            }
            result = OAuthChallenge(
                registration.data["policy"]["authorization_endpoint"] + "?" + urlencode(params), nonce, attempt_id.hex
            )
            await self.store.admit_credentials({"parameters": params, "challenge": asdict(result)})
            return result

    async def disconnect(self, name: str, actor: Principal) -> Data:
        async with self._locked(name) as session:
            await self._authority(session, actor)
            row = await self._required(name)
            row.status = "disconnected"
            for grant in await session.scalars(
                select(OAuthGrantEntity).where(OAuthGrantEntity.connection_id == row.id)
            ):
                grant.status = "revoked"
                grant.encrypted_data = self._seal(self._grant_context(row, grant), {})
            await self._event(session, row, "locally_disconnected", actor)
            result = await self._summary(session, row, actor)
            await self.store.admit_credentials(result)
            return result

    async def complete(
        self, name: str, state: str, code: str, browser_nonce: str, actor: Principal, *, issuer: str
    ) -> Data:
        if not state or len(state) > 128 or not code or len(code.encode()) > 4096:
            raise ValueError("Invalid authorization response")
        if actor.kind == "service" or actor.authentication_method == "oidc_bearer":
            raise ValueError("An authenticated human browser is required")
        rejection: str | None = None
        async with self._locked(name) as session:
            await self._authority(session, actor)
            row = await self._required(name)
            registration = await self._registration(session, row)
            attempt = await session.scalar(
                select(OAuthAttemptEntity)
                .where(
                    OAuthAttemptEntity.connection_id == row.id,
                    OAuthAttemptEntity.kind == "code",
                    OAuthAttemptEntity.digest == digest(state),
                )
                .with_for_update()
            )
            if attempt is None or attempt.consumed or attempt.expires_at <= await self._time(session):
                raise ValueError("Authorization attempt expired or consumed")
            if (
                attempt.actor_id != actor.id
                or len(browser_nonce) > 128
                or not hmac.compare_digest(attempt.nonce_digest or "", digest(browser_nonce))
                or issuer != registration.data["policy"]["issuer"]
                or attempt.revision != row.revision
                or row.status == "disconnected"
            ):
                raise ValueError("Authorization actor, browser, issuer or revision does not match")
            payload = self._open(attempt.encrypted_data, self._context("code", row.id, row.revision, attempt.id.hex))
            if payload["issuer"] != actor.issuer or payload["subject"] != actor.subject:
                raise ValueError("Authorization actor does not match")
            rejection = await self._effects_available(session, row)
            if rejection is None:
                attempt.consumed = True
                effect = OAuthEffectEntity(
                    id=uuid4(),
                    connection_id=row.id,
                    request_id=attempt.id.hex,
                    kind="code",
                    state="executing",
                    lease_expires_at=await self._time(session) + timedelta(seconds=30),
                    data={
                        "registration_revision": row.revision,
                        "actor_id": actor.id.hex,
                        "attempt_id": attempt.id.hex,
                        "request_digest": digest(state),
                    },
                )
                session.add(effect)
                await self._event(
                    session, row, "exchange_intent", actor, effect_id=effect.id.hex, attempt_id=attempt.id.hex
                )
                frozen, effect_id = dict(registration.data), effect.id
                secret = self._open(
                    registration.encrypted_secret, self._client_context(row.id, row.revision, registration.data)
                )["secret"]
        if rejection:
            raise OAuthDisposition(rejection)
        try:
            await self._dispatch_fence(name, effect_id, actor)
            async with self._client(frozen, secret) as client:
                tokens = await client.exchange_code(
                    code,
                    redirect_uri=frozen["callback"],
                    code_verifier=payload["verifier"],
                    authentication=frozen["authentication"],
                    resource=frozen["policy"]["resource"],
                )
            return await self._publish(name, effect_id, tokens, actor)
        except BaseException as error:
            await self._retain_failure(name, effect_id, actor, error)
            raise

    def _client(self, frozen: Data, secret: str | None) -> OAuth2Client:
        return OAuth2Client(
            frozen["client_id"],
            OAuth2Endpoints(frozen["policy"]["token_endpoint"]),
            client_secret=secret,
            timeout=10,
            max_response_bytes=65536,
            allow_loopback_http=self.loopback,
        )

    async def _dispatch_fence(self, name: str, effect_id: UUID, actor: Principal) -> None:
        async with self._locked(name) as session:
            await self._authority(session, actor)
            row = await self._required(name)
            await self._registration(session, row)
            effect = await session.get(OAuthEffectEntity, effect_id)
            if effect is None or effect.state != "executing" or effect.data["actor_id"] != actor.id.hex:
                raise OAuthDisposition("effect_changed")
            if effect.data["registration_revision"] != row.revision or row.status == "disconnected":
                raise OAuthDisposition("connection_changed")
            if effect.lease_expires_at <= await self._time(session):
                raise OAuthDisposition("effect_lease_expired")
            if effect.kind == "refresh":
                grant = await self._grant(session, row)
                if (
                    grant is None
                    or grant.family != effect.data["family"]
                    or grant.revision != effect.data["grant_revision"]
                    or grant.actor_id != actor.id
                ):
                    raise OAuthDisposition("grant_changed")

    async def _retain_failure(self, name: str, effect_id: UUID, actor: Principal, error: BaseException) -> None:
        # Cancellation must not erase a committed intent or enable a rotating-token
        # replay. The small receipt transaction is shielded; no remote I/O occurs here.
        async def retain() -> None:
            async with self._locked(name) as session:
                row = await self._required(name)
                effect = await session.get(OAuthEffectEntity, effect_id)
                if effect is None or effect.state != "executing":
                    return
                known_rejections = {
                    "invalid_client",
                    "invalid_grant",
                    "invalid_scope",
                    "access_denied",
                    "unauthorized_client",
                    "unsupported_grant_type",
                    "invalid_request",
                }
                kind = error.code if isinstance(error, OAuth2ClientError) else "unknown"
                effect.state = "rejected" if kind in known_rejections else "unknown"
                effect.data = {**effect.data, "disposition": kind}
                if row.revision == effect.data["registration_revision"] and row.status != "disconnected":
                    row.status = "reauthorization_required"
                    grant = await self._grant(session, row)
                    if grant:
                        grant.status = "reauthorization_required"
                await self._event(
                    session, row, "effect_" + effect.state, actor, effect_id=effect.id.hex, disposition=kind
                )

        task = asyncio.create_task(retain())
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
        task.result()

    async def _publish(self, name: str, effect_id: UUID, tokens: OAuth2Tokens, actor: Principal) -> Data:
        self._remember({"access_token": tokens.access_token, "refresh_token": tokens.refresh_token})
        async with self._locked(name) as session:
            await self._authority(session, actor)
            row = await self._required(name)
            registration = await self._registration(session, row)
            effect = await session.get(OAuthEffectEntity, effect_id)
            if (
                effect is None
                or effect.state != "executing"
                or effect.data["actor_id"] != actor.id.hex
                or row.revision != effect.data["registration_revision"]
                or row.status == "disconnected"
                or effect.lease_expires_at <= await self._time(session)
            ):
                raise OAuthDisposition("connection_changed")
            policy = registration.data["policy"]
            previous = await self._grant(session, row)
            expected = set(policy["scopes"])
            if tokens.scope is None:
                if effect.kind == "code" and policy["scope_omission"] == "requested":
                    scopes = expected
                elif effect.kind == "refresh" and policy["refresh_scope_omission"] == "preserve" and previous:
                    scopes = set(previous.data["scopes"])
                else:
                    raise OAuthDisposition("scope_missing")
            else:
                scopes = set(tokens.scope.split())
            if scopes != expected:
                raise OAuthDisposition("scope_mismatch")
            if tokens.token_type.lower() != "bearer" or tokens.expires_in is None or tokens.expires_in > 86400:
                raise OAuthDisposition("token_contract_invalid")
            refresh = tokens.refresh_token
            expires_at = await self._time(session) + timedelta(seconds=tokens.expires_in)
            if effect.kind == "refresh":
                if (
                    previous is None
                    or previous.family != effect.data["family"]
                    or previous.revision != effect.data["grant_revision"]
                    or previous.actor_id != actor.id
                ):
                    raise OAuthDisposition("grant_changed")
                if refresh is None:
                    if policy["refresh_token_omission"] != "preserve":
                        raise OAuthDisposition("refresh_token_missing")
                    refresh = self._open(previous.encrypted_data, self._grant_context(row, previous))["refresh_token"]
                grant = previous
                grant.revision += 1
            else:
                for old in await session.scalars(
                    select(OAuthGrantEntity).where(
                        OAuthGrantEntity.connection_id == row.id,
                        OAuthGrantEntity.status == "authorized_unverified",
                    )
                ):
                    old.status = "superseded"
                grant = OAuthGrantEntity(
                    id=uuid4(),
                    connection_id=row.id,
                    registration_revision=row.revision,
                    family=uuid4().hex,
                    revision=1,
                    actor_id=actor.id,
                    status="authorized_unverified",
                )
                session.add(grant)
            grant.expires_at = expires_at
            grant.data = {
                "scopes": sorted(scopes),
                "resource": policy["resource"],
                "issuer": policy["issuer"],
                "subject_verified": False,
            }
            grant.encrypted_data = self._seal(
                self._grant_context(row, grant),
                {
                    "access_token": tokens.access_token,
                    "refresh_token": refresh,
                },
            )
            await self._credential_evidence(
                session,
                row.id,
                "grant",
                grant.id,
                grant.revision,
                {"access_token": tokens.access_token, "refresh_token": refresh},
            )
            row.status = "authorized_unverified"
            await session.flush()
            result = await self._summary(session, row, actor, completing=effect.id)
            observed_at = (await self._time(session)).isoformat()
            result["observed_at"] = observed_at
            await self.store.admit_credentials(result)
            effect.data = {**effect.data, "result": result, "observed_at": observed_at}
            effect.state = "completed"
            await self._event(
                session,
                row,
                "effect_completed",
                actor,
                effect_id=effect.id.hex,
                family=grant.family,
                grant_revision=grant.revision,
            )
            return result

    async def refresh(self, name: str, request_id: str, actor: Principal) -> Data:
        if not re.fullmatch(r"[a-f0-9]{32}", request_id):
            raise ValueError("Refresh requires a stable request ID")
        rejection: str | None = None
        async with self._locked(name) as session:
            await self._authority(session, actor)
            row = await self._required(name)
            registration = await self._registration(session, row)
            existing = await session.scalar(
                select(OAuthEffectEntity).where(
                    OAuthEffectEntity.connection_id == row.id,
                    OAuthEffectEntity.request_id == request_id,
                )
            )
            if existing:
                if (
                    existing.kind != "refresh"
                    or existing.data["actor_id"] != actor.id.hex
                    or existing.data["registration_revision"] != row.revision
                    or row.status == "disconnected"
                ):
                    raise OAuthDisposition("request_context_changed")
                if existing.state == "completed":
                    grant = await self._grant(session, row)
                    if grant is None or grant.family != existing.data["family"]:
                        raise OAuthDisposition("grant_changed")
                    result = cast(Data, existing.data["result"])
                    await self.store.admit_credentials(result)
                    return result
                if existing.state == "rejected":
                    raise OAuth2ClientError(existing.data.get("disposition", "invalid_response"))
                raise OAuthDisposition("effect_" + existing.state)
            grant = await self._grant(session, row)
            if grant is None or row.status != "authorized_unverified" or grant.actor_id != actor.id:
                raise OAuthDisposition("reauthorization_required")
            rejection = await self._effects_available(session, row)
            if rejection is None:
                payload = self._open(grant.encrypted_data, self._grant_context(row, grant))
                if not payload.get("refresh_token"):
                    raise OAuthDisposition("refresh_token_unavailable")
                secret = self._open(
                    registration.encrypted_secret, self._client_context(row.id, row.revision, registration.data)
                )["secret"]
                effect = OAuthEffectEntity(
                    id=uuid4(),
                    connection_id=row.id,
                    request_id=request_id,
                    kind="refresh",
                    state="executing",
                    lease_expires_at=await self._time(session) + timedelta(seconds=30),
                    data={
                        "registration_revision": row.revision,
                        "actor_id": actor.id.hex,
                        "family": grant.family,
                        "grant_revision": grant.revision,
                    },
                )
                session.add(effect)
                frozen, effect_id = dict(registration.data), effect.id
                await self._event(
                    session,
                    row,
                    "refresh_intent",
                    actor,
                    effect_id=effect_id.hex,
                    family=grant.family,
                    grant_revision=grant.revision,
                )
        if rejection:
            raise OAuthDisposition(rejection)
        try:
            await self._dispatch_fence(name, effect_id, actor)
            async with self._client(frozen, secret) as client:
                tokens = await client.refresh_token(
                    payload["refresh_token"],
                    scopes=tuple(frozen["policy"]["scopes"]),
                    authentication=frozen["authentication"],
                    resource=frozen["policy"]["resource"],
                )
            return await self._publish(name, effect_id, tokens, actor)
        except BaseException as error:
            await self._retain_failure(name, effect_id, actor, error)
            raise
