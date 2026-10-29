# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Owner-private provider settings and immutable queued-run credentials."""

from __future__ import annotations

import json
import os
import stat
import tempfile
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from filelock import FileLock
from pydantic import BaseModel, ConfigDict, Field
from pyfly.container import service

# A process-local write cut; callers still re-admit external sources before exposure.
_write_generations: dict[str, object] = {}
_settings_generations: dict[str, object] = {}

FIELDS: dict[str, dict[str, str]] = {
    "codex": {
        "model": "FORGE_CODEX_MODEL",
        "executable": "FORGE_CODEX_EXECUTABLE",
        "timeout": "FORGE_PROVIDER_TIMEOUT",
    },
    "claude": {
        "model": "FORGE_CLAUDE_MODEL",
        "executable": "FORGE_CLAUDE_EXECUTABLE",
        "timeout": "FORGE_PROVIDER_TIMEOUT",
    },
    "bridge": {
        "model": "FORGE_BRIDGE_MODEL",
        "endpoint": "FORGE_BRIDGE_URL",
        "token": "FORGE_BRIDGE_TOKEN",
        "completion_recovery": "FORGE_BRIDGE_COMPLETION_RECOVERY",
        "checkpoints": "FORGE_AGENTIC_CHECKPOINTS",
        "timeout": "FORGE_PROVIDER_TIMEOUT",
    },
    "agentic": {
        "model": "FORGE_AGENTIC_MODEL",
        "endpoint": "FORGE_AGENTIC_BASE_URL",
        "api_key": "FORGE_AGENTIC_API_KEY",
        "timeout": "FORGE_PROVIDER_TIMEOUT",
        "max_tokens": "FORGE_AGENTIC_MAX_TOKENS",
    },
}
DEFAULTS = {
    "FORGE_PROVIDER_TIMEOUT": "1800",
    "FORGE_AGENTIC_MAX_TOKENS": "16384",
    "FORGE_AGENTIC_MODEL": "openai-responses:gpt-6-luna",
    "FORGE_BRIDGE_MODEL": "bridge/default",
}
SECRETS = {"token", "api_key"}
OPTIONAL_SETTINGS = {"FORGE_BRIDGE_COMPLETION_RECOVERY", "FORGE_AGENTIC_CHECKPOINTS"}


def validate_completion_recovery(value: str) -> str:
    if value not in {"legacy", "required"}:
        raise ValueError("Completion recovery must be legacy or required")
    return value


def validate_checkpoints(settings: dict[str, str], *, task: str = "implementation", provider: str = "bridge") -> None:
    value = settings.get("FORGE_AGENTIC_CHECKPOINTS", "disabled")
    if value not in {"disabled", "required"}:
        raise ValueError("Agentic checkpoints must be disabled or required")
    if value == "required" and (
        provider != "bridge"
        or task != "implementation"
        or settings.get("FORGE_BRIDGE_COMPLETION_RECOVERY") != "required"
    ):
        raise ValueError("Required checkpoints support only Bridge implementation with required completion recovery")


class ProviderConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid")
    values: dict[str, str] = Field(default_factory=dict, max_length=10)
    clear_secrets: list[str] = Field(default_factory=list, max_length=2)


def normalize_provider(provider: str) -> str:
    provider = "agentic" if provider == "api" else provider
    if provider not in FIELDS:
        raise ValueError("Unknown coding provider")
    return provider


def validate_endpoint(value: str) -> str:
    parsed = urlsplit(value)
    local = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    if (parsed.scheme != "https" and not (parsed.scheme == "http" and local)) or not parsed.hostname:
        raise ValueError("Use HTTPS, or HTTP on a loopback address")
    if parsed.username or parsed.password or parsed.query or parsed.fragment or any(ord(c) < 32 for c in value):
        raise ValueError("Endpoint must not contain credentials, query parameters, fragments, or control characters")
    try:
        _port = parsed.port
    except ValueError as exc:
        raise ValueError("Invalid endpoint port") from exc
    return value.rstrip("/")


def public_settings(provider: str, settings: dict[str, str]) -> dict[str, str | bool]:
    result: dict[str, str | bool] = {
        key + "_configured" if key in SECRETS else key: bool(settings.get(env))
        if key in SECRETS
        else settings.get(env, "")
        for key, env in FIELDS[normalize_provider(provider)].items()
        if env not in OPTIONAL_SETTINGS or env in settings
    }
    if result.get("endpoint"):
        try:
            result["endpoint"] = validate_endpoint(str(result["endpoint"]))
        except ValueError:
            result["endpoint"] = ""
            result["endpoint_invalid"] = True
    return result


def redact_secrets(value: str, settings: dict[str, str]) -> str:
    from firefly_forge.secrets import credential_representations, sensitive_name

    for name, secret in settings.items():
        if secret and sensitive_name(name):
            for representation in sorted(credential_representations(secret), key=len, reverse=True):
                value = value.replace(representation, "[redacted]")
    return value


@service
class ConfigurationStore:
    def __init__(self, directory: Path, *, existing_only: bool = False) -> None:
        self.directory = directory / "configuration"
        self.revisions = self.directory / "revisions"
        if existing_only:
            try:
                for folder in (self.directory, self.revisions):
                    info = folder.lstat()
                    if (
                        folder.resolve() != folder
                        or not stat.S_ISDIR(info.st_mode)
                        or info.st_uid != os.getuid()
                        or stat.S_IMODE(info.st_mode) != 0o700
                        or getattr(info, "st_flags", 0) & 0x40000000
                    ):
                        raise ValueError("Offline maintenance requires existing private configuration directories")
            except FileNotFoundError:
                raise ValueError("Offline maintenance requires existing private configuration directories") from None
        else:
            self._prepare_directories()
        self.path = self.directory / "providers.json"
        self.lock = FileLock(str(self.directory / "settings.lock"))
        self._generation_key = str(self.directory.resolve())
        _write_generations.setdefault(self._generation_key, object())
        _settings_generations.setdefault(self._generation_key, object())

    def _prepare_directories(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.directory.is_symlink():
            raise ValueError("Configuration directory must be a real directory")
        self.directory.chmod(0o700)
        self.revisions.mkdir(exist_ok=True, mode=0o700)
        if self.revisions.is_symlink():
            raise ValueError("Revision directory must be a real directory")
        self.revisions.chmod(0o700)

    @property
    def write_generation(self) -> object:
        return _write_generations[self._generation_key]

    @property
    def settings_generation(self) -> object:
        return _settings_generations[self._generation_key]

    def _read(self, path: Path) -> dict[str, Any]:
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return {}
        except OSError as exc:
            raise ValueError("Configuration must be a private regular file") from exc
        with os.fdopen(fd) as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError("Configuration must be a private regular file")
            if info.st_mode & 0o077:
                raise ValueError("Configuration permissions must be 0600")
            result: dict[str, Any] = json.load(source)
            return result

    def _write(self, path: Path, data: dict[str, Any]) -> None:
        if path.exists() or path.is_symlink():
            self._read(path)
        fd, filename = tempfile.mkstemp(prefix=".settings-", dir=path.parent)
        try:
            with os.fdopen(fd, "w") as target:
                json.dump(data, target)
                target.flush()
                os.fsync(target.fileno())
            os.replace(filename, path)
            _write_generations[self._generation_key] = object()
            snapshots = {self.revisions, self.directory / "model-profile-revisions"}
            immutable_snapshot = (
                path.parent in snapshots
                and not path.parent.is_symlink()
                and path.parent.resolve().parent == self.directory.resolve()
            )
            if not immutable_snapshot:
                _settings_generations[self._generation_key] = object()
        finally:
            Path(filename).unlink(missing_ok=True)

    def resolve(self, provider: str) -> dict[str, str]:
        provider = normalize_provider(provider)
        with self.lock:
            persisted = self._read(self.path).get(provider, {})
        result = {
            env: os.environ.get(env, DEFAULTS.get(env, ""))
            for env in FIELDS[provider].values()
            if env not in OPTIONAL_SETTINGS or env in os.environ
        }
        if provider == "agentic" and not result["FORGE_AGENTIC_API_KEY"]:
            model = str(persisted.get("FORGE_AGENTIC_MODEL", result["FORGE_AGENTIC_MODEL"]))
            aliases = ["OPENAI_API_KEY"]
            if model.startswith("anthropic:"):
                aliases = ["ANTHROPIC_API_KEY"]
            elif model.startswith("google"):
                aliases = ["GOOGLE_API_KEY", "GEMINI_API_KEY"]
            result["FORGE_AGENTIC_API_KEY"] = next((os.environ[key] for key in aliases if os.environ.get(key)), "")
        return {**result, **persisted}

    def public(self, provider: str) -> dict[str, str | bool]:
        return public_settings(provider, self.resolve(provider))

    def update(self, provider: str, request: ProviderConfiguration) -> dict[str, str | bool]:
        provider = normalize_provider(provider)
        if set(request.values) - FIELDS[provider].keys() or set(request.clear_secrets) - (
            SECRETS & FIELDS[provider].keys()
        ):
            raise ValueError("Unknown configuration field")
        values: dict[str, str] = {}
        for key, value in request.values.items():
            if len(value) > 16000 or "\x00" in value:
                raise ValueError("Configuration value exceeds limits")
            if key in SECRETS and not value:
                continue
            if key == "checkpoints" and not value:
                continue
            if key == "completion_recovery":
                value = validate_completion_recovery(value)
            if key == "endpoint" and value:
                value = validate_endpoint(value)
            if key in {"model", "executable"} and any(ord(c) < 32 for c in value):
                raise ValueError("Model and executable must not contain control characters")
            if key in {"timeout", "max_tokens"} and (
                not value.isdecimal() or not 1 <= int(value) <= (7200 if key == "timeout" else 131072)
            ):
                raise ValueError("Timeout or token limit is outside the supported range")
            values[FIELDS[provider][key]] = value
        with self.lock:
            data = self._read(self.path)
            validate_checkpoints({**data.get(provider, {}), **values}, provider=normalize_provider(provider))
            data[provider] = {**data.get(provider, {}), **values}
            for key in request.clear_secrets:
                data[provider][FIELDS[provider][key]] = ""
            self._write(self.path, data)
        return self.public(provider)

    def snapshot(self, provider: str) -> dict[str, Any]:
        settings = self.resolve(provider)
        validate_checkpoints(settings, provider=normalize_provider(provider))
        if "FORGE_BRIDGE_COMPLETION_RECOVERY" in settings:
            validate_completion_recovery(settings["FORGE_BRIDGE_COMPLETION_RECOVERY"])
        endpoint_name = FIELDS[normalize_provider(provider)].get("endpoint")
        if endpoint_name and settings.get(endpoint_name):
            settings[endpoint_name] = validate_endpoint(settings[endpoint_name])
        revision = uuid.uuid4().hex
        self._write(self.revisions / f"{revision}.json", settings)
        return {"config_revision": revision, "execution_config": public_settings(provider, settings)}

    def load_snapshot(self, revision: str) -> dict[str, str]:
        if len(revision) != 32 or any(c not in "0123456789abcdef" for c in revision):
            raise ValueError("Invalid configuration revision")
        data = self._read(self.revisions / f"{revision}.json")
        if not data:
            raise ValueError("Run configuration revision is unavailable")
        return {str(key): str(value) for key, value in data.items()}
