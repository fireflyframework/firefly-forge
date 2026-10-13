# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Reviewed external tool schemas, private credentials and project-bound grants."""

from __future__ import annotations

import asyncio
import copy
import json
import re
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx2
from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from pydantic import BaseModel, ConfigDict, Field
from pyfly.container import service

from firefly_forge.configuration import ConfigurationStore
from firefly_forge.journal import digest
from firefly_forge.secrets import contains_known_credential
from firefly_forge.workspace_tools import _private_name

Data = dict[str, Any]
CATALOG: dict[str, Data] = {
    "github": {
        "name": "GitHub",
        "endpoint": "https://api.githubcopilot.com/mcp/readonly",
        "tools": ["get_file_contents", "get_commit", "list_commits", "list_branches"],
        "prerequisite": None,
        "source": "https://github.com/github/github-mcp-server/blob/main/docs/remote-server.md",
    },
    "sentry": {
        "name": "Sentry",
        "endpoint": "https://mcp.sentry.dev/mcp",
        "tools": [],
        "prerequisite": (
            "Reviewed project-scoped adapter and server skill constraints are required; "
            "inference and issue enrichment are not admitted."
        ),
        "source": "https://github.com/getsentry/toolkit",
    },
    "figma": {
        "name": "Figma",
        "endpoint": "https://mcp.figma.com/mcp",
        "tools": [],
        "prerequisite": (
            "Vendor allowed-client approval or waitlist admission and an authorized OAuth connection are required."
        ),
        "source": "https://developers.figma.com/docs/figma-mcp-server/remote-server-installation/",
    },
    "playwright": {
        "name": "Playwright",
        "endpoint": None,
        "tools": [],
        "prerequisite": "Use the native isolated BrowserRunner; an external stdio launcher is not admitted.",
        "source": "https://github.com/microsoft/playwright-mcp",
    },
    "grafana": {
        "name": "Grafana",
        "endpoint": None,
        "tools": [],
        "prerequisite": "An approved managed HTTPS deployment and project-scoped adapter are required.",
        "source": "https://github.com/grafana/mcp-grafana",
    },
}


class MCPAdmissionError(ValueError):
    pass


class MCPConnectionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = Field(default=False, strict=True)
    api_key: str | None = Field(default=None, min_length=1, max_length=4096, json_schema_extra={"writeOnly": True})
    clear_secret: bool = Field(default=False, strict=True)


class MCPReviewInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    tools: list[str] = Field(min_length=1, max_length=4)


class MCPGrantInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    repository_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    review_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    tools: list[str] = Field(min_length=1, max_length=4)
    roles: list[Literal["implementer", "docs"]] = Field(default=["implementer"], min_length=1, max_length=2)
    stages: list[Literal["implement"]] = Field(default=["implement"], min_length=1, max_length=1)


def _identifier(value: str) -> str:
    if not re.fullmatch(r"[a-f0-9]{32}", value):
        raise ValueError("Select an existing project or repository identifier")
    return value


def _connector(value: str) -> Data:
    if value not in CATALOG:
        raise ValueError("Select an approved external MCP connector")
    return CATALOG[value]


def checked_schema(value: Data) -> Data:
    if not isinstance(value, dict) or len(json.dumps(value).encode()) > 30_000:
        raise MCPAdmissionError("MCP input schema exceeds its review bounds")
    nodes = 0

    def visit(item: Any, depth: int) -> None:
        nonlocal nodes
        nodes += 1
        if depth > 12 or nodes > 1000:
            raise MCPAdmissionError("MCP input schema is too deep or complex")
        if isinstance(item, dict):
            if set(item) & {"$ref", "$dynamicRef", "$recursiveRef", "pattern", "patternProperties", "format"}:
                raise MCPAdmissionError("MCP input schema references and regular expressions are unavailable")
            for child in item.values():
                visit(child, depth + 1)
        elif isinstance(item, list):
            for child in item:
                visit(child, depth + 1)

    visit(value, 0)
    if value.get("type") != "object" or not isinstance(value.get("properties", {}), dict):
        raise MCPAdmissionError("MCP input schema must describe an argument object")
    try:
        Draft202012Validator.check_schema(value)
    except Exception:
        raise MCPAdmissionError("MCP input schema is invalid") from None
    return copy.deepcopy(value)


def bounded_arguments(schema: Data, arguments: Data) -> Data:
    if not isinstance(arguments, dict) or len(json.dumps(arguments).encode()) > 64_000:
        raise MCPAdmissionError("MCP arguments exceed 64 KB")
    nodes = 0

    def visit(value: Any, rule: Any, depth: int) -> None:
        nonlocal nodes
        nodes += 1
        if depth > 12 or nodes > 1000:
            raise MCPAdmissionError("MCP arguments exceed structural bounds")
        if isinstance(value, str) and (len(value) > 4096 or any(ord(c) < 32 for c in value)):
            raise MCPAdmissionError("MCP argument strings exceed their bounds")
        if isinstance(value, dict):
            properties = rule.get("properties", {}) if isinstance(rule, dict) else {}
            if len(value) > 64 or set(value) - properties.keys():
                raise MCPAdmissionError("MCP arguments contain fields outside the reviewed schema")
            for name, child in value.items():
                visit(child, properties[name], depth + 1)
        elif isinstance(value, list):
            if len(value) > 100:
                raise MCPAdmissionError("MCP argument arrays exceed their bounds")
            for child in value:
                visit(child, rule.get("items", {}) if isinstance(rule, dict) else {}, depth + 1)
        elif value is not None and type(value) not in {bool, int, float, str}:
            raise MCPAdmissionError("MCP arguments must contain JSON values")

    visit(arguments, schema, 0)
    if not Draft202012Validator(schema).is_valid(arguments):
        raise MCPAdmissionError("MCP arguments do not match the reviewed schema")
    return copy.deepcopy(arguments)


class _BoundedStream(httpx2.AsyncByteStream):
    def __init__(self, stream: httpx2.AsyncByteStream) -> None:
        self.stream = stream

    async def __aiter__(self) -> AsyncIterator[bytes]:
        size = 0
        async for chunk in self.stream:
            size += len(chunk)
            if size > 1_000_000:
                raise MCPAdmissionError("MCP response exceeds 1 MB")
            yield chunk

    async def aclose(self) -> None:
        await self.stream.aclose()


class _PinnedTransport(httpx2.AsyncBaseTransport):
    def __init__(self, inner: httpx2.AsyncBaseTransport, endpoint: str, expected: Data | None) -> None:
        self.inner, self.endpoint, self.expected = inner, endpoint, expected
        self.call_sent = False
        self.requests = 0

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        self.requests += 1
        if str(request.url) != self.endpoint or self.requests > 12 or request.method not in {"POST", "GET", "DELETE"}:
            raise MCPAdmissionError("MCP request is outside its approved endpoint or request budget")
        if request.method == "POST":
            content = await request.aread()
            if len(content) > 80_000:
                raise MCPAdmissionError("MCP request exceeds its byte bound")
            packet = json.loads(content)
            method = packet.get("method")
            if method not in {
                "initialize",
                "notifications/initialized",
                "tools/list",
                "tools/call",
                "notifications/cancelled",
            }:
                raise MCPAdmissionError("MCP protocol method is not admitted")
            if method == "tools/call":
                params = dict(packet.get("params", {}))
                metadata = params.pop("_meta", {})
                if metadata and (
                    not isinstance(metadata, dict)
                    or set(metadata) != {"traceparent"}
                    or not isinstance(metadata["traceparent"], str)
                    or not re.fullmatch(r"00-[a-f0-9]{32}-[a-f0-9]{16}-[a-f0-9]{2}", metadata["traceparent"])
                ):
                    raise MCPAdmissionError("MCP invocation metadata is outside its tracing contract")
                if self.call_sent or self.expected is None or params != self.expected:
                    raise MCPAdmissionError("MCP tool dispatch differs from its approved invocation or attempts replay")
                self.call_sent = True
        response = await self.inner.handle_async_request(request)
        if 300 <= response.status_code < 400:
            await response.aclose()
            raise MCPAdmissionError("MCP redirects are unavailable")
        if response.status_code not in {200, 202, 204, 405}:
            await response.aclose()
            raise MCPAdmissionError("MCP request was rejected; check credentials, access and provider limits")
        if not isinstance(response.stream, httpx2.AsyncByteStream):
            raise MCPAdmissionError("MCP response stream is invalid")
        response.stream = _BoundedStream(response.stream)
        return response

    async def aclose(self) -> None:
        await self.inner.aclose()


def _safe_error(error: BaseException) -> MCPAdmissionError | None:
    if isinstance(error, MCPAdmissionError):
        return error
    if isinstance(error, BaseExceptionGroup):
        for child in error.exceptions:
            result = _safe_error(child)
            if result:
                return result
    return None


def mcp_snapshot(directory: Path | ConfigurationStore, project_id: str | None, repository_id: str) -> Data:
    private = directory if isinstance(directory, ConfigurationStore) else ConfigurationStore(directory)
    with private.lock:
        state = private._read(private.directory / "external-mcp.json")
        grants = [
            copy.deepcopy(item)
            for item in state.get("grants", [])
            if item["project_id"] == project_id
            and item["repository_id"] == repository_id
            and item.get("active")
            and state.get("connections", {}).get(item["connector"], {}).get("enabled")
            and state["connections"][item["connector"]].get("credential_revision") == item["credential_revision"]
        ]
    payload = {"version": 1, "project_id": project_id, "repository_id": repository_id, "grants": grants}
    return {**payload, "digest": digest(payload)}


@service
class ExternalMCPRegistry:
    def __init__(self, directory: Path | ConfigurationStore) -> None:
        self.private = directory if isinstance(directory, ConfigurationStore) else ConfigurationStore(directory)
        self.path = self.private.directory / "external-mcp.json"
        self.revisions = self.private.directory / "mcp-revisions"
        self.revisions.mkdir(exist_ok=True, mode=0o700)
        if self.revisions.is_symlink():
            raise ValueError("MCP revisions require a real private directory")
        self.revisions.chmod(0o700)
        self.transport: httpx2.AsyncBaseTransport | None = None
        self.credential_registry: Callable[[], dict[str, str]] = dict

    def _state(self) -> Data:
        state = self.private._read(self.path)
        return {"connections": {}, "grants": [], **state}

    def credentials(self) -> dict[str, str]:
        with self.private.lock:
            return {
                "FORGE_MCP_API_KEY_" + path.stem: record["api_key"]
                for path in self.revisions.glob("*.json")
                if (record := self.private._read(path)).get("api_key")
            }

    def redaction_settings(self) -> dict[str, str]:
        return self.credentials()

    def catalog(self) -> list[Data]:
        with self.private.lock:
            connections = self._state()["connections"]
        result = []
        for connector, definition in CATALOG.items():
            record = connections.get(connector, {})
            readiness = (
                "prerequisite_required"
                if definition["prerequisite"]
                else "disabled"
                if not record.get("enabled")
                else "credential_required"
                if not record.get("credential_revision")
                else "discovery_required"
                if not record.get("discovery")
                else "schema_review_required"
                if not record.get("review")
                else "project_grant_required"
            )
            result.append(
                {
                    "id": connector,
                    **copy.deepcopy(definition),
                    "enabled": bool(record.get("enabled")),
                    "credential_configured": bool(record.get("credential_revision")),
                    "readiness": readiness,
                    "discovery": copy.deepcopy(record.get("discovery")),
                    "review": copy.deepcopy(record.get("review")),
                    "live_tool_verified": False,
                }
            )
        return result

    def configure(self, connector: str, request: MCPConnectionInput) -> Data:
        definition = _connector(connector)
        if definition["prerequisite"] and (request.enabled or request.api_key):
            raise ValueError("This connector has an unmet prerequisite: " + definition["prerequisite"])
        if request.api_key and (any(ord(c) <= 32 for c in request.api_key) or request.clear_secret):
            raise ValueError("Supply one nonempty credential or clear the existing secret")
        with self.private.lock:
            state = self._state()
            record = state["connections"].get(connector, {})
            if request.api_key:
                revision = uuid.uuid4().hex
                self.private._write(
                    self.revisions / (revision + ".json"), {"connector": connector, "api_key": request.api_key}
                )
                record = {"credential_revision": revision}
            if request.clear_secret:
                record = {}
                for item in state["grants"]:
                    if item["connector"] == connector:
                        item["active"] = False
            if request.enabled and not record.get("credential_revision"):
                raise ValueError("Configure a credential before enabling this connector")
            state["connections"][connector] = {**record, "enabled": request.enabled}
            self.private._write(self.path, state)
        return next(item for item in self.catalog() if item["id"] == connector)

    @asynccontextmanager
    async def _session(
        self, connector: str, revision: str, expected: Data | None = None
    ) -> AsyncIterator[tuple[ClientSession, Data]]:
        definition = _connector(connector)
        _identifier(revision)
        with self.private.lock:
            credential = self.private._read(self.revisions / (revision + ".json"))
        if credential.get("connector") != connector or not credential.get("api_key"):
            raise MCPAdmissionError("Frozen MCP credential revision is unavailable")
        transport = _PinnedTransport(
            self.transport or httpx2.AsyncHTTPTransport(retries=0), definition["endpoint"], expected
        )
        try:
            async with (
                asyncio.timeout(30),
                httpx2.AsyncClient(
                    transport=transport,
                    timeout=15,
                    trust_env=False,
                    follow_redirects=False,
                    headers={"Authorization": "Bearer " + credential["api_key"]},
                ) as http,
                streamable_http_client(
                    definition["endpoint"], http_client=http, max_sse_event_size=1_000_000
                ) as streams,
                ClientSession(streams[0], streams[1], read_timeout_seconds=15) as session,
            ):
                initialized = await session.initialize()
                info = initialized.server_info.model_dump(by_alias=True, exclude_none=True)
                if len(json.dumps(info).encode()) > 1024:
                    raise MCPAdmissionError("MCP server identity exceeds its bounds")
                yield session, info
        except Exception as error:
            safe = _safe_error(error)
            raise safe or MCPAdmissionError(
                "MCP exchange or schema admission failed; inspect access, provider limits and the retained invocation"
            ) from None

    async def _tools(self, session: ClientSession, connector: str) -> Data:
        response = await session.list_tools()
        if response.next_cursor or len(response.tools) > 200:
            raise MCPAdmissionError("MCP catalog requires a bounded nonpaginated toolset")
        tools = {}
        for item in response.tools:
            if item.name not in CATALOG[connector]["tools"]:
                continue
            if item.name in tools:
                raise MCPAdmissionError("MCP catalog contains duplicate tool names")
            tools[item.name] = checked_schema(item.input_schema)
        if not tools:
            raise MCPAdmissionError("MCP server exposes no admitted repository-scoped tools")
        return tools

    async def discover(self, connector: str) -> Data:
        definition = _connector(connector)
        if definition["prerequisite"]:
            raise ValueError("This connector has an unmet prerequisite")
        with self.private.lock:
            connection = self._state()["connections"].get(connector, {})
            revision = connection.get("credential_revision")
            if not connection.get("enabled") or not revision:
                raise ValueError("Enable a configured connector before discovery")
        async with self._session(connector, revision) as (session, info):
            tools = await self._tools(session, connector)
        payload = {
            "connector": connector,
            "endpoint": definition["endpoint"],
            "credential_revision": revision,
            "server": info,
            "tools": tools,
        }
        if contains_known_credential(payload, {**self.credentials(), **self.credential_registry()}):
            raise ValueError("MCP discovery reflected configured credentials")
        discovery = {**payload, "digest": digest(payload)}
        with self.private.lock:
            state = self._state()
            if state["connections"].get(connector, {}).get("credential_revision") != revision:
                raise ValueError("MCP credential changed during discovery; repeat explicit discovery")
            state["connections"][connector].update(discovery=discovery, review=None)
            self.private._write(self.path, state)
        return copy.deepcopy(discovery)

    def review(self, connector: str, request: MCPReviewInput) -> Data:
        _connector(connector)
        with self.private.lock:
            state = self._state()
            record = state["connections"].get(connector, {})
            discovery = record.get("discovery") or {}
            if request.digest != discovery.get("digest"):
                raise ValueError("Review must match the exact discovered schema digest")
            if len(set(request.tools)) != len(request.tools) or set(request.tools) - discovery["tools"].keys():
                raise ValueError("Review selects an unavailable or duplicate tool")
            record["review"] = {"digest": request.digest, "tools": request.tools}
            state["connections"][connector] = record
            self.private._write(self.path, state)
        return next(item for item in self.catalog() if item["id"] == connector)

    def grant(self, project_id: str, connector: str, request: MCPGrantInput, *, repository: Data) -> Data:
        _identifier(project_id)
        _connector(connector)
        parsed = urlsplit(repository["url"])
        parts = parsed.path.removesuffix(".git").strip("/").split("/")
        if (
            connector != "github"
            or parsed.scheme != "https"
            or parsed.netloc != "github.com"
            or parsed.query
            or parsed.fragment
            or len(parts) != 2
            or any(not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", part) or part in {".", ".."} for part in parts)
            or repository["id"] != request.repository_id
        ):
            raise ValueError("MCP grant requires a registered HTTPS GitHub repository binding")
        with self.private.lock:
            state = self._state()
            connection = state["connections"].get(connector, {})
            discovery, review = connection.get("discovery") or {}, connection.get("review") or {}
            if (
                not connection.get("enabled")
                or request.review_digest != review.get("digest")
                or request.review_digest != discovery.get("digest")
                or len(set(request.tools)) != len(request.tools)
                or set(request.tools) - set(review.get("tools", []))
            ):
                raise ValueError("MCP grant requires the exact enabled reviewed tool revision")
            if sum(item.get("active", False) for item in state["grants"]) >= 1000:
                raise ValueError("MCP active grants exceed the configured bound")
            grant = {
                "id": uuid.uuid4().hex,
                "active": True,
                "project_id": project_id,
                "repository_id": request.repository_id,
                "connector": connector,
                "endpoint": CATALOG[connector]["endpoint"],
                "credential_revision": discovery["credential_revision"],
                "review_digest": request.review_digest,
                "server": discovery["server"],
                "tools": {name: discovery["tools"][name] for name in request.tools},
                "roles": request.roles,
                "stages": request.stages,
                "scope": {"owner": parts[0], "repo": parts[1]},
            }
            if contains_known_credential(grant, {**self.credentials(), **self.credential_registry()}):
                raise ValueError("MCP grant cannot contain configured credentials")
            state["grants"].append(grant)
            self.private._write(self.path, state)
        return copy.deepcopy(grant)

    def grants(self, project_id: str) -> list[Data]:
        _identifier(project_id)
        with self.private.lock:
            return [copy.deepcopy(item) for item in self._state()["grants"] if item["project_id"] == project_id]

    def revoke(self, project_id: str, grant_id: str) -> Data:
        _identifier(project_id)
        _identifier(grant_id)
        with self.private.lock:
            state = self._state()
            grant = next(
                (item for item in state["grants"] if item["project_id"] == project_id and item["id"] == grant_id), None
            )
            if grant is None:
                raise KeyError("MCP grant not found in this project")
            grant["active"] = False
            self.private._write(self.path, state)
        return copy.deepcopy(grant)

    def snapshot(self, project_id: str | None, repository_id: str) -> Data:
        return mcp_snapshot(self.private, project_id, repository_id)

    def authorize(
        self, snapshot: Data, connector: str, tool: str, arguments: Data, *, roles: list[str], stage: str
    ) -> tuple[Data, Data]:
        payload = {key: value for key, value in snapshot.items() if key != "digest"}
        if (
            set(payload) != {"version", "project_id", "repository_id", "grants"}
            or payload["version"] != 1
            or snapshot.get("digest") != digest(payload)
        ):
            raise ValueError("MCP frozen snapshot digest mismatch")
        if not payload["project_id"]:
            raise ValueError("MCP tools require a project-scoped grant")
        _connector(connector)
        grants = [
            item
            for item in payload["grants"]
            if item["connector"] == connector
            and tool in item["tools"]
            and stage in item["stages"]
            and set(roles) & set(item["roles"])
        ]
        if len(grants) != 1:
            raise ValueError("MCP invocation requires one exact role and tool grant")
        grant = grants[0]
        with self.private.lock:
            state = self._state()
            current = next((item for item in state["grants"] if item["id"] == grant["id"]), None)
            if current is None or not current.get("active"):
                raise ValueError("The frozen MCP grant has been revoked")
            if (
                current != grant
                or current["project_id"] != payload["project_id"]
                or current["repository_id"] != payload["repository_id"]
            ):
                raise ValueError("The MCP grant differs from its frozen project binding")
            if not state["connections"].get(connector, {}).get("enabled"):
                raise ValueError("The MCP connection is disabled")
        args = bounded_arguments(checked_schema(grant["tools"][tool]), arguments)
        if args.get("owner") != grant["scope"]["owner"] or args.get("repo") != grant["scope"]["repo"]:
            raise ValueError("MCP arguments must match the granted repository")
        if "path" in args:
            path = args["path"]
            if (
                not isinstance(path, str)
                or path.startswith("/")
                or "\\" in path
                or any(part in {"", ".", ".."} or _private_name(part) for part in path.split("/"))
            ):
                raise ValueError("MCP source paths must be confined and exclude credentials")
        if contains_known_credential(args, {**self.credentials(), **self.credential_registry()}):
            raise ValueError("MCP arguments cannot contain configured credentials")
        return copy.deepcopy(grant), args

    async def call(
        self, snapshot: Data, connector: str, tool: str, arguments: Data, *, roles: list[str], stage: str
    ) -> Data:
        grant, args = self.authorize(snapshot, connector, tool, arguments, roles=roles, stage=stage)
        async with self._session(connector, grant["credential_revision"], {"name": tool, "arguments": args}) as (
            session,
            info,
        ):
            tools = await self._tools(session, connector)
            if info != grant["server"] or tools.get(tool) != grant["tools"][tool]:
                raise MCPAdmissionError("MCP server identity or schema changed; perform explicit discovery and review")
            self.authorize(snapshot, connector, tool, arguments, roles=roles, stage=stage)
            response = await session.call_tool(tool, args, read_timeout_seconds=15)
            packet = response.model_dump(by_alias=True, exclude_none=True)
        if packet.get("isError") or len(json.dumps(packet).encode()) > 256_000:
            raise ValueError("MCP tool result was not admitted within its response bound")
        content = packet.get("content", [])
        if not content or any(item.get("type") != "text" or not isinstance(item.get("text"), str) for item in content):
            raise ValueError("MCP tools must return bounded text; resource and binary fetching is unavailable")
        result = {
            "connector": connector,
            "tool": tool,
            "review_digest": grant["review_digest"],
            "untrusted": True,
            "text": [item["text"] for item in content],
        }
        if contains_known_credential(result, {**self.credentials(), **self.credential_registry()}):
            raise ValueError("MCP result reflected configured credentials")
        return result
