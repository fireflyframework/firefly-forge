# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Purpose-bound irreversible credential history; prefixes only accelerate full matches."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import re
from typing import Any, cast

from firefly_forge.secrets import credential_representations

Data = dict[str, Any]
MAX_HISTORY = 1024
MAX_REPRESENTATIONS = 16384
MAX_TEXT_BYTES = 1_048_576
MAX_TOTAL_BYTES = 2_097_152
MAX_WORK = 2_097_152


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

    @staticmethod
    def _strings(value: Any) -> list[str]:
        strings: list[str] = []
        nodes = total = 0

        def capture(item: str) -> None:
            nonlocal total
            try:
                size = len(item.encode())
            except UnicodeError:
                raise ValueError("Credential admission requires valid Unicode text") from None
            total += size
            if size > MAX_TEXT_BYTES or total > MAX_TOTAL_BYTES:
                raise ValueError("Credential admission exceeds its text bound")
            strings.append(item)

        def visit(item: Any, depth: int) -> None:
            nonlocal nodes
            nodes += 1
            if nodes > 10000 or depth > 24:
                raise ValueError("Credential admission exceeds its structural bound")
            if isinstance(item, str):
                capture(item)
            elif isinstance(item, dict):
                for key, element in item.items():
                    visit(key, depth + 1)
                    visit(element, depth + 1)
            elif isinstance(item, (list, tuple)):
                for element in item:
                    visit(element, depth + 1)
            elif item is None or isinstance(item, bool):
                capture(json.dumps(item))
            elif type(item) is int:
                if item.bit_length() > 4096:
                    raise ValueError("Credential admission exceeds its numeric bound")
                capture(str(item))
            elif type(item) is float:
                if not math.isfinite(item):
                    raise ValueError("Credential admission requires finite JSON scalars")
                capture(json.dumps(item, allow_nan=False))
            else:
                raise ValueError("Credential admission requires bounded JSON data")

        visit(value, 0)
        if isinstance(value, (dict, list, tuple)):
            capture(json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")))
        return strings

    def contains(self, value: Any, packets: list[Data]) -> bool:
        strings = self._strings(value)
        if len(packets) > MAX_HISTORY:
            raise ValueError("OAuth credential history exceeds its bound")
        represented = [item for packet in packets for item in self._validated(packet)]
        if len(represented) > MAX_REPRESENTATIONS:
            raise ValueError("OAuth credential representations exceed their bound")
        prefixes: dict[int, dict[str, list[Data]]] = {}
        for item in represented:
            prefixes.setdefault(item["prefix_length"], {}).setdefault(item["prefix"], []).append(item)
        work = sum(max(0, len(value) - width + 1) for value in strings for width in prefixes)
        if work > MAX_WORK:
            raise ValueError("Credential admission exceeds its matching work bound")
        hashed_bytes = 0
        for value in strings:
            for width, candidates in prefixes.items():
                for start in range(max(0, len(value) - width + 1)):
                    prefix = self._digest(b"prefix", value[start : start + width])
                    for item in candidates.get(prefix, []):
                        if start + item["length"] <= len(value):
                            candidate = value[start : start + item["length"]]
                            hashed_bytes += len(candidate.encode())
                            if hashed_bytes > 16_777_216:
                                raise ValueError("Credential admission exceeds its full matching bound")
                            if hmac.compare_digest(self._digest(b"match", candidate), item["match"]):
                                return True
        return False
