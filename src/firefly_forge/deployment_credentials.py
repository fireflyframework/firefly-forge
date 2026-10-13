# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Private credential profiles granted only to explicit deployment commands."""

from __future__ import annotations

import json
import os
import re
import stat
import uuid
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from pyfly.container import service

from firefly_forge.configuration import ConfigurationStore

ALLOWED_KEYS = {
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_REGION",
    "AWS_DEFAULT_REGION",
    "ARM_CLIENT_ID",
    "ARM_CLIENT_SECRET",
    "ARM_TENANT_ID",
    "ARM_SUBSCRIPTION_ID",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "GOOGLE_CLOUD_PROJECT",
    "DIGITALOCEAN_TOKEN",
    "SSH_AUTH_SOCK",
    "DOCKER_CONFIG",
    "ANSIBLE_PRIVATE_KEY_FILE",
    "SSH_KNOWN_HOSTS_FILE",
}


class CredentialInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    values: dict[str, str] = Field(default_factory=dict, max_length=20)
    clear_keys: list[str] = Field(default_factory=list, max_length=20)


@service
class DeploymentCredentials:
    def __init__(self, configuration: ConfigurationStore) -> None:
        self.configuration = configuration
        self.directory = configuration.directory / "deployment-credentials"
        self.directory.mkdir(exist_ok=True, mode=0o700)
        if self.directory.is_symlink():
            raise ValueError("Deployment credentials require a private regular directory")
        self.directory.chmod(0o700)
        self.profiles = self.directory / "profiles.json"

    def _id(self, value: str) -> None:
        if not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", value):
            raise ValueError("Invalid deployment credential profile")

    def catalog(self) -> list[dict[str, Any]]:
        with self.configuration.lock:
            data = self.configuration._read(self.profiles)
        return [{"id": key, "configured_keys": sorted(values)} for key, values in data.items()]

    def configure(self, profile: str, request: CredentialInput) -> dict[str, Any]:
        self._id(profile)
        if (set(request.values) | set(request.clear_keys)) - ALLOWED_KEYS:
            raise ValueError("Credential profile contains an unsupported deployment variable")
        for value in request.values.values():
            if len(value) > 32000 or "\x00" in value:
                raise ValueError("Invalid deployment credential value")
        with self.configuration.lock:
            data = self.configuration._read(self.profiles)
            values = {**data.get(profile, {}), **{key: value for key, value in request.values.items() if value}}
            for key in request.clear_keys:
                values.pop(key, None)
            data[profile] = values
            self.configuration._write(self.profiles, data)
        return {"id": profile, "configured_keys": sorted(values)}

    def snapshot(self, profile: str | None) -> str | None:
        if profile is None:
            return None
        self._id(profile)
        with self.configuration.lock:
            values = self.configuration._read(self.profiles).get(profile)
            if values is None:
                raise ValueError("Configure the deployment credential profile before queueing")
            revision = uuid.uuid4().hex
            values = dict(values)
            for key in ("GOOGLE_APPLICATION_CREDENTIALS", "ANSIBLE_PRIVATE_KEY_FILE", "SSH_KNOWN_HOSTS_FILE"):
                if values.get(key):
                    values[key] = str(
                        self._freeze_file(
                            Path(values[key]), revision + "-" + key.lower(), public=key == "SSH_KNOWN_HOSTS_FILE"
                        )
                    )
            if values.get("DOCKER_CONFIG"):
                directory = Path(values["DOCKER_CONFIG"])
                if not directory.is_absolute() or directory.is_symlink() or not directory.is_dir():
                    raise ValueError("Docker credentials require an explicit regular configuration directory")
                frozen = self.directory / (revision + "-docker")
                frozen.mkdir(mode=0o700)
                copied = self._freeze_file(directory / "config.json", revision + "-docker-config")
                (frozen / "config.json").write_bytes(copied.read_bytes())
                (frozen / "config.json").chmod(0o600)
                values["DOCKER_CONFIG"] = str(frozen)
            self.configuration._write(self.directory / (revision + ".json"), values)
        return revision

    def _freeze_file(self, source: Path, name: str, *, public: bool = False) -> Path:
        if not source.is_absolute():
            raise ValueError("Deployment credential files require absolute paths")
        try:
            descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
        except OSError as exc:
            raise ValueError("Deployment credential file must be a readable regular file") from exc
        with os.fdopen(descriptor, "rb") as file:
            info = os.fstat(file.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or info.st_size > 2_000_000
                or (not public and info.st_mode & 0o077)
            ):
                raise ValueError("Deployment credential file must be private, regular and bounded")
            payload = file.read(2_000_001)
        if len(payload) > 2_000_000:
            raise ValueError("Deployment credential file exceeds 2 MB")
        destination = self.directory / (name + ".credential")
        with destination.open("xb") as file:
            destination.chmod(0o600)
            file.write(payload)
            file.flush()
            os.fsync(file.fileno())
        return destination

    def load(self, revision: str | None) -> dict[str, str]:
        if revision is None:
            return {}
        if not re.fullmatch(r"[a-f0-9]{32}", revision):
            raise ValueError("Invalid deployment credential revision")
        path = self.directory / (revision + ".json")
        if not path.exists():
            raise ValueError("Deployment credential revision is unavailable")
        values = self.configuration._read(path)
        if set(values) - ALLOWED_KEYS:
            raise ValueError("Invalid deployment credential revision contents")
        return {str(key): str(value) for key, value in values.items()}

    def redaction_settings(self) -> dict[str, str]:
        result: dict[str, str] = {}
        for file in self.directory.glob("*.json"):
            values = self.configuration._read(file)
            if file == self.profiles:
                for profile, items in values.items():
                    result.update({f"DEPLOYMENT_SECRET_{profile}_{key}": value for key, value in items.items()})
            else:
                result.update({f"DEPLOYMENT_SECRET_{file.stem}_{key}": value for key, value in values.items()})
        for file in self.directory.glob("*.credential"):
            value = file.read_text(errors="replace")
            if file.name.endswith("ansible_private_key_file.credential"):
                result["DEPLOYMENT_SECRET_" + file.stem] = value
            else:
                try:
                    content = json.loads(value)
                except ValueError:
                    continue

                def secrets(item: Any, path: str = "", prefix: str = file.stem) -> None:
                    if isinstance(item, dict):
                        for key, nested in item.items():
                            secrets(nested, path + "_" + key, prefix)
                    elif isinstance(item, str) and any(
                        name in path.lower() for name in ("private_key", "token", "auth", "password")
                    ):
                        result["DEPLOYMENT_SECRET_" + prefix + path] = item
                        result["DEPLOYMENT_SECRET_ESCAPED_" + prefix + path] = json.dumps(item)[1:-1]

                secrets(content)
        return result
