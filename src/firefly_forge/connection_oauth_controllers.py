# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Authenticated connection administration; browser consent cannot run through tools."""

from typing import Any, cast
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field
from pyfly.container import rest_controller, service
from pyfly.container.ordering import HIGHEST_PRECEDENCE, order
from pyfly.oauth2 import OAuth2ClientError
from pyfly.web import Body, PathVar, QueryParam, get_mapping, post_mapping, put_mapping, request_mapping
from pyfly.web.filters import OncePerRequestFilter
from pyfly.web.ports.filter import CallNext
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response

from firefly_forge.connection_oauth import ConnectionOAuth
from firefly_forge.connection_oauth_models import OAuthConnectionInput
from firefly_forge.identity import SESSION_COOKIE, IdentityService, Principal
from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied


class OAuthRefreshInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: str = Field(pattern=r"^[a-f0-9]{32}$")


class BrowserConsentRequired(AccessDenied):
    pass


@service
@order(HIGHEST_PRECEDENCE + 200)
class ConnectionOAuthPrivacy(OncePerRequestFilter):
    async def do_filter(self, request: Request, call_next: CallNext) -> Response:
        response = cast(Response, await call_next(request))
        if request.url.path.startswith("/api/v1/connection-oauth/"):
            # The native default header filter appends its general policy. Override
            # outside that filter so a browser cannot select its less strict value.
            response.headers["Referrer-Policy"] = "no-referrer"
            response.headers["Cache-Control"] = "no-store"
        return response


@rest_controller
@request_mapping("/api/v1/connection-oauth")
class ConnectionOAuthController:
    def __init__(self, connections: ConnectionOAuth, identity: IdentityService) -> None:
        self.connections, self.identity = connections, identity

    @staticmethod
    def _actor() -> Principal:
        actor = CURRENT_PRINCIPAL.get()
        if actor is None:
            raise AccessDenied("An authenticated factory administrator is required")
        return actor

    async def _browser(self, request: Request) -> Principal:
        actor = self._actor()
        if (
            not request.cookies.get(SESSION_COOKIE)
            or actor.authentication_method == "oidc_bearer"
            or actor.kind == "service"
        ):
            raise BrowserConsentRequired("Open this connection in your authenticated factory browser")
        scope = {
            **request.scope,
            "headers": [(key, value) for key, value in request.scope["headers"] if key.lower() != b"authorization"],
        }
        browser = await self.identity.authenticate(Request(scope))
        if (
            browser is None
            or browser.id != actor.id
            or browser.issuer != actor.issuer
            or browser.subject != actor.subject
            or browser.authentication_method == "oidc_bearer"
            or browser.kind == "service"
        ):
            raise BrowserConsentRequired("Open this connection in your authenticated factory browser")
        return actor

    @get_mapping("/providers")
    def providers(self) -> list[dict[str, Any]]:
        return self.connections.providers()

    @get_mapping("/{name}")
    async def summary(self, name: PathVar[str]) -> dict[str, Any]:
        return await self.connections.summary(name, self._actor())

    @put_mapping("/{name}")
    async def configure(self, name: PathVar[str], body: Body[OAuthConnectionInput]) -> dict[str, Any]:
        return await self.connections.configure(name, body, self._actor())

    @post_mapping("/{name}/prepare")
    async def prepare(self, name: PathVar[str]) -> dict[str, Any]:
        result = await self.connections.prepare(name, self._actor())
        return {"continuation_url": result["continuation_url"], "expires_in": 300}

    @get_mapping("/{name}/continue")
    async def begin(self, name: PathVar[str], request: Request, ticket: QueryParam[str] = "") -> Response:
        try:
            actor = await self._browser(request)
        except BrowserConsentRequired:
            return JSONResponse(
                {
                    "detail": "Open this connection in your authenticated factory browser.",
                    "oauth_error": "browser_required",
                },
                status_code=403,
            )
        if len(request.query_params.getlist("ticket")) != 1:
            raise ValueError("A single browser handoff is required")
        challenge = await self.connections.begin(name, ticket, actor, browser_authenticated=True)
        response = RedirectResponse(challenge.authorization_url, status_code=303)
        response.set_cookie(
            "forge_connection_" + name,
            challenge.browser_nonce,
            max_age=300,
            httponly=True,
            secure=urlsplit(self.connections.browser_origin).scheme == "https",
            samesite="lax",
            path="/api/v1/connection-oauth/" + name + "/callback",
        )
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @get_mapping("/{name}/callback")
    async def callback(
        self,
        name: PathVar[str],
        request: Request,
        code: QueryParam[str] = "",
        state: QueryParam[str] = "",
        iss: QueryParam[str] = "",
    ) -> Response:
        try:
            actor = await self._browser(request)
        except BrowserConsentRequired:
            return JSONResponse(
                {
                    "detail": "Open this connection in your authenticated factory browser.",
                    "oauth_error": "browser_required",
                },
                status_code=403,
            )
        response: Response
        try:
            if any(len(request.query_params.getlist(key)) != 1 for key in ("code", "state", "iss")):
                raise ValueError("Authorization response requires exact code, state and issuer")
            await self.connections.complete(
                name, state, code, request.cookies.get("forge_connection_" + name, ""), actor, issuer=iss
            )
            response = RedirectResponse("/", status_code=303)
        except (ValueError, OAuth2ClientError):
            response = JSONResponse(
                {"detail": "Authorization did not complete. Start a new connection attempt."}, status_code=409
            )
        response.delete_cookie("forge_connection_" + name, path="/api/v1/connection-oauth/" + name + "/callback")
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @post_mapping("/{name}/refresh")
    async def refresh(self, name: PathVar[str], body: Body[OAuthRefreshInput]) -> dict[str, Any] | JSONResponse:
        try:
            return await self.connections.refresh(name, body.request_id, self._actor())
        except OAuth2ClientError as error:
            return JSONResponse(
                {"detail": "Reauthorize this connection before trying again.", "oauth_error": error.code},
                status_code=409,
            )

    @post_mapping("/{name}/disconnect")
    async def disconnect(self, name: PathVar[str]) -> dict[str, Any]:
        return await self.connections.disconnect(name, self._actor())
