# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Administrator input never supplies provider paths, scopes or credentials."""

import re
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

LOCAL_PROVIDER = "local-repository-tools"


def local_origin(value: str) -> str:
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme != "http"
            or parsed.hostname not in {"127.0.0.1", "::1"}
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path
            or parsed.query
            or parsed.fragment
            or parsed.port is None
            or not 1024 <= parsed.port <= 65535
        ):
            raise ValueError
        host = "[::1]" if parsed.hostname == "::1" else "127.0.0.1"
        canonical = f"http://{host}:{parsed.port}"
        if value != canonical or re.search(r"\s", value):
            raise ValueError
        return canonical
    except ValueError:
        raise ValueError("Use a canonical literal-loopback development address and an unprivileged port") from None


class OAuthProviderPolicyInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    enabled: bool
    local_origin: str = Field(min_length=1, max_length=100)
    expected_revision: int = Field(ge=0, le=2_147_483_646)
    request_id: str = Field(pattern=r"^[a-f0-9]{32}$")

    @field_validator("local_origin")
    @classmethod
    def canonical_origin(cls, value: str) -> str:
        return local_origin(value)
