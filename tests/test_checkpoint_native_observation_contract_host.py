# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Current observation/authentication contracts; no native authority or SQL is emulated."""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import NAMESPACE_URL, uuid5

import httpx
import pytest
from cryptography.fernet import Fernet
from pydantic import ValidationError
from starlette.requests import Request

from firefly_forge.api import OperatorAuth
from firefly_forge.identity import IdentityConfiguration, IdentityService
from firefly_forge.identity_persistence import ForgeIdentityUserEntity
from firefly_forge.scm_conversation_models import addressed_status
from firefly_forge.scm_polling import SCMPolling


class CurrentPolicyRead(Exception):
    pass


@pytest.mark.parametrize("natural", [False, True])
async def test_current_status_scope_accepts_only_observed_typed_address_before_policy_lookup(natural):
    text = "@forge how is this going?"
    message = addressed_status(text)
    assert message is not None and message.intent == "status"
    command = message.model_dump(mode="json") if natural else {"run_id": "1" * 32}
    delivery = {"id": "2" * 32, "repository_id": "3" * 32, "command": command}
    reads = []

    async def policy(repository):
        reads.append(repository)
        raise CurrentPolicyRead()

    service = SCMPolling.__new__(SCMPolling)
    service._policy = policy
    with pytest.raises(CurrentPolicyRead if natural else ValidationError):
        await service.conversation_scope(delivery, remote=False)
    assert reads == ([delivery["repository_id"]] if natural else [])
    assert "run_id" not in message.model_dump()


async def test_disabled_bootstrap_identity_returns401_before_creation_or_project_authority():
    token = "public-disabled-identity-fixture"
    writes = []

    async def execute(statement):
        writes.append(type(statement).__name__)

    @asynccontextmanager
    async def unit():
        yield SimpleNamespace(execute=execute)

    disabled = ForgeIdentityUserEntity(
        id=uuid5(NAMESPACE_URL, "firefly-forge:local-bootstrap"),
        issuer="local:bootstrap",
        subject="operator",
        enabled=False,
    )

    async def subject(issuer, name):
        assert (issuer, name) == (disabled.issuer, disabled.subject)
        return disabled

    async def forbidden(request):
        raise AssertionError("A disabled native identity must not reach creation or project authorization")

    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: pytest.fail("No provider request"))) as client:
        store = SimpleNamespace(_unit=unit)
        identity = IdentityService(
            store,
            IdentityConfiguration(
                mode="local_bootstrap", bootstrap_token=token, encryption_key=Fernet.generate_key().decode()
            ),
            users=SimpleNamespace(subject=subject),
            client=client,
        )
        auth = OperatorAuth(token, identity, SimpleNamespace(), store)
        auth.routes.authorize = forbidden
        request = Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/api/v1/run-creation-requests/" + "4" * 32,
                "headers": [(b"authorization", ("Bearer " + token).encode())],
                "client": ("127.0.0.1", 52602),
                "server": ("testserver", 80),
                "scheme": "http",
            }
        )
        response = await auth.do_filter(request, forbidden)
    assert response.status_code == 401
    assert response.body == b'{"detail":"Sign in to access the factory"}'
    assert disabled.enabled is False and writes == ["Insert"]
