# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Independent model configurations and immutable, owner-private invocation snapshots."""

from __future__ import annotations

import copy
import re
from collections.abc import Callable
from typing import Any, Literal, cast
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pyfly.container import service

from firefly_forge.configuration import (
    FIELDS,
    SECRETS,
    ConfigurationStore,
    public_settings,
    validate_checkpoints,
    validate_completion_recovery,
    validate_endpoint,
)
from firefly_forge.product_provenance import content_digest
from firefly_forge.secrets import contains_known_credential

Data = dict[str, Any]
ModelProvider = Literal["codex", "claude", "bridge", "agentic"]
Task = Literal["product", "functions", "design", "architecture", "implementation", "review", "multimodal_qa"]
TASK_PROVIDERS: dict[str, set[str]] = {
    "product": {"agentic", "bridge"},
    "functions": {"agentic", "bridge"},
    "design": {"agentic", "bridge"},
    "architecture": {"agentic", "bridge"},
    "implementation": {"codex", "claude", "agentic", "bridge"},
    "review": {"codex", "claude", "agentic", "bridge"},
    "multimodal_qa": {"agentic", "bridge"},
}


class ModelProfileInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=2000)
    provider: ModelProvider
    values: dict[str, str] = Field(default_factory=dict, max_length=10)
    clear_secrets: list[str] = Field(default_factory=list, max_length=2)
    inherit_credentials: bool = False

    @field_validator("values", mode="before")
    @classmethod
    def recovery_choice(cls, values: Any) -> Any:
        if isinstance(values, dict) and "completion_recovery" in values:
            value = values["completion_recovery"]
            if not isinstance(value, str):
                raise ValueError("Completion recovery must be legacy or required")
            validate_completion_recovery(value)
        return values


def profile_id(value: str) -> str:
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", value):
        raise ValueError("Model profile requires a bounded stable identifier")
    return value


def validate_task(task: str, provider: str) -> None:
    if task not in TASK_PROVIDERS or provider not in TASK_PROVIDERS[task]:
        raise ValueError("Selected provider does not implement this task contract")


def task_catalog() -> list[Data]:
    descriptions = {
        "product": "Customer product dialogue and proposal; owner approval remains separate",
        "functions": "Journey-to-feature and expected-behavior dialogue proposal",
        "design": "Design guidance dialogue proposal; reference rendering remains deterministic",
        "architecture": "Architecture dialogue proposal; the existing profile-based endpoint remains deterministic",
        "implementation": "Real source-editing worker under frozen execution controls",
        "review": "Independent provider review of an isolated immutable verified tree",
        "multimodal_qa": "Actual native screenshot reasoning with matching verified image-input capability",
    }
    return [
        {
            "id": task,
            "description": descriptions[task],
            "providers": sorted(providers),
            "requires_verified_image_input": task == "multimodal_qa",
        }
        for task, providers in TASK_PROVIDERS.items()
    ]


@service
class ModelProfiles:
    def __init__(self, private: ConfigurationStore, credential_registry: Callable[[], Any] | None = None) -> None:
        self.private = private
        self.path = private.directory / "model-profiles.json"
        self.manifests = private.directory / "model-profile-revisions"
        self.manifests.mkdir(exist_ok=True, mode=0o700)
        if self.manifests.is_symlink():
            raise ValueError("Model profile revision directory must be a private real directory")
        self.manifests.chmod(0o700)
        self.credential_registry = credential_registry or (lambda: {})

    def redaction_settings(self) -> dict[str, str]:
        with self.private.lock:
            document = self.private._read(self.path)
        return {
            f"profile_{name}_{field}": record["settings"].get(environment, "")
            for name, record in document.items()
            for field, environment in FIELDS[record["provider"]].items()
            if field in SECRETS
        }

    def _read(self, name: str) -> Data:
        with self.private.lock:
            record = self.private._read(self.path).get(profile_id(name))
        if not record:
            raise KeyError("Model profile not found")
        if record["provider"] not in FIELDS:
            raise ValueError("Model profile provider is invalid")
        return copy.deepcopy(record)

    def resolve(self, name: str) -> tuple[str, dict[str, str]]:
        record = self._read(name)
        return record["provider"], dict(record["settings"])

    def _public(self, name: str, record: Data) -> Data:
        return {
            "id": name,
            "name": record["name"],
            "description": record["description"],
            "provider": record["provider"],
            "revision": record["revision"],
            "config": public_settings(record["provider"], record["settings"]),
            "tasks": [task for task, providers in TASK_PROVIDERS.items() if record["provider"] in providers],
            "image_input": {"status": "unverified"},
            "generation_verified": False,
        }

    def catalog(self) -> list[Data]:
        with self.private.lock:
            document = self.private._read(self.path)
        return [self._public(name, document[name]) for name in sorted(document)]

    def _settings(self, request: ModelProfileInput, previous: Data | None) -> dict[str, str]:
        fields = FIELDS[request.provider]
        if set(request.values) - fields.keys() or set(request.clear_secrets) - (SECRETS & fields.keys()):
            raise ValueError("Unknown model profile configuration field")
        if previous and previous["provider"] == request.provider:
            settings = dict(previous["settings"])
        else:
            settings = self.private.resolve(request.provider)
            if not request.inherit_credentials:
                for name in SECRETS & fields.keys():
                    settings[fields[name]] = ""
        for name, value in request.values.items():
            if len(value) > 16000 or "\x00" in value:
                raise ValueError("Model profile configuration exceeds limits")
            if name == "completion_recovery":
                value = validate_completion_recovery(value)
            if name in SECRETS and not value:
                continue
            if name == "checkpoints" and not value:
                continue
            if name in {"model", "executable"} and any(ord(char) < 32 for char in value):
                raise ValueError("Model profile configuration contains control characters")
            if name in {"timeout", "max_tokens"} and (
                not value.isdecimal() or not 1 <= int(value) <= (7200 if name == "timeout" else 131072)
            ):
                raise ValueError("Model profile execution limit is invalid")
            settings[fields[name]] = value
        for name in request.clear_secrets:
            settings[fields[name]] = ""
        if "FORGE_BRIDGE_COMPLETION_RECOVERY" in settings:
            validate_completion_recovery(settings["FORGE_BRIDGE_COMPLETION_RECOVERY"])
        endpoint = fields.get("endpoint")
        if endpoint and settings.get(endpoint):
            settings[endpoint] = validate_endpoint(settings[endpoint])
        validate_checkpoints(settings, provider=request.provider)
        return settings

    def _prepared_record(self, name: str, request: ModelProfileInput, document: Data) -> Data:
        previous = document.get(name)
        settings = self._settings(request, previous)
        secret_values = {
            fields[key]: settings.get(fields[key], "")
            for fields in [FIELDS[request.provider]]
            for key in SECRETS & fields.keys()
        }
        public = {
            "name": request.name,
            "description": request.description,
            "settings": public_settings(request.provider, settings),
        }
        registry = {**secret_values, **self.redaction_settings(), **self.credential_registry()}
        if contains_known_credential(public, registry):
            raise ValueError("Remove configured credentials from public model profile fields")
        return {
            "name": request.name,
            "description": request.description,
            "provider": request.provider,
            "revision": previous["revision"] + 1 if previous else 1,
            "settings": settings,
        }

    def preview_save(self, name: str, request: ModelProfileInput) -> tuple[Data, str]:
        name = profile_id(name)
        with self.private.lock:
            document = self.private._read(self.path)
            record = self._prepared_record(name, request, document)
            return self._public(name, record), content_digest({"document": document, "record": record})

    def save(self, name: str, request: ModelProfileInput, *, expected_digest: str | None = None) -> Data:
        name = profile_id(name)
        with self.private.lock:
            document = self.private._read(self.path)
            record = self._prepared_record(name, request, document)
            if (
                expected_digest is not None
                and content_digest({"document": document, "record": record}) != expected_digest
            ):
                raise ValueError("Model profile settings changed during admission; review and retry")
            document[name] = record
            self.private._write(self.path, document)
        return self._public(name, record)

    async def capabilities(self, name: str) -> Data:
        from firefly_forge.providers import ConfiguredProvider, get_provider

        provider, settings = self.resolve(name)
        return await cast(ConfiguredProvider, get_provider(provider, settings)).image_input_capability()

    async def test(self, name: str) -> Data:
        from firefly_forge.providers import test_provider_connection

        provider, settings = self.resolve(name)
        result = await test_provider_connection(provider, _ProfileConfiguration(self.private, provider, settings))
        return {
            "profile_id": name,
            "provider": provider,
            "readiness": result["readiness"],
            "image_input": await self.capabilities(name),
            "generation_verified": False,
        }

    async def validate_selection(self, name: str, task: str) -> tuple[Data, Data]:
        from firefly_forge.providers import ConfiguredProvider, get_provider

        record = self._read(name)
        provider, settings = record["provider"], record["settings"]
        validate_task(task, provider)
        if provider in {"bridge", "agentic"}:
            field = "FORGE_BRIDGE_TOKEN" if provider == "bridge" else "FORGE_AGENTIC_API_KEY"
            if not settings.get(field):
                raise ValueError("Selected model profile requires its own configured credential")
        if provider == "bridge" and not settings.get("FORGE_BRIDGE_URL"):
            raise ValueError("Selected bridge profile requires a configured endpoint")
        image_input: Data = {"status": "unverified"}
        if task == "multimodal_qa":
            image_input = await cast(ConfiguredProvider, get_provider(provider, settings)).image_input_capability()
            if image_input.get("status") != "verified":
                raise ValueError("Multimodal task requires verified image-input capability; no fallback is permitted")
            model_field = FIELDS[provider]["model"]
            if image_input.get("model") != settings.get(model_field):
                raise ValueError("Multimodal task requires matching verified model capability")
            revision = image_input.get("config_revision")
            if not isinstance(revision, str) or not re.fullmatch(r"[a-f0-9]{64}", revision):
                raise ValueError("Verified image-input capability requires its exact configuration revision")
        return record, image_input

    async def snapshot(self, name: str, task: str) -> Data:
        record, image_input = await self.validate_selection(name, task)
        provider, settings = record["provider"], record["settings"]
        validate_checkpoints(settings, task=task, provider=provider)
        revision = uuid4().hex
        self.private._write(self.private.revisions / f"{revision}.json", settings)
        snapshot = {
            "profile_id": name,
            "profile_revision": record["revision"],
            "profile_digest": content_digest({"provider": provider, "settings": settings}),
            "provider": provider,
            "task": task,
            "config_revision": revision,
            "execution_config": public_settings(provider, settings),
            "image_input": image_input,
        }
        self.private._write(self.manifests / f"{revision}.json", snapshot)
        return snapshot

    def validate_snapshot(self, snapshot: Data) -> dict[str, str]:
        profile_id(snapshot["profile_id"])
        validate_task(snapshot["task"], snapshot["provider"])
        settings = self.private.load_snapshot(snapshot["config_revision"])
        manifest = self.private._read(self.manifests / f"{snapshot['config_revision']}.json")
        if not manifest or any(snapshot.get(key) != value for key, value in manifest.items()):
            raise ValueError("Model profile snapshot digest/manifest mismatch")
        if snapshot["profile_digest"] != content_digest({"provider": snapshot["provider"], "settings": settings}):
            raise ValueError("Model profile snapshot digest mismatch")
        if snapshot["execution_config"] != public_settings(snapshot["provider"], settings):
            raise ValueError("Model profile snapshot public configuration mismatch")
        return settings


class _ProfileConfiguration(ConfigurationStore):
    def __init__(self, base: ConfigurationStore, provider: str, settings: dict[str, str]) -> None:
        self.base, self.provider, self.settings = base, provider, settings

    def resolve(self, provider: str) -> dict[str, str]:
        return dict(self.settings) if provider == self.provider else self.base.resolve(provider)
