# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Bounded integration contracts and atomic, incremental product-definition changes."""

from __future__ import annotations

import copy
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pyfly.container import service

from firefly_forge.functional_features import FunctionalFeature
from firefly_forge.product_provenance import content_digest, validate_origin
from firefly_forge.store import identifier, now

if TYPE_CHECKING:
    from firefly_forge.collaboration import Collaboration

Data = dict[str, Any]


class ContractField(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_.-]{0,99}$")
    type: Literal["string", "integer", "number", "boolean", "object", "array"]
    required: bool = True
    description: str = Field(default="", max_length=2000)


class IntegrationAuthentication(BaseModel):
    model_config = ConfigDict(extra="forbid")
    method: Literal["none", "oauth2", "api_key", "mtls", "workload_identity"] = "none"
    secret_refs: list[str] = Field(default_factory=list, max_length=10)

    @field_validator("secret_refs")
    @classmethod
    def references_only(cls, values: list[str]) -> list[str]:
        import re

        if len(set(values)) != len(values) or any(
            not re.fullmatch(r"secret://[a-z][a-z0-9-]{0,63}/[A-Za-z][A-Za-z0-9_-]{0,99}", value) for value in values
        ):
            raise ValueError("Authentication accepts named secret references only")
        return values

    @model_validator(mode="after")
    def authentication_contract(self) -> IntegrationAuthentication:
        if self.method in {"api_key", "oauth2", "mtls"} and not self.secret_refs:
            raise ValueError("This authentication method requires named secret references")
        if self.method == "none" and self.secret_refs:
            raise ValueError("Unauthenticated integrations cannot declare secret references")
        return self


class IntegrationRetry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_attempts: int = Field(default=1, ge=1, le=5)
    backoff_seconds: int = Field(default=0, ge=0, le=3600)
    retry_on: list[Literal["timeout", "connection_failure", "rate_limit", "server_error"]] = Field(
        default_factory=list, max_length=4
    )

    @model_validator(mode="after")
    def explicit_retry(self) -> IntegrationRetry:
        if len(set(self.retry_on)) != len(self.retry_on) or self.max_attempts > 1 and not self.retry_on:
            raise ValueError("Retries require unique, explicit retryable failures")
        return self


class IntegrationIdempotency(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["required", "optional", "not_applicable"] = "not_applicable"
    key_field: str | None = Field(default=None, pattern=r"^[A-Za-z][A-Za-z0-9_.-]{0,99}$")
    key_location: Literal["header", "body"] = "header"

    @model_validator(mode="after")
    def key_contract(self) -> IntegrationIdempotency:
        if (self.mode == "not_applicable") != (self.key_field is None):
            raise ValueError("Idempotency requires an explicit key field when applicable")
        return self


class IntegrationContract(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    id: str = Field(pattern=r"^[a-z][a-z0-9-]{0,63}$")
    name: str = Field(min_length=1, max_length=200)
    kind: Literal["owned", "third_party"]
    protocol: Literal["http", "event", "queue", "database"]
    endpoint: str | None = Field(default=None, max_length=2048)
    service_ref: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9-]{0,63}$")
    header_names: list[str] = Field(default_factory=list, max_length=20)
    authentication: IntegrationAuthentication = Field(default_factory=IntegrationAuthentication)
    inputs: list[ContractField] = Field(min_length=1, max_length=50)
    outputs: list[ContractField] = Field(min_length=1, max_length=50)
    failure_paths: list[str] = Field(min_length=1, max_length=20)
    retry: IntegrationRetry = Field(default_factory=IntegrationRetry)
    idempotency: IntegrationIdempotency = Field(default_factory=IntegrationIdempotency)
    side_effects: bool = False
    feature_refs: list[str] = Field(min_length=1, max_length=100)
    journey_refs: list[str] = Field(min_length=1, max_length=50)

    @field_validator("endpoint")
    @classmethod
    def public_endpoint(cls, value: str | None) -> str | None:
        if value is not None:
            parsed = urlsplit(value)
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError("Integration endpoint metadata requires HTTPS without credentials or query parameters")
            _ = parsed.port
        return value

    @field_validator("header_names")
    @classmethod
    def names_only(cls, values: list[str]) -> list[str]:
        import re

        if len(set(values)) != len(values) or any(
            not re.fullmatch(r"[A-Za-z][A-Za-z0-9-]{0,99}", value) for value in values
        ):
            raise ValueError("Declare header names only, never credential values")
        return values

    @field_validator("failure_paths", "feature_refs", "journey_refs")
    @classmethod
    def bounded_entries(cls, values: list[str]) -> list[str]:
        if len(set(values)) != len(values) or any(not value.strip() or len(value) > 2000 for value in values):
            raise ValueError("Integration entries require unique bounded values")
        return values

    @model_validator(mode="after")
    def coherent_contract(self) -> IntegrationContract:
        if self.kind == "owned" and not self.service_ref:
            raise ValueError("Owned integrations require an explicit service reference")
        if self.protocol == "http" and not self.endpoint:
            raise ValueError("HTTP integrations require a public endpoint contract")
        if any(len({field.name for field in fields}) != len(fields) for fields in (self.inputs, self.outputs)):
            raise ValueError("Integration input/output names must be unique")
        if self.side_effects and self.retry.max_attempts > 1 and self.idempotency.mode != "required":
            raise ValueError("Retrying side effects requires mandatory idempotency")
        if (
            self.idempotency.key_location == "body"
            and self.idempotency.key_field
            and self.idempotency.key_field not in {field.name for field in self.inputs}
        ):
            raise ValueError("Body idempotency keys must reference an input field")
        if len(self.model_dump_json()) > 20000:
            raise ValueError("Integration contract exceeds 20 KB")
        return self


def validate_integrations(
    integrations: list[IntegrationContract], features: list[FunctionalFeature], journeys: list[str]
) -> None:
    if len({item.id for item in integrations}) != len(integrations):
        raise ValueError("Integration IDs must be unique")
    feature_ids = {item.id for item in features}
    for item in integrations:
        if set(item.feature_refs) - feature_ids or set(item.journey_refs) - set(journeys):
            raise ValueError("Integration references require existing feature IDs and exact customer journeys")


class FeatureChangeInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    request_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    base_revision: int = Field(ge=1)
    base_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    reason: str = Field(default="Incremental product change", min_length=1, max_length=2000)
    feature_upserts: list[FunctionalFeature] = Field(default_factory=list, max_length=100)
    remove_feature_ids: list[str] = Field(default_factory=list, max_length=100)
    integration_upserts: list[IntegrationContract] = Field(default_factory=list, max_length=50)
    remove_integration_ids: list[str] = Field(default_factory=list, max_length=50)
    append_journeys: list[str] = Field(default_factory=list, max_length=50)
    append_acceptance: list[str] = Field(default_factory=list, max_length=100)
    append_user_stories: list[str] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def bounded_changes(self) -> FeatureChangeInput:
        for upserts, removals in (
            (self.feature_upserts, self.remove_feature_ids),
            (self.integration_upserts, self.remove_integration_ids),
        ):
            ids = [item.id for item in upserts]
            if len(set(ids)) != len(ids) or len(set(removals)) != len(removals) or set(ids) & set(removals):
                raise ValueError("Change IDs must be unique and cannot be both removed and upserted")
        for entries in (self.append_journeys, self.append_acceptance, self.append_user_stories):
            if len(set(entries)) != len(entries) or any(not entry.strip() or len(entry) > 4000 for entry in entries):
                raise ValueError("Appended entries require unique bounded text")
        if len(self.model_dump_json()) > 80000:
            raise ValueError("Product change exceeds 80 KB")
        return self


@service
class ProductLifecycle:
    def __init__(self, collaboration: Collaboration) -> None:
        self.collaboration, self.store = collaboration, collaboration.store

    def _prepare(self, previous: Data, request: FeatureChangeInput) -> Data:
        from firefly_forge.collaboration import ProductDefinition

        if previous["revision"] != request.base_revision or previous["digest"] != request.base_digest:
            raise ValueError("Product change requires the current base revision and digest")
        original = ProductDefinition.model_validate(previous["definition"]).model_dump()
        proposed = copy.deepcopy(original)
        for field, upserts, removals in (
            ("features", request.feature_upserts, request.remove_feature_ids),
            ("integrations", request.integration_upserts, request.remove_integration_ids),
        ):
            existing = {item["id"]: item for item in proposed[field]}
            if set(removals) - set(existing):
                raise ValueError("Cannot remove an unknown contract ID")
            for item_id in removals:
                del existing[item_id]
            for item in upserts:
                existing[item.id] = item.model_dump()
            proposed[field] = list(existing.values())
        for field, entries in (
            ("journey", request.append_journeys),
            ("acceptance", request.append_acceptance),
            ("user_stories", request.append_user_stories),
        ):
            if set(entries) & set(proposed[field]):
                raise ValueError("Appended contract entries already exist")
            proposed[field].extend(entries)
        if original == proposed:
            raise ValueError("The request makes no change to the product contract")
        proposed["functional_reviewed"], proposed["design_review"] = False, None
        if proposed["architecture"]:
            proposed["architecture"]["reviewed"] = False
        definition = ProductDefinition.model_validate(proposed)
        before = {item["id"]: item for item in original["features"]}
        after = {item.id: item.model_dump() for item in definition.features}
        changed = sorted(key for key in set(before) | set(after) if before.get(key) != after.get(key))
        unchanged = sorted(key for key in before if before[key] == after.get(key))
        impact = {
            "changed_feature_ids": changed,
            "unchanged_feature_ids": unchanged,
            "unchanged_feature_digests": {key: content_digest(before[key]) for key in unchanged},
            "changed_integration_ids": sorted(
                {item.id for item in request.integration_upserts} | set(request.remove_integration_ids)
            ),
            "affected_journeys": sorted(
                {
                    journey
                    for key in changed
                    for item in (before.get(key), after.get(key))
                    if item
                    for journey in item["journey_refs"]
                }
                | set(request.append_journeys)
            ),
            "regression_acceptance_refs": sorted({ref for key in unchanged for ref in before[key]["acceptance_refs"]}),
            "reviews_required": ["functional", "design"] + (["architecture"] if definition.architecture else []),
        }
        return {
            "base_revision": request.base_revision,
            "base_digest": request.base_digest,
            "definition": definition.model_dump(mode="json"),
            "impact": impact,
            "implementation_status": "not_started",
        }

    async def preview(self, repository_id: str, request: FeatureChangeInput) -> Data:
        await self.store.admit_credentials(request.model_dump())
        previous = await self.collaboration.product(repository_id)
        if previous is None:
            raise ValueError("Save an initial product definition before requesting a feature change")
        return self._prepare(previous, request)

    async def apply(self, repository_id: str, request: FeatureChangeInput, *, actor_id: UUID | None = None) -> Data:
        from firefly_forge.collaboration import ProductDefinition
        from firefly_forge.collaboration_persistence import ForgeProductDefinitionEntity

        await self.store.admit_credentials(request.model_dump())
        request_digest = content_digest(request.model_dump())
        async with self.store._unit():
            await self.collaboration._repo_lock(repository_id)
            replay = await self.collaboration.products.find_change(identifier(repository_id), request.request_id)
            if replay:
                if replay.data["change_receipt"]["request_digest"] != request_digest:
                    raise ValueError("Product change replay conflicts with the original request")
                return copy.deepcopy(replay.data)
            previous = await self.collaboration.products.find_current(identifier(repository_id))
            if previous is None:
                raise ValueError("Save an initial product definition before requesting a feature change")
            prepared = self._prepare(previous.data, request)
            definition = ProductDefinition.model_validate(prepared["definition"])
            await self.store.admit_credentials(definition.model_dump())
            origin = await validate_origin(self.store, repository_id, definition)
            data = {
                "repository_id": repository_id,
                "revision": previous.revision + 1,
                "definition": definition.model_dump(mode="json"),
                "created_at": now(),
                "origin": origin,
                "change_receipt": {
                    "request_id": request.request_id,
                    "request_digest": request_digest,
                    "base_revision": request.base_revision,
                    "base_digest": request.base_digest,
                    "actor_id": actor_id.hex if actor_id else None,
                    "reason": request.reason,
                    "impact": prepared["impact"],
                    "implementation_status": "not_started",
                },
            }
            data["digest"] = content_digest(data)
            await self.collaboration.products.save(
                ForgeProductDefinitionEntity(
                    id=uuid4(),
                    repository_id=identifier(repository_id),
                    revision=data["revision"],
                    digest=data["digest"],
                    data=data,
                )
            )
            return copy.deepcopy(data)
