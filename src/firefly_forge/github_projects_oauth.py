# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Separately consented GitHub OAuth App profiles, including provider-owned expiry."""

import math
from dataclasses import asdict
from datetime import datetime, timedelta
from typing import Any

from pyfly.oauth2 import OAuth2Tokens

from firefly_forge.connection_oauth_models import OAuthDisposition, OAuthPolicyBinding, OAuthProvider
from firefly_forge.journal import digest

GITHUB_PROJECTS_PROVIDERS = ("github-projects", "github-projects-private")


def github_policy(name: str) -> OAuthPolicyBinding | None:
    if name not in GITHUB_PROJECTS_PROVIDERS:
        return None
    provider = OAuthProvider(
        id=name,
        issuer="https://github.com",
        authorization_endpoint="https://github.com/login/oauth/authorize",
        token_endpoint="https://github.com/login/oauth/access_token",
        resource="https://api.github.com/graphql",
        scopes=("project", "repo") if name.endswith("-private") else ("project",),
    )
    return OAuthPolicyBinding(provider, 0, digest(asdict(provider)))


def nonexpiring_policy(policy: dict[str, Any]) -> bool:
    binding = github_policy(policy.get("id", ""))
    if binding is None:
        return False
    return {**policy, "scopes": tuple(policy.get("scopes", ()))} == asdict(binding.provider)


def token_expiry(policy: dict[str, Any], tokens: OAuth2Tokens, now: datetime) -> datetime | None:
    seconds = tokens.expires_in
    if tokens.token_type.lower() != "bearer":
        raise OAuthDisposition("token_contract_invalid")
    if seconds is None:
        if not nonexpiring_policy(policy):
            raise OAuthDisposition("token_contract_invalid")
        return None
    if type(seconds) not in {int, float} or not math.isfinite(seconds) or not 0 < seconds <= 86400:
        raise OAuthDisposition("token_contract_invalid")
    return now + timedelta(seconds=seconds)


def token_resource(provider: str, resource: str) -> str | None:
    return None if provider in GITHUB_PROJECTS_PROVIDERS else resource


def granted_scopes(provider: str, scope: str) -> set[str]:
    return set((scope.replace(",", " ") if provider in GITHUB_PROJECTS_PROVIDERS else scope).split())


def callback_issuer(provider: str, supplied: str, expected: str) -> str:
    if not supplied and provider in GITHUB_PROJECTS_PROVIDERS and expected == "https://github.com":
        # GitHub's code/state callback has no RFC9207 issuer parameter. The
        # retained registration, exact redirect, state, nonce and PKCE still bind it.
        return expected
    return supplied
