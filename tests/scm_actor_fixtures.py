# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Native SCM setup using real identity and application code with a synthetic provider transport."""

from datetime import UTC, datetime, timedelta

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from worker_reply_fixtures import authenticated_operator

from firefly_forge.channel_actions import BootstrapSettings, RepositoryChannels
from firefly_forge.collaboration import Collaboration, CollaborationSettings
from firefly_forge.journal import digest
from firefly_forge.project_access import ProjectAccess
from firefly_forge.project_models import ProjectDefinition
from firefly_forge.projects import Projects
from firefly_forge.scm_actor_authority import SCMActorAuthority
from firefly_forge.scm_authority_models import SCMActorBindingInputV1
from firefly_forge.scm_connections import SCMConnections


async def native_mapped_channel(store):
    key = (
        rsa.generate_private_key(public_exponent=65537, key_size=2048)
        .private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
        .decode()
    )

    def provider(request):
        from pyfly.data.transaction import current_unit_of_work

        assert current_unit_of_work() is None
        if request.url.path == "/app/installations/34/access_tokens":
            return httpx.Response(
                201,
                json={
                    "token": "owned-native-channel-fixture-token",
                    "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
                },
            )
        assert request.method == "GET" and request.url.path == "/installation/repositories"
        return httpx.Response(
            200,
            json={
                "repositories": [
                    {
                        "id": 42,
                        "full_name": "team/product",
                        "clone_url": "https://github.com/team/product.git",
                        "default_branch": "main",
                    }
                ]
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(provider))
    scm = SCMConnections(store, client=client)
    try:
        await scm.configure("github", {"values": {"app_id": "12", "installation_id": "34", "private_key": key}})
        repo = await scm.connect("github", "42")
        collaboration = Collaboration(store, scm)
        await collaboration.configure(
            repo["id"],
            CollaborationSettings(
                enabled=True,
                trusted_actors=["7"],
                configuration_actors=["7"],
                channel_commands=["respond"],
                webhook_secret="global-webhook-secret",
            ),
        )
        channels = RepositoryChannels(store, collaboration, scm)
        await channels.configure(
            "github",
            BootstrapSettings(
                enabled=True,
                trusted_actors=["7"],
                configuration_actors=["7"],
                webhook_secret="global-webhook-secret",
            ),
        )
        project = await Projects(store).configure(
            ProjectDefinition(
                name="Signed clarification",
                description="Existing owner-authorized component",
                topology="monorepo",
                owners=["Owner"],
                components=[dict(id="api", name="API", repository_id=repo["id"], responsibility="Serve requests")],
            )
        )
        return channels, collaboration, repo, project, client
    except BaseException:
        await client.aclose()
        raise


async def map_native_delivery(store, collaboration, project, repository, delivery):
    async with authenticated_operator(store) as owner:
        await ProjectAccess(store).set_membership(project["id"], owner, "owner")
        return await SCMActorAuthority(store, collaboration.scm).configure(
            project["id"],
            repository["id"],
            SCMActorBindingInputV1(
                version=1,
                request_id="a" * 32,
                expected_previous_digest=None,
                expected_project_binding_digest=digest(await Projects(store).project(project["id"])),
                verified_receipt_id=delivery["id"],
                expected_verified_reference_digest=digest(delivery["verification"]),
                native_user_id=owner.id.hex,
                component_ids=["api"],
                allowed_actions=["respond"],
                active=True,
                acknowledge_webhook_asserted_identity=True,
            ),
        )
