# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Exact raw-body signature verification for scoped SCM delegation."""

import base64
import binascii
import hashlib
import hmac
import re
from collections.abc import Mapping


def verify_gitlab_signing_token(
    raw: bytes, headers: Mapping[str, str], signing_token: str, *, current_time: float
) -> bool:
    """Verify Standard Webhooks without falling back to a shared token."""
    normalized = {key.lower(): value for key, value in headers.items()}
    message_id = normalized.get("webhook-id", "")
    timestamp = normalized.get("webhook-timestamp", "")
    signatures = normalized.get("webhook-signature", "")
    if (
        len(raw) > 100000
        or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,200}", message_id)
        or not re.fullmatch(r"[1-9][0-9]{0,11}", timestamp)
        or abs(current_time - int(timestamp)) > 300
        or len(signatures) > 2048
        or not signing_token.startswith("whsec_")
        or len(signing_token) > 100
    ):
        return False
    try:
        key = base64.b64decode(signing_token[6:], validate=True)
    except (binascii.Error, ValueError):
        return False
    if len(key) != 32:
        return False
    message = message_id.encode() + b"." + timestamp.encode() + b"." + raw
    expected = "v1," + base64.b64encode(hmac.new(key, message, hashlib.sha256).digest()).decode()
    return any(hmac.compare_digest(expected.encode(), item.encode()) for item in signatures.split(" "))
