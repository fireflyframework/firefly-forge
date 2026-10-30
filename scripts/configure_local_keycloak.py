# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Prepare a private, loopback-only Keycloak sandbox without starting services."""

from __future__ import annotations

import argparse
import base64
import json
import os
import secrets
from pathlib import Path
from urllib.parse import urlsplit


def loopback_origin(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "localhost"}
        or parsed.username
        or parsed.password
        or parsed.path
        or parsed.query
        or parsed.fragment
        or parsed.port is None
    ):
        raise ValueError("Choose an exact HTTP loopback origin with an explicit port")
    return value


def realm_configuration(api_origin: str, browser_origin: str) -> dict:
    return {
        "realm": "forge",
        "enabled": True,
        "displayName": "firefly forge",
        "loginTheme": "forge",
        "registrationAllowed": False,
        "resetPasswordAllowed": False,
        "rememberMe": False,
        "bruteForceProtected": True,
        "sslRequired": "none",
        "roles": {"client": {"forge-api": [{"name": "forge-admin"}, {"name": "forge-worker"}]}},
        "clientScopeMappings": {"forge-api": [{"client": "forge-studio", "roles": ["forge-admin"]}]},
        "clients": [
            {
                "clientId": "forge-api",
                "protocol": "openid-connect",
                "enabled": True,
                "bearerOnly": True,
                "standardFlowEnabled": False,
                "directAccessGrantsEnabled": False,
                "serviceAccountsEnabled": False,
                "fullScopeAllowed": False,
            },
            {
                "clientId": "forge-studio",
                "name": "firefly forge",
                "protocol": "openid-connect",
                "enabled": True,
                "publicClient": False,
                "secret": "${FORGE_LOCAL_CLIENT_SECRET}",
                "clientAuthenticatorType": "client-secret",
                "standardFlowEnabled": True,
                "implicitFlowEnabled": False,
                "directAccessGrantsEnabled": False,
                "serviceAccountsEnabled": False,
                "fullScopeAllowed": False,
                "redirectUris": [api_origin + "/api/v1/auth/callback"],
                "webOrigins": [browser_origin],
                "attributes": {"pkce.code.challenge.method": "S256"},
                "defaultClientScopes": ["profile", "email", "roles"],
                "protocolMappers": [
                    {
                        "name": "forge-api-audience",
                        "protocol": "openid-connect",
                        "protocolMapper": "oidc-audience-mapper",
                        "config": {
                            "included.client.audience": "forge-api",
                            "access.token.claim": "true",
                            "id.token.claim": "false",
                        },
                    }
                ],
            },
        ],
    }


def write_private(path: Path, text: str, mode: int = 0o600) -> None:
    with open(path, "x", encoding="utf-8", opener=lambda name, flags: os.open(name, flags, mode)) as stream:
        os.fchmod(stream.fileno(), mode)
        stream.write(text)


def configure(
    output: Path,
    *,
    api_origin: str = "http://127.0.0.1:8787",
    browser_origin: str | None = None,
    keycloak_origin: str = "http://127.0.0.1:18080",
) -> None:
    api_origin = loopback_origin(api_origin)
    browser_origin = loopback_origin(browser_origin or api_origin)
    keycloak_origin = loopback_origin(keycloak_origin)
    api, browser, provider = (urlsplit(value) for value in (api_origin, browser_origin, keycloak_origin))
    if api.hostname != browser.hostname:
        raise ValueError("Use the same loopback hostname for Studio and the API so session cookies remain same-site")
    if provider.port in {api.port, browser.port}:
        raise ValueError("Choose a Keycloak port distinct from the API and Studio ports")
    output = output.expanduser().absolute()
    if any(char in str(output) for char in "\n\r\x00'\"$`\\"):
        raise ValueError("Choose a configuration path without shell or interpolation characters")
    if output.parent.resolve() != output.parent:
        raise ValueError("Choose a canonical configuration parent without symlinks")
    output.mkdir(mode=0o700)
    client_secret = secrets.token_urlsafe(48)
    key = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()
    # The mounted realm contains no credentials; it must be readable by Keycloak's different container UID.
    write_private(
        output / "forge-realm.json", json.dumps(realm_configuration(api_origin, browser_origin), indent=2) + "\n", 0o644
    )
    keycloak = {
        "FORGE_KEYCLOAK_CONFIG": str(output),
        "FORGE_KEYCLOAK_ORIGIN": keycloak_origin,
        "FORGE_KEYCLOAK_PORT": str(urlsplit(keycloak_origin).port),
        "KC_BOOTSTRAP_ADMIN_USERNAME": "forge-admin",
        "KC_BOOTSTRAP_ADMIN_PASSWORD": secrets.token_urlsafe(48),
        "FORGE_LOCAL_CLIENT_SECRET": client_secret,
    }
    forge = {
        "FORGE_AUTH_MODE": "oidc",
        "FORGE_OIDC_ISSUER": keycloak_origin + "/realms/forge",
        "FORGE_OIDC_CLIENT_ID": "forge-studio",
        "FORGE_OIDC_CLIENT_SECRET": client_secret,
        "FORGE_OIDC_API_AUDIENCE": "forge-api",
        "FORGE_OIDC_REDIRECT_URI": api_origin + "/api/v1/auth/callback",
        "FORGE_BROWSER_ORIGIN": browser_origin,
        "FORGE_OIDC_ALLOW_LOOPBACK_HTTP": "true",
        "FORGE_IDENTITY_ENCRYPTION_KEY": key,
    }
    for name, values in [("keycloak.env", keycloak), ("forge.env", forge)]:
        write_private(
            output / name,
            "# Private local configuration. Do not commit or publish.\n"
            + "".join(f'{key}="{value}"\n' for key, value in values.items()),
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path, help="New private directory; existing directories are never overwritten")
    parser.add_argument("--api-origin", default="http://127.0.0.1:8787")
    parser.add_argument("--browser-origin")
    parser.add_argument("--keycloak-origin", default="http://127.0.0.1:18080")
    args = parser.parse_args()
    configure(
        args.output,
        api_origin=args.api_origin,
        browser_origin=args.browser_origin,
        keycloak_origin=args.keycloak_origin,
    )
    print("Local identity configuration prepared. No services started. Follow deploy/keycloak/README.md.")


if __name__ == "__main__":
    main()
