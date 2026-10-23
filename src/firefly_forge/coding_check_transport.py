# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""One invocation's loopback MCP endpoint; never the factory operator API."""

from __future__ import annotations

import asyncio
import json
import math
import secrets
import socket
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, contextmanager
from typing import Annotated, Any

import uvicorn
from mcp.server.mcpserver import MCPServer
from pydantic import Field
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from firefly_forge.coding_check_models import CheckIndex, ConfiguredCheckRequest
from firefly_forge.prompts import prompt_text
from firefly_forge.secrets import ephemeral_credentials, redact_environment
from firefly_forge.source_edit_models import READ_TOOLS, SourceReadRequest
from firefly_forge.source_tool_budget import ToolBudgetRefusal

TOKEN_ENV = "FORGE_CODING_RUNTIME_TOKEN"
TOOL_NAME = "run_configured_check"


class _Boundary:
    def __init__(self, app: ASGIApp, owner: CodingCheckServer, port: int) -> None:
        self.app, self.owner, self.port = app, owner, port

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = scope.get("headers", [])
        values = {key: value for key, value in headers}
        status = None
        if scope.get("raw_path") != b"/mcp" or scope.get("query_string"):
            status = 404
        elif any(
            sum(key == name for key, _ in headers) != 1 for name in (b"host", b"authorization")
        ) or not secrets.compare_digest(values.get(b"authorization", b""), ("Bearer " + self.owner.token).encode()):
            status = 401
        elif values.get(b"host") != f"127.0.0.1:{self.port}".encode() or (
            b"origin" in values and values[b"origin"] != f"http://127.0.0.1:{self.port}".encode()
        ):
            status = 403
        elif not self.owner.accepting or self.owner.expires_at_ms <= int(time.time() * 1000):
            status = 410
        if status:
            await PlainTextResponse("Scoped coding endpoint unavailable", status_code=status)(scope, receive, send)
            return
        if scope["method"] == "POST":
            body = bytearray()
            while True:
                message = await receive()
                if message["type"] != "http.request":
                    return
                body.extend(message.get("body", b""))
                if len(body) > 4096:
                    await PlainTextResponse("Scoped request exceeds its bound", status_code=413)(scope, receive, send)
                    return
                if not message.get("more_body"):
                    break
            packet = None
            try:
                packet = json.loads(body)
                if isinstance(packet, dict) and packet.get("method") == "tools/call":
                    params = packet["params"]
                    if params["name"] not in self.owner.tool_names:
                        raise ValueError("Only the explicitly scoped tools are available")
                    if self.owner.source_callback is not None:
                        SourceReadRequest(tool=params["name"], **params["arguments"])
                    else:
                        ConfiguredCheckRequest.model_validate(params["arguments"])
                elif (
                    isinstance(packet, dict)
                    and self.owner.source_callback is not None
                    and packet.get("method") in {"resources/list", "resources/templates/list", "resources/read"}
                ):
                    params = packet.get("params", {})
                    field = "uri" if packet["method"] == "resources/read" else "cursor"
                    if not isinstance(params, dict) or params.keys() - {field, "_meta"}:
                        raise ValueError("Resources cannot select another server or expand the scoped transport")
                    value = params.get(field)
                    if (field == "uri" or value is not None) and (
                        not isinstance(value, str) or len(value) > 2048 or (field == "uri" and not value)
                    ):
                        raise ValueError("Resources cannot select another server or expand the scoped transport")
                    meta = params.get("_meta")
                    if meta is not None:
                        if not isinstance(meta, dict) or meta.keys() - {"progressToken"}:
                            raise ValueError("Only bounded standard resource progress metadata is available")
                        if "progressToken" in meta:
                            token = meta["progressToken"]
                            if type(token) not in (int, str) or (isinstance(token, str) and len(token) > 2048):
                                raise ValueError("Resource progress tokens must be bounded strings or integers")
            except (ValueError, KeyError, TypeError):
                request_id = (
                    packet.get("id")
                    if isinstance(packet, dict)
                    and packet.get("jsonrpc") == "2.0"
                    and (
                        packet.get("method") in {"resources/list", "resources/templates/list", "resources/read"}
                        or (self.owner.source_callback is not None and packet.get("method") == "tools/call")
                    )
                    else None
                )
                if type(request_id) not in (int, str):
                    request_id = None
                await JSONResponse(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "error": {"code": -32602, "message": "Invalid configured check"},
                    },
                    status_code=400,
                )(scope, receive, send)
                return
            original_receive = receive
            delivered = False

            async def replay() -> Any:
                nonlocal delivered
                if not delivered:
                    delivered = True
                    return {"type": "http.request", "body": bytes(body), "more_body": False}
                return await original_receive()

            receive = replay
        with ephemeral_credentials(self.owner.environment):
            await self.app(scope, receive, send)


class _Server(uvicorn.Server):
    @contextmanager
    def capture_signals(self) -> Any:
        # This endpoint belongs to an already supervised API worker, not a new process supervisor.
        yield


class CodingCheckServer:
    def __init__(
        self,
        callback: Callable[[ConfiguredCheckRequest], Awaitable[dict[str, Any]]] | None,
        *,
        expires_at_ms: int,
        bind_port: int = 0,
        invocation_timeout: float | None = None,
        source_callback: Callable[[SourceReadRequest], Awaitable[dict[str, Any]]] | None = None,
    ) -> None:
        if (callback is None) == (source_callback is None):
            raise ValueError("Select exactly one scoped coding capability")
        self.callback, self.expires_at_ms = callback, expires_at_ms
        self.source_callback = source_callback
        self.tool_names = READ_TOOLS if source_callback is not None else (TOOL_NAME,)
        if invocation_timeout is not None:
            if not math.isfinite(invocation_timeout) or invocation_timeout <= 0:
                raise ValueError("The coding invocation requires a finite positive provider timeout")
            self.expires_at_ms = min(expires_at_ms, int(time.time() * 1000 + invocation_timeout * 1000))
        if type(bind_port) is not int or not (bind_port == 0 or 1024 <= bind_port <= 65535):
            raise ValueError("Use an explicitly reviewed unprivileged loopback port or zero")
        self.bind_port = bind_port
        self.token = secrets.token_urlsafe(32)
        self.environment = {TOKEN_ENV: self.token}
        self.accepting = True
        self.active: set[asyncio.Task[Any]] = set()
        self.endpoint: str | None = None

    def application(self, port: int) -> Any:
        server = MCPServer("forge scoped coding checks", log_level="ERROR")

        async def invoke(request: ConfiguredCheckRequest | SourceReadRequest) -> dict[str, Any]:
            task = asyncio.current_task()
            assert task is not None
            self.active.add(task)
            try:
                if not self.accepting or self.expires_at_ms <= int(time.time() * 1000):
                    raise ValueError("The configured-check invocation has expired")
                if isinstance(request, SourceReadRequest):
                    assert self.source_callback is not None
                    result = await self.source_callback(request)
                else:
                    assert self.callback is not None
                    result = await self.callback(request)
                safe = json.loads(redact_environment(json.dumps(result, ensure_ascii=False)))
                if not isinstance(safe, dict):
                    raise ValueError("Configured-check result must be a bounded object")
                return safe
            except asyncio.CancelledError:
                raise
            except ToolBudgetRefusal as refusal:
                if not isinstance(request, SourceReadRequest):
                    raise ValueError("Configured check not admitted; inspect its original native receipt") from None
                return refusal.safe_result
            except Exception:
                raise ValueError("Configured check not admitted; inspect its original native receipt") from None
            finally:
                self.active.discard(task)

        if self.source_callback is not None:

            @server.tool(description=prompt_text("coding-runtime/source-list", version="1.0.1"))
            async def list_files(request_id: Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")]) -> dict[str, Any]:
                return await invoke(SourceReadRequest(tool="list_files", request_id=request_id))

            @server.tool(description=prompt_text("coding-runtime/source-read", version="1.0.1"))
            async def read_file(
                path: Annotated[str, Field(min_length=1, max_length=1000)],
                request_id: Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")],
            ) -> dict[str, Any]:
                return await invoke(SourceReadRequest(tool="read_file", path=path, request_id=request_id))

            @server.tool(description=prompt_text("coding-runtime/source-search", version="1.0.1"))
            async def search_text(
                text: Annotated[str, Field(min_length=1, max_length=500)],
                request_id: Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")],
                path: Annotated[str, Field(max_length=1000)] = "",
                max_results: Annotated[int, Field(ge=1, le=100)] = 50,
            ) -> dict[str, Any]:
                return await invoke(
                    SourceReadRequest(
                        tool="search_text", text=text, path=path, max_results=max_results, request_id=request_id
                    )
                )
        else:

            @server.tool(description=prompt_text("coding-runtime/run-configured-check"))
            async def run_configured_check(
                command_index: CheckIndex, request_id: Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")]
            ) -> dict[str, Any]:
                return await invoke(ConfiguredCheckRequest(command_index=command_index, request_id=request_id))

        app = server.streamable_http_app(
            json_response=True, stateless_http=True, max_request_body_size=4096, max_sessions=1, host="127.0.0.1"
        )
        app.add_middleware(_Boundary, owner=self, port=port)
        return app

    @asynccontextmanager
    async def running(self) -> AsyncIterator[CodingCheckServer]:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        task = None
        server = None
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("127.0.0.1", self.bind_port))
            port = sock.getsockname()[1]
            self.endpoint = f"http://127.0.0.1:{port}/mcp"
            config = uvicorn.Config(
                self.application(port),
                access_log=False,
                log_level="critical",
                log_config=None,
                limit_concurrency=4,
                backlog=4,
            )
            server = _Server(config)
            task = asyncio.create_task(server.serve(sockets=[sock]))
            async with asyncio.timeout(5):
                while not server.started:
                    if task.done():
                        await task
                        raise RuntimeError("The scoped coding endpoint failed to start")
                    await asyncio.sleep(0.01)
            with ephemeral_credentials(self.environment):
                yield self
        finally:
            self.accepting = False

            async def close() -> None:
                try:
                    # Provider settlement comes first; accepted native effects must finish before transport teardown.
                    if self.active:
                        await asyncio.gather(*self.active, return_exceptions=True)
                    if server is not None and task is not None:
                        server.should_exit = True
                        await task
                finally:
                    sock.close()

            cleanup = asyncio.create_task(close())
            cancelled = False
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    cancelled = True
            await cleanup
            if cancelled:
                raise asyncio.CancelledError()
