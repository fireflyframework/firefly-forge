# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Owned IPv6 authorization/MCP server using stock public protocol implementations."""

import asyncio
import json
import socket
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

import uvicorn
from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from pyfly.security.context import SecurityContext
from pyfly.security.oauth2.authorization_server import AuthorizationServer, InMemoryTokenStore
from pyfly.security.oauth2.client import ClientRegistration, InMemoryClientRegistrationRepository
from pyfly.security.oauth2.endpoints import AuthorizationServerEndpoints
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route


@dataclass
class OwnedRepositoryService:
    origin: str
    token_requests: list = field(default_factory=list)
    tokens: list = field(default_factory=list, repr=False)
    tool_calls: list = field(default_factory=list)
    browser_cookie_seen: list = field(default_factory=list)
    hold_tool: bool = False
    entered: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)


@asynccontextmanager
async def repository_provider(factory_origin):
    listener = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    listener.bind(("::1", 0))
    origin = f"http://[::1]:{listener.getsockname()[1]}"
    owned = OwnedRepositoryService(origin)
    native = AuthorizationServer(
        "owned-local-repository-signing-fixture-key",
        InMemoryClientRegistrationRepository(
            ClientRegistration(
                registration_id="client",
                client_id="client",
                client_secret="fixture-secret",
                redirect_uri=factory_origin + "/api/v1/connection-oauth/design/callback",
                scopes=["repository:read"],
            )
        ),
        InMemoryTokenStore(),
        issuer=origin,
        audience=origin + "/mcp/readonly",
    )
    server = MCPServer("owned-local-repository", version="fixture-1", log_level="CRITICAL")

    async def recorded(name, owner, repo, **arguments):
        if (owner, repo) != ("team", "product"):
            raise ValueError("Only the owned fixture repository is available")
        owned.tool_calls.append({"tool": name, "owner": owner, "repo": repo, **arguments})
        owned.entered.set()
        if owned.hold_tool:
            await owned.release.wait()
        return "Owned repository receipt: stock = 5; no external repository was contacted."

    @server.tool()
    async def get_file_contents(owner: str, repo: str, path: str = "product.py") -> str:
        return await recorded("get_file_contents", owner, repo, path=path)

    @server.tool()
    async def get_commit(owner: str, repo: str, sha: str = "HEAD") -> str:
        return await recorded("get_commit", owner, repo, sha=sha)

    @server.tool()
    async def list_commits(owner: str, repo: str) -> str:
        return await recorded("list_commits", owner, repo)

    @server.tool()
    async def list_branches(owner: str, repo: str) -> str:
        return await recorded("list_branches", owner, repo)

    mcp_app = server.streamable_http_app(
        streamable_http_path="/mcp/readonly",
        json_response=True,
        stateless_http=True,
        max_request_body_size=80_000,
        transport_security=TransportSecuritySettings(
            allowed_hosts=[f"[::1]:{listener.getsockname()[1]}"], allowed_origins=[factory_origin]
        ),
        host="::1",
    )
    routes = AuthorizationServerEndpoints(native).routes()
    stock_token = next(route.endpoint for route in routes if route.path == "/oauth2/token")

    async def observed_token(request):
        owned.token_requests.append({"grant_type": (await request.form()).get("grant_type")})
        response = await stock_token(request)
        if response.status_code == 200:
            owned.tokens.append(json.loads(response.body))
        return response

    class LocalAuthority(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            owned.browser_cookie_seen.append("forge_session" in request.cookies)
            if request.url.path == "/mcp/readonly":
                authorization = request.headers.get("authorization", "")
                if not authorization.startswith("Bearer "):
                    return JSONResponse({"error": "resource_authorization_required"}, status_code=401)
                observed = await native.introspect(authorization[7:], allow_any_client=True)
                if (
                    not observed.get("active")
                    or observed.get("iss") != origin
                    or observed.get("aud") != origin + "/mcp/readonly"
                    or observed.get("scope") != "repository:read"
                ):
                    return JSONResponse({"error": "resource_binding_invalid"}, status_code=401)
            else:
                request.state.security_context = SecurityContext(user_id="owned-fixture-owner")
            return await call_next(request)

    @asynccontextmanager
    async def lifespan(app):
        async with mcp_app.router.lifespan_context(mcp_app):
            yield

    app = Starlette(
        routes=[
            Route("/oauth2/token", observed_token, methods=["POST"]),
            *[r for r in routes if r.path != "/oauth2/token"],
            Mount("/", mcp_app),
        ],
        lifespan=lifespan,
    )
    app.add_middleware(LocalAuthority)
    process = uvicorn.Server(uvicorn.Config(app, log_level="critical", access_log=False))
    running = asyncio.create_task(process.serve(sockets=[listener]))
    try:
        async with asyncio.timeout(5):
            while not process.started:
                if running.done():
                    running.result()
                await asyncio.sleep(0.01)
        yield owned
    finally:
        owned.release.set()
        process.should_exit = True
        await asyncio.wait_for(running, 5)
        listener.close()
