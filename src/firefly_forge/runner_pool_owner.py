# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Authenticated renewable runner ownership, independent of optional work duration."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import threading
import time
from collections.abc import Callable
from typing import Any


def _owner_body(execution_id: str, operation: str, sequence: int, challenge: str) -> bytes:
    if (
        type(execution_id) is not str
        or not re.fullmatch(r"[a-f0-9]{32}", execution_id)
        or type(operation) is not str
        or operation not in {"start", "renew"}
        or type(sequence) is not int
        or not 1 <= sequence <= 2**63 - 1
        or type(challenge) is not str
        or not re.fullmatch(r"[a-f0-9]{32}", challenge)
    ):
        raise ValueError("Invalid runner ownership message")
    return json.dumps(
        {
            "protocol": "forge-runner-owner-v2",
            "execution_id": execution_id,
            "operation": operation,
            "sequence": sequence,
            "challenge": challenge,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def owner_request(key: bytes, execution_id: str, operation: str, sequence: int, challenge: str) -> bytes:
    if type(key) is not bytes or len(key) != 32:
        raise ValueError("Invalid runner ownership key")
    body = _owner_body(execution_id, operation, sequence, challenge)
    return json.dumps({**json.loads(body), "signature": hmac.new(key, body, hashlib.sha256).hexdigest()}).encode()


def owner_reply(raw: bytes, execution_id: str, *, claim: bool = False, sequence: int | None = None) -> dict[str, Any]:
    try:
        if type(raw) is not bytes or not 0 < len(raw) <= 4096:
            raise ValueError()
        value = json.loads(raw)
        if (
            type(value) is not dict
            or value.get("protocol") != "forge-runner-owner-status-v2"
            or value.get("execution_id") != execution_id
            or type(value.get("sequence")) is not int
            or type(value.get("challenge")) is not str
            or not re.fullmatch(r"[a-f0-9]{32}", value["challenge"])
        ):
            raise ValueError()
        fields = {"protocol", "execution_id", "state", "sequence", "challenge"}
        if claim:
            if (
                sequence is not None
                or set(value) != fields | {"key"}
                or value["state"] != "waiting"
                or value["sequence"] != 0
                or type(value["key"]) is not str
                or not re.fullmatch(r"[a-f0-9]{64}", value["key"])
            ):
                raise ValueError()
        else:
            if (
                type(sequence) is not int
                or not 1 <= value["sequence"] <= sequence
                or value["state"] not in {"running", "draining", "drained"}
                or (value["state"] == "running" and value["sequence"] != sequence)
            ):
                raise ValueError()
            if value["state"] == "drained":
                fields.add("exit_code")
                if type(value.get("exit_code")) is not int:
                    raise ValueError()
            if set(value) != fields:
                raise ValueError()
    except (ValueError, TypeError, KeyError, UnicodeError):
        raise ValueError("Runner ownership acknowledgement is invalid") from None
    result: dict[str, Any] = value
    return result


class OwnerLiveness:
    def __init__(
        self,
        execution_id: str,
        key: bytes,
        grace: int,
        alarm: Callable[[int], Any],
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        _owner_body(execution_id, "start", 1, "0" * 32)
        if type(key) is not bytes or len(key) != 32 or type(grace) is not int or not 1 <= grace <= 120:
            raise ValueError("Invalid runner ownership liveness policy")
        self.execution_id, self.key, self.grace, self.alarm = execution_id, key, grace, alarm
        self.sequence, self.claimed, self.state = 0, False, "waiting"
        self.exit_code: int | None = None
        self.lock = threading.Lock()
        self.clock = clock
        self._challenge()

    def _challenge(self) -> None:
        self.challenge = secrets.token_hex(16)
        self.challenge_until = self.clock() + self.grace

    def _status(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "protocol": "forge-runner-owner-status-v2",
            "execution_id": self.execution_id,
            "state": self.state,
            "sequence": self.sequence,
            "challenge": self.challenge,
        }
        if self.state == "drained":
            result["exit_code"] = self.exit_code
        return result

    def handle(self, value: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            if type(value) is not dict or value.get("execution_id") != self.execution_id:
                raise ValueError("Runner ownership message selects another invocation")
            if value.get("operation") == "claim":
                if (
                    set(value) != {"protocol", "execution_id", "operation"}
                    or value["protocol"] != "forge-runner-owner-v2"
                    or self.claimed
                    or self.state != "waiting"
                ):
                    raise ValueError("Runner ownership key is unavailable")
                self.claimed = True
                self._challenge()
                return {**self._status(), "key": self.key.hex()}
            if (
                set(value) != {"protocol", "execution_id", "operation", "sequence", "challenge", "signature"}
                or value["protocol"] != "forge-runner-owner-v2"
                or type(value["signature"]) is not str
                or not re.fullmatch(r"[a-f0-9]{64}", value["signature"])
                or not self.claimed
            ):
                raise ValueError("Runner ownership message is not authenticated")
            body = _owner_body(self.execution_id, value["operation"], value["sequence"], value["challenge"])
            expected = hmac.new(self.key, body, hashlib.sha256).hexdigest()
            if not hmac.compare_digest(expected, value["signature"]):
                raise ValueError("Runner ownership signature is invalid")
            if value["challenge"] != self.challenge or value["sequence"] <= self.sequence:
                raise ValueError("Runner ownership message is stale")
            if self.state in {"draining", "drained"}:
                return self._status()
            if self.clock() >= self.challenge_until:
                raise ValueError("Runner ownership control challenge expired")
            if (value["operation"] == "start") != (self.state == "waiting"):
                raise ValueError("Runner ownership operation cannot restart project code")
            self.sequence = value["sequence"]
            self.state = "running"
            self._challenge()
            self.alarm(self.grace)
            return self._status()

    def begin_drain(self) -> None:
        with self.lock:
            if self.state != "running":
                raise ValueError("Runner drain does not follow owned execution")
            self.state = "draining"
            self.alarm(60)

    def finish(self, exit_code: int) -> None:
        with self.lock:
            if type(exit_code) is not int or self.state not in {"running", "draining"}:
                raise ValueError("Runner exit does not follow owned execution")
            if self.state == "running":
                self.alarm(60)
            self.exit_code, self.state = exit_code, "drained"
