# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""OAuth's absent expiry remains absent and authenticated in durable custody."""

from dataclasses import asdict
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID

import pytest
from cryptography.fernet import Fernet
from pyfly.oauth2 import OAuth2Tokens

from firefly_forge import github_projects_oauth
from firefly_forge.connection_oauth import ConnectionOAuth
from firefly_forge.connection_oauth_models import OAuthDisposition


@pytest.mark.parametrize("expiry", [None, datetime(2026, 10, 31, tzinfo=UTC)])
def test_grant_cipher_context_preserves_provider_expiry_and_owner(expiry):
    oauth = ConnectionOAuth.__new__(ConnectionOAuth)
    oauth.store = SimpleNamespace(schema="forge_test_projects")
    oauth.cipher = Fernet(Fernet.generate_key())
    row = SimpleNamespace(id=UUID(int=1))
    grant = SimpleNamespace(
        registration_revision=2,
        revision=3,
        family="a" * 32,
        actor_id=UUID(int=4),
        data={"issuer": "https://github.com", "resource": "https://api.github.com/graphql", "scopes": ["project"]},
        expires_at=expiry,
    )
    context = oauth._grant_context(row, grant)
    assert context["expires_at"] == (expiry.isoformat() if expiry is not None else None)
    packet = {"access_token": "synthetic-projects-test-token", "refresh_token": None}
    sealed = oauth._seal(context, packet)
    assert oauth._open(sealed, context) == packet
    grant.actor_id = UUID(int=5)
    with pytest.raises(ValueError, match="context"):
        oauth._open(sealed, oauth._grant_context(row, grant))


@pytest.mark.parametrize("provider", ["github-projects", "github-projects-private"])
def test_exact_github_policy_accepts_absent_expiry_without_inventing_one(provider):
    policy = asdict(github_projects_oauth.github_policy(provider).provider)
    now = datetime(2026, 10, 31, tzinfo=UTC)
    expiry = getattr(github_projects_oauth, "token_expiry", None)
    assert callable(expiry), "Publish must validate expiry against the exact admitted provider policy"
    assert expiry(policy, OAuth2Tokens("synthetic", "bearer"), now) is None
    assert expiry(policy, OAuth2Tokens("synthetic", "bearer", 3600), now).timestamp() == now.timestamp() + 3600


@pytest.mark.parametrize("mutation", ["id", "issuer", "resource", "scopes", "authentication"])
def test_nonexpiring_token_does_not_expand_other_or_forged_provider_policy(mutation):
    policy = asdict(github_projects_oauth.github_policy("github-projects").provider)
    policy[mutation] = "foreign" if mutation != "scopes" else ["project", "repo"]
    expiry = getattr(github_projects_oauth, "token_expiry", None)
    assert callable(expiry), "Publish must validate expiry against the exact admitted provider policy"
    with pytest.raises(OAuthDisposition, match="token_contract_invalid"):
        expiry(policy, OAuth2Tokens("synthetic", "bearer"), datetime.now(UTC))


@pytest.mark.parametrize("seconds", [0, -1, float("nan"), float("inf"), 86401, True])
def test_invalid_provider_expiry_is_not_an_unlimited_grant(seconds):
    policy = asdict(github_projects_oauth.github_policy("github-projects").provider)
    expiry = getattr(github_projects_oauth, "token_expiry", None)
    assert callable(expiry), "Publish must validate expiry against the exact admitted provider policy"
    with pytest.raises(OAuthDisposition, match="token_contract_invalid"):
        expiry(policy, OAuth2Tokens("synthetic", "bearer", seconds), datetime.now(UTC))
