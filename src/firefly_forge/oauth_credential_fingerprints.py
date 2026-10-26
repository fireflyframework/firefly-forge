# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Purpose-bound irreversible credential history; prefixes only accelerate full matches."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import re
from collections.abc import Awaitable, Callable, Iterable, Iterator
from functools import lru_cache
from itertools import chain
from typing import Any, cast

from firefly_forge.credential_text_stream import admission_documents, canonical_chunks, raw_documents, text_chunks
from firefly_forge.secrets import credential_representations

Data = dict[str, Any]
MAX_HISTORY = 1024
MAX_REPRESENTATIONS = 16384


class OAuthCredentialMatcher:
    def __init__(self, encryption_key: str, schema: str) -> None:
        raw = base64.urlsafe_b64decode(encryption_key)
        if len(raw) != 32:
            raise ValueError("Invalid shared vault key")
        self.key = hmac.digest(raw, b"firefly-forge:oauth-redaction:v1:" + schema.encode(), "sha256")
        self.key_id = hashlib.sha256(self.key).hexdigest()

    def _digest(self, purpose: bytes, value: str) -> str:
        return hmac.digest(self.key, purpose + b"\0" + value.encode(), "sha256").hex()

    def _signature(self, packet: Data) -> str:
        return self._digest(b"packet", json.dumps(packet, sort_keys=True, separators=(",", ":")))

    def packet(
        self,
        row_id: str,
        kind: str,
        source_id: str,
        revision: int,
        secrets: list[str],
        connection_id: str | None = None,
    ) -> Data:
        representations = sorted({item for secret in secrets if secret for item in credential_representations(secret)})
        if len(representations) > 32 or any(len(item.encode()) > 262144 for item in representations):
            raise ValueError("OAuth credential history exceeds its bound")
        packet: Data = {
            "version": 1,
            "key_id": self.key_id,
            "row_id": row_id,
            "kind": kind,
            "source_id": source_id,
            "connection_id": connection_id or source_id,
            "revision": revision,
            "representations": [
                {
                    "length": len(item),
                    "prefix_length": min(16, len(item)),
                    "prefix": self._digest(b"prefix", item[:16]),
                    "match": self._digest(b"match", item),
                }
                for item in representations
            ],
        }
        return {**packet, "signature": self._signature(packet)}

    def _validated(self, packet: Data) -> list[Data]:
        try:
            if (
                not isinstance(packet, dict)
                or set(packet)
                != {
                    "version",
                    "key_id",
                    "row_id",
                    "kind",
                    "source_id",
                    "connection_id",
                    "revision",
                    "representations",
                    "signature",
                }
                or type(packet["version"]) is not int
                or packet["version"] != 1
                or packet["key_id"] != self.key_id
                or packet["kind"] not in {"registration", "grant"}
                or type(packet["revision"]) is not int
                or not 1 <= packet["revision"] <= 2_147_483_647
                or any(
                    not isinstance(packet[key], str) or not re.fullmatch(r"[a-f0-9]{32}", packet[key])
                    for key in ("row_id", "source_id", "connection_id")
                )
                or not isinstance(packet["signature"], str)
                or not re.fullmatch(r"[a-f0-9]{64}", packet["signature"])
                or not isinstance(packet["representations"], list)
                or len(packet["representations"]) > 32
            ):
                raise ValueError
            result = packet["representations"]
            for item in result:
                if (
                    not isinstance(item, dict)
                    or set(item) != {"length", "prefix_length", "prefix", "match"}
                    or type(item["length"]) is not int
                    or not 1 <= item["length"] <= 262144
                    or item["prefix_length"] != min(16, item["length"])
                    or any(
                        not isinstance(item[key], str) or not re.fullmatch(r"[a-f0-9]{64}", item[key])
                        for key in ("prefix", "match")
                    )
                ):
                    raise ValueError
            signed = {key: value for key, value in packet.items() if key != "signature"}
            if not hmac.compare_digest(packet["signature"], self._signature(signed)):
                raise ValueError
            return cast(list[Data], result)
        except (KeyError, TypeError, ValueError, OverflowError):
            raise ValueError("OAuth credential history cannot be authenticated") from None

    def contains(self, value: Any, packets: list[Data]) -> bool:
        scanner = _FingerprintScan(self, packets)
        return any(scanner.feed(chunk) for chunk in admission_documents(value))

    async def contains_async(
        self,
        value: Any,
        packets: list[Data],
        *,
        check_chunk: Callable[[str | None], bool] | None = None,
        check_canonical_chunk: Callable[[str | None], bool] | None = None,
        verify_current: Callable[[], Awaitable[None]] | None = None,
    ) -> bool:
        scanner = _FingerprintScan(self, packets)
        identity = hashlib.sha256()
        phases = [(raw_documents(value), check_chunk)]
        if isinstance(value, (dict, list, tuple)):
            phases.append((chain(canonical_chunks(value), (None,)), check_canonical_chunk))
        for documents, check in phases:
            for chunk in documents:
                _identity_chunk(identity, chunk)
                for matched in scanner.steps(chunk):
                    if matched:
                        return True
                    await asyncio.sleep(0)
                if check is not None and check(chunk):
                    return True
                # Total work belongs to the receiver's cancellation/deadline policy.
                # One text window, rather than the entire project, is synchronous.
                await asyncio.sleep(0)
        if verify_current is not None:
            await verify_current()
        final = hashlib.sha256()
        for chunk in admission_documents(value):
            _identity_chunk(final, chunk)
        if not hmac.compare_digest(identity.digest(), final.digest()):
            raise ValueError("Credential data changed during admission")
        return False

    def contains_source_patch(self, patch: Any, packets: list[Data]) -> bool:
        from firefly_forge.source_patch_admission import SourcePatch

        if type(patch) is not SourcePatch:
            raise TypeError("Credential admission requires the typed bounded source patch")
        return self._contains_strings([patch.text], packets)

    def _contains_strings(self, strings: Iterable[str], packets: list[Data]) -> bool:
        scanner = _FingerprintScan(self, packets)
        for value in strings:
            for chunk in text_chunks(value):
                if scanner.feed(chunk):
                    return True
            if scanner.feed(None):
                return True
        return False


def _identity_chunk(identity: Any, chunk: str | None) -> None:
    if chunk is None:
        identity.update(b"\0")
    else:
        encoded = chunk.encode("utf-8")
        identity.update(b"\1" + len(encoded).to_bytes(8, "big"))
        identity.update(encoded)


class _FingerprintScan:
    def __init__(self, matcher: OAuthCredentialMatcher, packets: list[Data]) -> None:
        if len(packets) > MAX_HISTORY:
            raise ValueError("OAuth credential history exceeds its bound")
        represented = [dict(item) for packet in packets for item in matcher._validated(packet)]
        if len(represented) > MAX_REPRESENTATIONS:
            raise ValueError("OAuth credential representations exceed their bound")
        self.matcher = matcher
        self.prefix_digest = lru_cache(maxsize=1024)(lambda value: matcher._digest(b"prefix", value))
        self.prefixes: dict[int, dict[str, list[Data]]] = {}
        for item in represented:
            self.prefixes.setdefault(item["prefix_length"], {}).setdefault(item["prefix"], []).append(item)
        self.length = max((item["length"] for item in represented), default=1)
        self.tail = ""

    def feed(self, chunk: str | None) -> bool:
        return any(self.steps(chunk))

    def steps(self, chunk: str | None) -> Iterator[bool]:
        value = self.tail + (chunk or "")
        work = 0
        for width, candidates in self.prefixes.items():
            # Delay starts until every authenticated representation can be checked.
            # This avoids rescanning overlap or retaining an unbounded candidate list.
            stop = max(0, len(value) - (width if chunk is None else self.length) + 1)
            for start in range(stop):
                prefix = self.prefix_digest(value[start : start + width])
                work += width
                for item in candidates.get(prefix, []):
                    if start + item["length"] <= len(value):
                        candidate = value[start : start + item["length"]]
                        work += len(candidate)
                        if hmac.compare_digest(self.matcher._digest(b"match", candidate), item["match"]):
                            yield True
                            return
                        if work >= 65536:
                            yield False
                            work = 0
                if work >= 65536:
                    yield False
                    work = 0
        self.tail = "" if chunk is None else value[-(self.length - 1) :] if self.length > 1 else ""
        yield False
