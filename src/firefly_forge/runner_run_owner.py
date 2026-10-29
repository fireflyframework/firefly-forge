# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Versioned PID1 owner frames bind each renewed alarm to a fresh run grant."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import math
import re
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from firefly_forge.runner_pool_owner import OwnerLiveness, owner_reply

if TYPE_CHECKING:
    from firefly_forge.runner_run_liveness import RunLease


def _run_owner_body(
    execution_id: str,
    binding_digest: str,
    operation: str,
    sequence: int,
    challenge: str,
    grant_sequence: int,
    ttl_seconds: int | None,
) -> bytes:
    if (
        type(execution_id) is not str
        or not re.fullmatch(r"[a-f0-9]{32}", execution_id)
        or type(binding_digest) is not str
        or not re.fullmatch(r"[a-f0-9]{64}", binding_digest)
        or type(operation) is not str
        or operation not in {"start", "renew", "observe"}
        or type(sequence) is not int
        or not 1 <= sequence <= 2**63 - 1
        or type(challenge) is not str
        or not re.fullmatch(r"[a-f0-9]{32}", challenge)
        or type(grant_sequence) is not int
        or not 0 <= grant_sequence <= 2**63 - 1
        or (
            ttl_seconds is not None
            if operation == "observe"
            else type(ttl_seconds) is not int or not 1 <= ttl_seconds <= 120
        )
    ):
        raise ValueError("Invalid run-bound owner message")
    return json.dumps(
        {
            "protocol": "forge-runner-owner-v3",
            "execution_id": execution_id,
            "binding_digest": binding_digest,
            "operation": operation,
            "sequence": sequence,
            "challenge": challenge,
            "grant_sequence": grant_sequence,
            "ttl_seconds": ttl_seconds,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def run_owner_request(
    key: bytes,
    execution_id: str,
    binding_digest: str,
    operation: str,
    sequence: int,
    challenge: str,
    grant_sequence: int,
    ttl_seconds: int | None,
) -> bytes:
    if type(key) is not bytes or len(key) != 32:
        raise ValueError("Invalid run-bound owner key")
    body = _run_owner_body(execution_id, binding_digest, operation, sequence, challenge, grant_sequence, ttl_seconds)
    return json.dumps({**json.loads(body), "signature": hmac.new(key, body, hashlib.sha256).hexdigest()}).encode()


def run_owner_reply(
    raw: bytes,
    execution_id: str,
    binding_digest: str,
    *,
    claim: bool = False,
    sequence: int | None = None,
    grant_sequence: int | None = None,
) -> dict[str, Any]:
    try:
        if type(raw) is not bytes or not 0 < len(raw) <= 4096:
            raise ValueError()
        value = json.loads(raw)
        if (
            type(value) is not dict
            or value.get("protocol") != "forge-runner-owner-status-v3"
            or value.get("binding_digest") != binding_digest
            or type(value.get("grant_sequence")) is not int
            or (
                value["grant_sequence"] != -1
                if claim
                else type(grant_sequence) is not int or not 0 <= value["grant_sequence"] <= grant_sequence
            )
            or not claim
            and value.get("state") == "running"
            and value["grant_sequence"] != grant_sequence
        ):
            raise ValueError()
        normalized = {key: item for key, item in value.items() if key not in {"binding_digest", "grant_sequence"}}
        normalized["protocol"] = "forge-runner-owner-status-v2"
        owner_reply(json.dumps(normalized).encode(), execution_id, claim=claim, sequence=sequence)
    except (ValueError, TypeError, KeyError, UnicodeError):
        raise ValueError("Run-bound owner acknowledgement is invalid") from None
    result: dict[str, Any] = value
    return result


class RunOwnerLiveness(OwnerLiveness):
    def __init__(
        self,
        execution_id: str,
        binding_digest: str,
        key: bytes,
        grace: int,
        alarm: Callable[[int], Any],
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if type(binding_digest) is not str or not re.fullmatch(r"[a-f0-9]{64}", binding_digest):
            raise ValueError("Invalid run owner binding")
        super().__init__(execution_id, key, grace, alarm, clock=clock)
        self.binding_digest, self.grant_sequence = binding_digest, -1
        self.live_until = self.clock() + self.grace

    def _challenge(self) -> None:
        super()._challenge()
        self.challenge_issued_at = self.clock()

    def _status(self) -> dict[str, Any]:
        return {
            **super()._status(),
            "protocol": "forge-runner-owner-status-v3",
            "binding_digest": self.binding_digest,
            "grant_sequence": self.grant_sequence,
        }

    def handle(self, value: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            if (
                type(value) is not dict
                or value.get("protocol") != "forge-runner-owner-v3"
                or value.get("execution_id") != self.execution_id
                or value.get("binding_digest") != self.binding_digest
            ):
                raise ValueError("Run owner message selects another binding")
            if value.get("operation") == "claim":
                if (
                    set(value) != {"protocol", "execution_id", "binding_digest", "operation"}
                    or self.claimed
                    or self.state != "waiting"
                ):
                    raise ValueError("Run owner key is unavailable")
                self.claimed = True
                self._challenge()
                return {**self._status(), "key": self.key.hex()}
            if (
                set(value)
                != {
                    "protocol",
                    "execution_id",
                    "binding_digest",
                    "operation",
                    "sequence",
                    "challenge",
                    "grant_sequence",
                    "ttl_seconds",
                    "signature",
                }
                or not self.claimed
                or type(value["signature"]) is not str
                or not re.fullmatch(r"[a-f0-9]{64}", value["signature"])
            ):
                raise ValueError("Run owner message is not authenticated")
            body = _run_owner_body(
                self.execution_id,
                self.binding_digest,
                value["operation"],
                value["sequence"],
                value["challenge"],
                value["grant_sequence"],
                value["ttl_seconds"],
            )
            if not hmac.compare_digest(hmac.new(self.key, body, hashlib.sha256).hexdigest(), value["signature"]):
                raise ValueError("Run owner signature is invalid")
            if value["sequence"] != self.sequence + 1 or value["challenge"] != self.challenge:
                raise ValueError("Run owner message is stale")
            if self.state in {"draining", "drained"}:
                self.sequence = value["sequence"]
                self._challenge()
                return self._status()
            if self.clock() >= self.live_until:
                raise ValueError("Run owner grant expired")
            if value["operation"] == "observe":
                if self.state != "running" or value["grant_sequence"] != self.grant_sequence:
                    raise ValueError("Run owner status selects another grant")
            else:
                if (
                    (value["operation"] == "start") != (self.state == "waiting")
                    or value["grant_sequence"] <= self.grant_sequence
                    or value["ttl_seconds"] > self.grace
                ):
                    raise ValueError("Run owner grant cannot restart or extend this authority")
                deadline = self.challenge_issued_at + value["ttl_seconds"]
                remaining = deadline - self.clock()
                if remaining <= 0:
                    raise ValueError("Run owner grant expired during control delivery")
                self.grant_sequence = value["grant_sequence"]
                self.live_until = deadline
                self.alarm(math.ceil(remaining))
                self.state = "running"
            self.sequence = value["sequence"]
            self._challenge()
            return self._status()


class RunOwnerChannel:
    def __init__(
        self,
        execution_id: str,
        lease: RunLease,
        exchange: Callable[[Callable[[], bytes]], Awaitable[bytes]],
    ) -> None:
        self.execution_id, self.lease, self.exchange = execution_id, lease, exchange
        self.key: bytes | None = None
        self.challenge: str | None = None
        self.sequence, self.grant_sequence, self.closed = 0, -1, False
        self.lock = asyncio.Lock()

    async def start(self) -> dict[str, Any]:
        async with self.lock:
            if self.closed or self.key is not None:
                raise ValueError("Run owner channel cannot be claimed again")
            try:
                self.lease.remaining()
                raw = await self.exchange(
                    lambda: json.dumps(
                        {
                            "protocol": "forge-runner-owner-v3",
                            "execution_id": self.execution_id,
                            "binding_digest": self.lease.invocation_digest,
                            "operation": "claim",
                        }
                    ).encode()
                )
                claimed = run_owner_reply(raw, self.execution_id, self.lease.invocation_digest, claim=True)
                self.key, self.challenge = bytes.fromhex(claimed["key"]), claimed["challenge"]
                return await self._frame("start")
            except BaseException:
                self.closed = True
                raise

    async def _frame(self, operation: str) -> dict[str, Any]:
        if self.closed or self.key is None or self.challenge is None:
            raise ValueError("Run owner channel requires reconciliation")
        self.lease.remaining()
        self.sequence += 1
        sent_grant: int | None = None

        def materialize() -> bytes:
            nonlocal sent_grant
            if sent_grant is not None:
                raise ValueError("Run owner control frame cannot be retried")
            sent_grant = self.grant_sequence if operation == "observe" else self.lease.sequence
            ttl = None if operation == "observe" else math.ceil(self.lease.remaining())
            assert self.key is not None and self.challenge is not None
            return run_owner_request(
                self.key,
                self.execution_id,
                self.lease.invocation_digest,
                operation,
                self.sequence,
                self.challenge,
                sent_grant,
                ttl,
            )

        try:
            raw = await self.exchange(materialize)
            if sent_grant is None:
                raise ValueError("Run owner transport did not bind its frame")
            reply = run_owner_reply(
                raw,
                self.execution_id,
                self.lease.invocation_digest,
                sequence=self.sequence,
                grant_sequence=sent_grant,
            )
            self.challenge, self.grant_sequence = reply["challenge"], reply["grant_sequence"]
            return reply
        except BaseException:
            self.closed = True
            raise

    async def status(self) -> dict[str, Any]:
        async with self.lock:
            observed = await self._frame("observe")
            if observed["state"] == "running" and self.lease.sequence > self.grant_sequence:
                return await self._frame("renew")
            return observed
