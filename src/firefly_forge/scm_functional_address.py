# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Bounded explicit dialogue addresses, separate from the read-only status adapter."""

import re
from typing import Literal

from pydantic import Field

from firefly_forge.scm_authority_models import Digest
from firefly_forge.scm_conversation_models import AddressedStatusV1
from firefly_forge.scm_poll_models import PollContract


class AddressedFunctionalV1(PollContract):
    version: Literal[1] = 1
    action: Literal["assistant"] = "assistant"
    operation: Literal["turn"] = "turn"
    adapter: Literal["addressed_functional_v1"] = "addressed_functional_v1"
    message: str = Field(min_length=1, max_length=12000)
    source_body_sha256: Digest


def addressed_functional(text: str, status: AddressedStatusV1) -> AddressedFunctionalV1 | None:
    if status.intent != "clarify":
        return None
    direct = re.fullmatch(
        rf"(?:@{re.escape(status.address)}\s*[,.:]?\s*|{re.escape(status.address)},\s*)(.+)",
        text.strip(),
        re.IGNORECASE | re.DOTALL,
    )
    if direct is None or len(direct[1]) > 12000:
        return None
    return AddressedFunctionalV1(message=direct[1], source_body_sha256=status.source_body_sha256)
