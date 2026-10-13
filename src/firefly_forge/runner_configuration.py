# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Private repository runner defaults and authenticated native runner controls."""

from __future__ import annotations

import copy
import os
import stat
import uuid
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pyfly.container import rest_controller, service
from pyfly.web import Body, PathVar, get_mapping, post_mapping, put_mapping, request_mapping
from starlette.responses import Response

from firefly_forge.browser_runner import BrowserRunner
from firefly_forge.configuration import ConfigurationStore
from firefly_forge.runner_backends import require_available_runner
from firefly_forge.sandbox import SandboxExecutor
from firefly_forge.sandbox_models import SandboxInput, capture_sandbox
from firefly_forge.search_tools import SearchTools, search_domains
from firefly_forge.secrets import contains_known_credential
from firefly_forge.store import Store, identifier
from firefly_forge.workspace_tools import tool_catalog


class RunnerDefaults(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sandbox: SandboxInput


class SearchConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid")
    api_key: str | None = Field(default=None, min_length=1, max_length=4096, json_schema_extra={"writeOnly": True})
    clear_secret: bool = Field(default=False, strict=True)
    provider: Literal["brave", "tavily"] | None = None
    include_domains: list[str] | None = Field(default=None, max_length=10)

    @field_validator("include_domains")
    @classmethod
    def validate_domains(cls, value: list[str] | None) -> list[str] | None:
        return search_domains(value) if value is not None else None


class SearchQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1, max_length=600)
    max_results: int = Field(default=5, ge=1, le=10, strict=True)


@service
class RunnerConfiguration:
    def __init__(self, store: Store, executor: SandboxExecutor, search: SearchTools, browser: BrowserRunner) -> None:
        self.store, self.executor, self.search, self.browser = store, executor, search, browser
        self.executor.credential_registry = lambda: self.search.credential_registry()
        self.private = ConfigurationStore(store.path.parent)
        self.path = self.private.directory / "runners.json"

    async def configuration(self, repository_id: str) -> dict[str, Any]:
        repository_id = identifier(repository_id).hex
        await self.store.repository(repository_id)
        with self.private.lock:
            saved = self.private._read(self.path).get(repository_id)
        return {
            "repository_id": repository_id,
            "source": "repository" if saved else "platform_default",
            "revision": saved["revision"] if saved else None,
            "sandbox": saved["sandbox"] if saved else capture_sandbox(None)["configuration"],
        }

    async def configure_repository(self, repository_id: str, request: RunnerDefaults) -> dict[str, Any]:
        repository_id = identifier(repository_id).hex
        await self.store.repository(repository_id)
        require_available_runner(request.sandbox)
        configuration = capture_sandbox(request.sandbox)["configuration"]
        if contains_known_credential(configuration, self.search.credential_registry()):
            raise ValueError("Runner defaults cannot contain configured credentials")
        with self.private.lock:
            saved = self.private._read(self.path)
            if repository_id not in saved and len(saved) >= 1000:
                raise ValueError("Repository runner defaults exceed the 1000-project configuration bound")
            saved[repository_id] = {"revision": uuid.uuid4().hex, "sandbox": configuration}
            self.private._write(self.path, saved)
        return await self.configuration(repository_id)

    async def capture_run(self, repository_id: str, selected: SandboxInput | None = None) -> dict[str, Any]:
        settings = await self.configuration(repository_id)
        config = selected if selected is not None else SandboxInput.model_validate(settings["sandbox"])
        require_available_runner(config)
        sandbox = capture_sandbox(config)
        if contains_known_credential(sandbox, self.search.credential_registry()):
            raise ValueError("Runner configuration cannot contain configured credentials")
        search = self.search.snapshot() if config.internet_search else None
        if config.internet_search and not (search or {}).get("configured"):
            raise ValueError("Internet search must be configured before enabling it for a run")
        snapshot = {
            "sandbox_snapshot": sandbox,
            "runner_config_revision": settings["revision"] if selected is None else None,
            "search_snapshot": search,
        }
        return copy.deepcopy(snapshot)

    async def capabilities(self, snapshot: dict[str, Any], provider: str = "agentic") -> list[dict[str, Any]]:
        sandbox = snapshot["sandbox_snapshot"]
        configuration = sandbox["configuration"]
        if self.store.runtime_roles and self.store.runtime_roles.role == "api":
            tools = tool_catalog(
                {
                    "execute_command": True,
                    "browser_check": bool(configuration.get("browser")),
                    "browser_journey": bool(configuration.get("browser")),
                    "internet_search": bool((snapshot.get("search_snapshot") or {}).get("configured")),
                },
                provider,
            )
            for tool in tools:
                if tool["requires"] and tool["availability"] == "available":
                    tool.update(
                        availability="worker_unverified",
                        reason="Configured grant; execution worker must attest runtime availability",
                    )
            return tools
        command = await self.executor.probe(sandbox)
        browser = configuration.get("browser")
        browser_available = False
        if browser and command["available"]:
            browser_snapshot = capture_sandbox({**configuration, "image": browser["image"], "network": "none"})
            browser_available = bool((await self.executor.probe(browser_snapshot))["available"])
        return tool_catalog(
            {
                "execute_command": command["available"],
                "browser_check": browser_available,
                "browser_journey": browser_available,
                "internet_search": bool((snapshot.get("search_snapshot") or {}).get("configured")),
            },
            provider,
        )

    async def readiness(self, repository_id: str) -> dict[str, Any]:
        configuration = await self.configuration(repository_id)
        snapshot = await self.capture_run(repository_id)
        execution = (
            {
                "available": False,
                "status": "unknown",
                "detail": "Physical runner availability is checked by the execution worker",
            }
            if self.store.runtime_roles and self.store.runtime_roles.role == "api"
            else await self.executor.probe(snapshot["sandbox_snapshot"])
        )
        return {
            **configuration,
            "execution": execution,
            "tools": await self.capabilities(snapshot),
            "search": self.search.configuration(),
            "browser_note": (
                "Cached image presence does not prove a project server or matching Playwright package works"
            ),
        }

    def screenshot(self, evidence_id: str) -> Response:
        evidence_id = identifier(evidence_id).hex
        path = self.browser.directory / (evidence_id + ".png")
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError as exc:
            raise KeyError("Browser screenshot not found") from exc
        with os.fdopen(fd, "rb") as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_mode & 0o077:
                raise ValueError("Browser screenshot must remain a private regular file")
            data = source.read(2_000_001)
        if len(data) > 2_000_000 or not data.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError("Browser screenshot exceeds its bounded PNG contract")
        return Response(data, media_type="image/png", headers={"Cache-Control": "no-store"})


@rest_controller
@request_mapping("/api/v1")
class RunnerController:
    def __init__(self, settings: RunnerConfiguration) -> None:
        self.settings = settings

    @get_mapping("/repositories/{id}/runner")
    async def defaults(self, id: PathVar[str]) -> dict[str, Any]:
        return await self.settings.configuration(id)

    @put_mapping("/repositories/{id}/runner")
    async def configure(self, id: PathVar[str], body: Body[RunnerDefaults]) -> dict[str, Any]:
        return await self.settings.configure_repository(id, body)

    @get_mapping("/repositories/{id}/runner/readiness")
    async def readiness(self, id: PathVar[str]) -> dict[str, Any]:
        return await self.settings.readiness(id)

    @post_mapping("/runners/probe")
    async def probe(self, body: Body[SandboxInput]) -> dict[str, Any]:
        return await self.settings.executor.probe(capture_sandbox(body))

    @get_mapping("/search/configuration")
    def search_configuration(self) -> dict[str, Any]:
        return self.settings.search.configuration()

    @put_mapping("/search/configuration")
    def configure_search(self, body: Body[SearchConfiguration]) -> dict[str, Any]:
        return self.settings.search.configure(body.model_dump(exclude_none=True))

    @post_mapping("/search/test")
    async def test_search(self) -> dict[str, Any]:
        return await self.settings.search.test()

    @post_mapping("/search/query")
    async def search(self, body: Body[SearchQuery]) -> dict[str, Any]:
        return await self.settings.search.search(body.query, body.max_results)

    @get_mapping("/runner-evidence/{id}/screenshot")
    def screenshot(self, id: PathVar[str]) -> Response:
        return self.settings.screenshot(id)

    @get_mapping("/search/citations/{id}")
    def citation(self, id: PathVar[str]) -> dict[str, Any]:
        return self.settings.search.citation(id)
