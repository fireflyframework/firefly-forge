# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Private administrator transport authority; no plaintext or optional TLS fallback."""

from __future__ import annotations

import os
import ssl
import stat
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator


def absolute_path(value: Path) -> Path:
    if not value.is_absolute():
        raise ValueError("Controller TLS paths must be absolute administrator paths")
    return value


def private_tls_file(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        raise ValueError("TLS material must be a private regular administrator file") from None
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_size > 65536
        ):
            raise ValueError("TLS material must be a private regular administrator file")
    finally:
        os.close(descriptor)


class RunnerServerTLS(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    certificate: Path
    private_key: Path
    client_ca: Path

    _absolute = field_validator("certificate", "private_key", "client_ca")(absolute_path)

    def uvicorn_settings(self) -> dict[str, str | int]:
        for path in (self.certificate, self.private_key, self.client_ca):
            private_tls_file(path)
        return {
            "ssl_certfile": str(self.certificate),
            "ssl_keyfile": str(self.private_key),
            "ssl_ca_certs": str(self.client_ca),
            "ssl_cert_reqs": ssl.CERT_REQUIRED,
            "ssl_version": ssl.PROTOCOL_TLS_SERVER,
        }


class RunnerWorkerTransport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    active: bool = False
    endpoint: str = Field(max_length=512)
    certificate: Path
    private_key: Path
    ca: Path
    token: SecretStr = Field(min_length=32, max_length=4096)
    profile_id: str = Field(pattern=r"^[a-z][a-z0-9-]{0,63}$")
    profile_revision: str = Field(pattern=r"^[a-f0-9]{64}$")

    _absolute = field_validator("certificate", "private_key", "ca")(absolute_path)

    @field_validator("endpoint")
    @classmethod
    def authenticated_origin(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or any(ord(character) < 32 for character in value)
        ):
            raise ValueError("Runner controller requires a configured HTTPS origin without embedded authority")
        try:
            _port = parsed.port
        except ValueError:
            raise ValueError("Runner controller origin has an invalid port") from None
        return value.rstrip("/")

    def require_active(self) -> None:
        if not self.active:
            raise RuntimeError("Administrator runner transport is inactive")

    def client_context(self) -> ssl.SSLContext:
        self.require_active()
        for path in (self.certificate, self.private_key, self.ca):
            private_tls_file(path)
        context = ssl.create_default_context(cafile=str(self.ca))
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(str(self.certificate), str(self.private_key))
        return context
