# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Supported administrator configuration, separate from provider credentials."""

from typing import Any

from pyfly.container import rest_controller
from pyfly.web import Body, PathVar, get_mapping, put_mapping, request_mapping

from firefly_forge.oauth_provider_policies import OAuthProviderPolicies
from firefly_forge.oauth_provider_policy_models import OAuthProviderPolicyInput
from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied


@rest_controller
@request_mapping("/api/v1/oauth-provider-policies")
class OAuthProviderPolicyController:
    def __init__(self, policies: OAuthProviderPolicies) -> None:
        self.policies = policies

    @get_mapping("")
    async def directory(self) -> dict[str, Any]:
        actor = CURRENT_PRINCIPAL.get()
        if actor is None:
            raise AccessDenied("Sign in as a factory administrator")
        return await self.policies.directory(actor)

    @put_mapping("/{name}")
    async def configure(self, name: PathVar[str], body: Body[OAuthProviderPolicyInput]) -> dict[str, Any]:
        actor = CURRENT_PRINCIPAL.get()
        if actor is None:
            raise AccessDenied("Sign in as a factory administrator")
        return await self.policies.configure(name, body, actor)
