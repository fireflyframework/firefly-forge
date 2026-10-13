# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Credential redaction and minimal environments for untrusted child code."""

from __future__ import annotations

import json
import os
import re
from typing import Any
from urllib.parse import unquote, urlsplit


def sensitive_name(name: str) -> bool:
    name = name.upper()
    if name == "FORGE_AGENTIC_MAX_TOKENS" or name.endswith("_FORGE_AGENTIC_MAX_TOKENS"):
        return False
    return any(
        part in name
        for part in ("TOKEN", "PASSWORD", "SECRET", "API_KEY", "PRIVATE_KEY", "CREDENTIAL", "DATABASE_URL", "REDIS_URL")
    ) or name in {
        "PGPASSWORD",
        "PGPASSFILE",
        "SSH_AUTH_SOCK",
        "DOCKER_CONFIG",
        "KUBECONFIG",
        "ANSIBLE_PRIVATE_KEY_FILE",
        "SSH_KNOWN_HOSTS_FILE",
    }


def credential_representations(value: str) -> set[str]:
    representations = {value}
    for _ in range(2):
        representations.update(
            json.dumps(item, ensure_ascii=ascii_mode)[1:-1]
            for item in tuple(representations)
            for ascii_mode in (True, False)
        )
    return representations


def redact_environment(value: str) -> str:
    secrets = set()
    for name, item in os.environ.items():
        if item and sensitive_name(name):
            secrets.add(item)
        if "://" in item:
            try:
                password = urlsplit(item).password
            except ValueError:
                password = None
            if password:
                secrets.update([password, unquote(password)])
    represented = {representation for secret in secrets for representation in credential_representations(secret)}
    for secret in sorted(represented, key=len, reverse=True):
        value = value.replace(secret, "[redacted]")
    return re.sub(r"(?i)(authorization: ?bearer )[^\s]+", r"\1[redacted]", value)


def contains_known_credential(value: Any, registry: dict[str, str] | None = None) -> bool:
    if isinstance(value, str):
        if redact_environment(value) != value:
            return True
        return any(
            secret
            and sensitive_name(name)
            and any(representation in value for representation in credential_representations(secret))
            for name, secret in (registry or {}).items()
        )
    if isinstance(value, dict):
        return any(
            contains_known_credential(key, registry) or contains_known_credential(item, registry)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(contains_known_credential(item, registry) for item in value)
    return False


def runtime_environment(overrides: dict[str, str] | None = None) -> dict[str, str]:
    prefixes = (
        "FORGE_",
        "PYFLY_",
        "OPENAI_",
        "ANTHROPIC_",
        "GOOGLE_",
        "GEMINI_",
        "GITHUB_",
        "GITLAB_",
        "BITBUCKET_",
        "AZURE_",
        "AWS_",
        "ARM_",
        "DIGITALOCEAN_",
        "GCP_",
        "GCE_",
        "CLOUDSDK_",
        "CLAUDE_",
        "GH_",
        "PG",
    )
    result = {
        key: value
        for key, value in os.environ.items()
        if not sensitive_name(key) and not key.upper().startswith(prefixes)
    }
    # Proxy URLs sometimes contain credentials even though their variable names do not.
    for key, value in list(result.items()):
        if "://" in value:
            try:
                if urlsplit(value).password:
                    result.pop(key)
            except ValueError:
                result.pop(key)
    return {**result, "GIT_TERMINAL_PROMPT": "0", **(overrides or {})}
