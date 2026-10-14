# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Standalone administrator-configured native controller with mandatory mutual TLS."""

from __future__ import annotations

import ipaddress
from pathlib import Path
from typing import Any

import click
import uvicorn
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator
from pyfly.core.config import Config

from firefly_forge.runner_controller import RunnerControllerSettings, create_runner_application
from firefly_forge.runner_controller_config import RunnerServerTLS, private_tls_file
from firefly_forge.runner_pool_dispatch import RunnerDockerAuthority


class RunnerControllerAdmin(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    authority: RunnerDockerAuthority
    runner_schema: str
    factory_schema: str
    tls: RunnerServerTLS
    token: SecretStr = Field(min_length=32, max_length=4096)
    active: bool = False
    git_binary: Path | None = None
    host: str = "127.0.0.1"
    port: int = Field(default=9917, ge=1024, le=65535, strict=True)

    @field_validator("host")
    @classmethod
    def explicit_address(cls, value: str) -> str:
        return str(ipaddress.ip_address(value))

    def settings(self) -> RunnerControllerSettings:
        return RunnerControllerSettings(
            authority=self.authority,
            runner_schema=self.runner_schema,
            factory_schema=self.factory_schema,
            token=self.token,
            tls=self.tls,
            active=self.active,
            git_binary=self.git_binary,
        )


def load_configuration(path: Path) -> RunnerControllerAdmin:
    if not path.is_absolute() or path.stem.startswith("pyfly") or path.suffix not in {".yaml", ".yml", ".toml"}:
        raise ValueError("Use an explicit private runner configuration, without implicit profile loading")
    private_tls_file(path)
    config = Config.from_file(path, load_defaults=False)
    value = RunnerControllerAdmin.model_validate(config.effective_section("forge.runner.controller"))
    value.settings()
    return value


@click.command()
@click.option("--configuration", type=click.Path(path_type=Path), required=True)
def main(configuration: Path) -> None:
    try:
        value = load_configuration(configuration)
        tls: dict[str, Any] = value.tls.uvicorn_settings()
    except Exception:
        raise click.ClickException("Private runner configuration or mutual TLS material is invalid") from None
    uvicorn.run(
        create_runner_application(value.settings()),
        host=value.host,
        port=value.port,
        proxy_headers=False,
        access_log=False,
        **tls,
    )


if __name__ == "__main__":
    main()
