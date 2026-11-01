# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
import pytest

from firefly_forge.github_projects_oauth import callback_issuer, github_policy, granted_scopes, token_resource


def test_explicit_profiles_do_not_expand_existing_github_app():
    public = github_policy("github-projects")
    private = github_policy("github-projects-private")
    assert public.provider.scopes == ("project",)
    assert private.provider.scopes == ("project", "repo")
    assert public.provider.resource == "https://api.github.com/graphql"
    assert public.provider.issuer == "https://github.com"
    assert github_policy("github") is None


@pytest.mark.parametrize("provider", ["github-projects", "github-projects-private"])
def test_github_scope_delimiters_and_resource_control(provider):
    assert granted_scopes(provider, "project,repo") == {"project", "repo"}
    assert granted_scopes(provider, "project repo") == {"project", "repo"}
    assert granted_scopes(provider, "project,repo,offline_access") == {"project", "repo", "offline_access"}
    assert token_resource(provider, "https://api.github.com/graphql") is None


def test_other_providers_keep_exact_scope_and_resource_behavior():
    assert granted_scopes("local-repository-tools", "repository:read,unexpected") == {"repository:read,unexpected"}
    assert token_resource("local-repository-tools", "https://service.example/mcp") == "https://service.example/mcp"


def test_only_pinned_github_policy_accepts_absent_callback_issuer():
    assert callback_issuer("github-projects", "", "https://github.com") == "https://github.com"
    assert callback_issuer("github-projects", "https://evil.example", "https://github.com") == "https://evil.example"
    assert callback_issuer("local-repository-tools", "", "https://service.example") == ""


async def test_native_catalog_and_policy_directory_do_not_duplicate_github_profiles():
    from types import SimpleNamespace

    from cryptography.fernet import Fernet

    from firefly_forge.connection_oauth import ConnectionOAuth

    async def admit(value):
        pass

    oauth = ConnectionOAuth(
        SimpleNamespace(schema="forge_projects_catalog_test", admit_credentials=admit),
        encryption_key=Fernet.generate_key().decode(),
        browser_origin="https://forge.example",
        catalog={name: github_policy(name).provider for name in ("github-projects", "github-projects-private")},
    )
    directory = await oauth.provider_directory(object())
    assert len(directory) == len({row["provider"] for row in directory}) == 7
