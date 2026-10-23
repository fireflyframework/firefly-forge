# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Private provider cooldowns, independent of approved configuration revisions."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
import stat
import tempfile
from contextlib import suppress
from email.utils import parsedate_to_datetime
from pathlib import Path
from time import time
from typing import Any

import httpx
from filelock import AsyncFileLock, Timeout

from firefly_forge.configuration import ConfigurationStore


class ProviderRequestDeferred(ValueError):
    """This request was held locally before any HTTP dispatch."""

    def __init__(self, message: str, *, retry_at: float | None = None) -> None:
        super().__init__(message)
        self.retry_at = time() + 1 if retry_at is None else retry_at
        if type(self.retry_at) not in (int, float) or not math.isfinite(self.retry_at) or self.retry_at <= 0:
            raise ValueError("A deferred provider request needs a finite retry deadline")

    def evidence(self, intent: dict[str, Any] | None) -> dict[str, Any]:
        return {
            "kind": "provider_request_deferred_v1",
            "outcome": "not_dispatched",
            "retry_at": self.retry_at,
            "intent_sha256": hashlib.sha256(
                json.dumps(intent, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
        }


class DeferredFeedbackChanged(ValueError):
    """A saved nondispatched draft no longer matches current publication facts."""


def retained_deferred(data: dict[str, Any]) -> dict[str, Any] | None:
    evidence = data.get("feedback_deferred")
    if evidence is None:
        return None
    if (
        not isinstance(evidence, dict)
        or set(evidence) != {"kind", "outcome", "retry_at", "intent_sha256"}
        or evidence["kind"] != "provider_request_deferred_v1"
        or evidence["outcome"] != "not_dispatched"
        or type(evidence["retry_at"]) not in (int, float)
        or not math.isfinite(evidence["retry_at"])
        or evidence["retry_at"] <= 0
        or evidence["intent_sha256"]
        != ProviderRequestDeferred("Retained local hold", retry_at=evidence["retry_at"]).evidence(
            data.get("feedback_intent")
        )["intent_sha256"]
    ):
        raise ValueError("The retained provider nondispatch evidence changed")
    return evidence


class ProviderBackoff:
    def __init__(self, directory: Path, scope: list[str]) -> None:
        root = directory / "scm-runtime"
        if root.is_symlink():
            raise ValueError("Provider runtime directory must be a real directory")
        self.directory = ConfigurationStore(root).directory
        key = hashlib.sha256(json.dumps(scope, separators=(",", ":")).encode()).hexdigest()
        self.path = self.directory / (key + ".json")
        if not self.path.exists() and len(list(self.directory.glob("*.json"))) >= 32:
            raise ValueError("Provider cooldown scope limit reached")
        self.lock = AsyncFileLock(str(self.directory / (key + ".lock")), timeout=0, mode=0o600)

    def _read(self) -> dict[str, Any]:
        try:
            descriptor = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return {}
        with os.fdopen(descriptor, "rb") as stream:
            metadata = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
                or metadata.st_uid != os.getuid()
                or stat.S_IMODE(metadata.st_mode) != 0o600
            ):
                raise ValueError("Provider cooldown must be an owner-private file")
            raw = stream.read(16385)
        if len(raw) > 16384:
            raise ValueError("Provider cooldown exceeds its size limit")
        value = json.loads(raw)
        if (
            not isinstance(value, dict)
            or set(value) != {"version", "until", "failures", "evidence"}
            or type(value["version"]) is not int
            or value["version"] != 1
            or type(value["until"]) not in (int, float)
            or not math.isfinite(value["until"])
            or type(value["failures"]) is not int
            or not 1 <= value["failures"] <= 16
            or not isinstance(value["evidence"], dict)
        ):
            raise ValueError("Provider cooldown state is invalid")
        return value

    async def __aenter__(self) -> ProviderBackoff:
        try:
            await self.lock.acquire()
        except Timeout:
            raise ProviderRequestDeferred("A provider request is already in progress") from None
        except BaseException:
            await asyncio.shield(self.lock.release(force=True))
            raise
        try:
            self.previous = self._read()
            if self.previous.get("until", 0) > time():
                raise ProviderRequestDeferred(
                    "Provider cooldown is active; wait before trying again", retry_at=self.previous["until"]
                )
        except BaseException:
            await asyncio.shield(self.lock.release(force=True))
            raise
        return self

    async def __aexit__(self, *args: Any) -> None:
        await asyncio.shield(self.lock.release(force=True))

    def observe(self, response: httpx.Response) -> None:
        if response.status_code not in {403, 429}:
            return
        now = time()
        failures = min(16, self.previous.get("failures", 0) + 1)
        delay = min(3600, 60 * 2 ** (failures - 1))

        def numeric(name: str) -> int | None:
            text = response.headers.get(name, "")
            return int(text) if re.fullmatch(r"[0-9]{1,18}", text) else None

        retry = numeric("retry-after")
        if retry is None and response.headers.get("retry-after"):
            with suppress(ValueError, TypeError, OverflowError):
                retry = max(0, math.ceil(parsedate_to_datetime(response.headers["retry-after"]).timestamp() - now))
        reset, remaining = numeric("x-ratelimit-reset"), numeric("x-ratelimit-remaining")
        if retry is not None:
            delay = max(delay, retry)
        if remaining == 0 and reset is not None:
            delay = max(delay, math.ceil(reset - now))
        request_id = response.headers.get("x-github-request-id", response.headers.get("x-request-id", ""))
        evidence = {
            "status": response.status_code,
            "observed_at": now,
            "retry_after_seconds": retry,
            "limit": numeric("x-ratelimit-limit"),
            "remaining": remaining,
            "reset": reset,
            "request_id": request_id if re.fullmatch(r"[A-Za-z0-9:._-]{1,128}", request_id) else None,
            "request_sha256": hashlib.sha256(str(response.request.url).encode()).hexdigest(),
            "credential_sha256": hashlib.sha256(response.request.headers.get("authorization", "").encode()).hexdigest(),
        }
        value = {"version": 1, "until": now + min(86400, delay), "failures": failures, "evidence": evidence}
        raw = json.dumps(value, separators=(",", ":")).encode()
        descriptor, temporary = tempfile.mkstemp(prefix=".backoff-", dir=self.directory)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            directory = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
