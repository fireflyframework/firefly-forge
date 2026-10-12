# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Credential redaction and minimal environments for untrusted child code."""

from __future__ import annotations

import os
import re
from urllib.parse import unquote, urlsplit


def sensitive_name(name: str) -> bool:
    name = name.upper()
    return any(
        part in name
        for part in ("TOKEN", "PASSWORD", "SECRET", "API_KEY", "PRIVATE_KEY", "CREDENTIAL", "DATABASE_URL", "REDIS_URL")
    ) or name in {"PGPASSWORD", "PGPASSFILE"}


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
    for secret in sorted(secrets, key=len, reverse=True):
        value = value.replace(secret, "[redacted]")
    return re.sub(r"(?i)(authorization: ?bearer )[^\s]+", r"\1[redacted]", value)


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
