# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Typed native login endpoints with owner-only cookies and fixed redirects."""

from typing import Any

from pydantic import BaseModel, ConfigDict, SecretStr
from pyfly.container import rest_controller
from pyfly.web import Body, QueryParam, get_mapping, post_mapping, request_mapping
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response

from firefly_forge.identity import ATTEMPT_COOKIE, AUTH_PREFIX, SESSION_COOKIE, IdentityService


class IdentityLoginInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: SecretStr = SecretStr("")


@rest_controller
@request_mapping(AUTH_PREFIX)
class IdentityController:
    def __init__(self, identity: IdentityService) -> None:
        self.identity = identity

    @get_mapping("/configuration")
    def configuration(self) -> dict[str, Any]:
        return self.identity.configuration()

    @post_mapping("/login")
    async def login(self, request: Request, body: Body[IdentityLoginInput]) -> Response:
        self.identity.check_cookie_origin(request)
        if self.identity.config.mode == "local_bootstrap":
            result = await self.identity.login_bootstrap(body.token.get_secret_value(), request)
            response: Response = JSONResponse({"authenticated": True})
            response.set_cookie(
                SESSION_COOKIE,
                result.cookie,
                max_age=result.expires_in,
                httponly=True,
                secure=request.url.scheme == "https",
                samesite="lax",
                path="/",
            )
            response.headers["Cache-Control"] = "no-store"
            return response
        challenge = await self.identity.begin_login()
        response = JSONResponse({"authorization_url": challenge.authorization_url, "expires_in": challenge.expires_in})
        response.set_cookie(
            ATTEMPT_COOKIE,
            challenge.browser_nonce,
            max_age=challenge.expires_in,
            httponly=True,
            secure=self.identity.configuration()["cookie_secure"],
            samesite="lax",
            path=AUTH_PREFIX + "/callback",
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    @get_mapping("/callback")
    async def callback(
        self, request: Request, code: QueryParam[str] = "", state: QueryParam[str] = "", iss: QueryParam[str] = ""
    ) -> Response:
        try:
            result = await self.identity.complete_login(
                code, state, request.cookies.get(ATTEMPT_COOKIE, ""), issuer=iss or None
            )
            response: Response = RedirectResponse("/", status_code=303)
            response.set_cookie(
                SESSION_COOKIE,
                result.cookie,
                max_age=result.expires_in,
                httponly=True,
                secure=self.identity.configuration()["cookie_secure"],
                samesite="lax",
                path="/",
            )
        except ValueError:
            response = JSONResponse({"detail": "Sign-in failed. Start a new sign-in attempt."}, status_code=401)
        response.delete_cookie(ATTEMPT_COOKIE, path=AUTH_PREFIX + "/callback")
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @post_mapping("/logout")
    async def logout(self, request: Request) -> Response:
        self.identity.check_cookie_origin(request)
        await self.identity.logout(request.cookies.get(SESSION_COOKIE, ""))
        response = JSONResponse({"authenticated": False})
        response.delete_cookie(SESSION_COOKIE, path="/")
        response.headers["Cache-Control"] = "no-store"
        return response
