# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Host admission fences; these controls do not qualify native transactions."""

import pytest

from firefly_forge.project_access import AccessDenied
from firefly_forge.scm_actor_authority import SCMActorAuthority


@pytest.mark.parametrize("permission", ["execute", "release"])
async def test_status_delegation_never_confers_write_permission(permission):
    service = object.__new__(SCMActorAuthority)

    async def unexpected(*args):
        raise AssertionError("A read-only action must refuse before reading a write target")

    service._project = unexpected
    context = dict(
        project_id="1" * 32,
        repository_id="2" * 32,
        component_id="api",
        run_id="3" * 32,
        binding_key="4" * 64,
        binding_digest="5" * 64,
        binding_request_id="6" * 32,
        verified_receipt_id="7" * 32,
        verified_reference={},
        action="status",
    )
    with pytest.raises(AccessDenied):
        await service.authorize_snapshot(
            {"global_admin": False, "api_audience": None, "scm_delegation": context},
            context["project_id"],
            permission,
        )


def test_connection_delegation_scope_uses_one_configuration_cut():
    from firefly_forge.scm_connections import SCMConnections

    service = object.__new__(SCMConnections)
    reads = []

    def record(provider):
        reads.append(provider)
        assert len(reads) == 1, "A verification cut must not mix revisions from separate configuration reads"
        return dict(revision="a" * 32, values=dict(app_id="12", installation_id="34", private_key="owned-fixture"))

    service._record = record
    cut = service.delegation_scope(dict(connection_provider="github", remote_repository_id="42"))
    assert cut["connection_revision"] == "a" * 32
    assert cut["installation_id"] == "34" and cut["api_origin"] == "https://api.github.com"
    assert reads == ["github"]
