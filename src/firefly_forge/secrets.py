# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Credential redaction and minimal environments for untrusted child code."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any
from urllib.parse import unquote, urlsplit

from firefly_forge.credential_text_stream import raw_documents
from firefly_forge.personal_token_format import PERSONAL_TOKEN_LENGTH, PERSONAL_TOKEN_PATTERN

_ephemeral: ContextVar[tuple[str, ...]] = ContextVar("forge_ephemeral_credentials", default=())


@contextmanager
def ephemeral_credentials(values: dict[str, str]) -> Iterator[None]:
    token = _ephemeral.set(
        (*_ephemeral.get(), *(value for name, value in values.items() if value and sensitive_name(name)))
    )
    try:
        yield
    finally:
        _ephemeral.reset(token)


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
    secrets = set(_ephemeral.get())
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
    value = PERSONAL_TOKEN_PATTERN.sub("[redacted]", value)
    return re.sub(r"(?i)(authorization: ?bearer )[^\s]+", r"\1[redacted]", value)


def credential_values(registry: dict[str, str] | None = None) -> tuple[str, ...]:
    values = set(_ephemeral.get())
    for name, item in os.environ.items():
        if item and sensitive_name(name):
            values.add(item)
        if "://" in item:
            try:
                password = urlsplit(item).password
            except ValueError:
                password = None
            if password:
                values.update((password, unquote(password)))
    values.update(value for name, value in (registry or {}).items() if value and sensitive_name(name))
    return tuple(sorted(values))


class KnownCredentialScan:
    def __init__(self, credentials: tuple[str, ...], *, bearer: bool = True) -> None:
        self.bearer = bearer
        self.represented = {item for value in credentials for item in credential_representations(value)}
        self.overlap = (
            max(
                (
                    PERSONAL_TOKEN_LENGTH,
                    len("authorization: bearer [redacted]") + 1,
                    *(len(item) for item in self.represented),
                )
            )
            - 1
        )
        self.tail = ""

    def feed(self, chunk: str | None) -> bool:
        value = self.tail + (chunk or "")
        if PERSONAL_TOKEN_PATTERN.search(value) or any(item in value for item in self.represented):
            return True
        for match in re.finditer(r"(?i)(authorization: ?bearer )([^\s]+)", value) if self.bearer else ():
            token = match.group(2)
            if token != "[redacted]" and not (
                chunk is not None and match.end() == len(value) and "[redacted]".startswith(token)
            ):
                return True
        self.tail = "" if chunk is None else value[-self.overlap :]
        return False


def contains_known_credential(value: Any, registry: dict[str, str] | None = None) -> bool:
    scanner = KnownCredentialScan(credential_values(registry))
    return any(scanner.feed(chunk) for chunk in raw_documents(value, strict=False))


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
