# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""The typed private renewal route binds its request and ACK to the exact invocation."""

from types import SimpleNamespace

import pytest
from pydantic import SecretStr, ValidationError
from test_run_owned_authority_host import case

from firefly_forge import runner_controller as server
from firefly_forge.runner_controller_client import RunnerControllerClient
from firefly_forge.runner_run_liveness import RunLease, run_digest


@pytest.mark.parametrize("forged", [None, "controller", "sequence", "worker"])
async def test_client_renews_once_and_rejects_foreign_ack_without_transport_retry(monkeypatch, forged):
    invocation, fence, *_ = case()
    transport = SimpleNamespace(
        profile_id=invocation.profile_id,
        profile_revision=invocation.profile_revision,
        token=SecretStr("fictional-private-controller-credential"),
    )
    client = RunnerControllerClient(transport)
    assert hasattr(client, "renew_run") and hasattr(server, "PoolRunRenewal"), "Typed run renewal transport is missing"
    lease = RunLease(run_digest(invocation.model_dump(mode="json")), invocation.run_liveness, "e" * 32, 30)
    calls = []

    async def request(path, payload):
        body = server.PoolRunRenewal.model_validate(payload)
        calls.append((path, payload))
        assert body.execution_id == invocation.execution_id
        assert body.scope == server.invocation_scope(transport.token, invocation.model_dump(mode="json"))
        assert body.fence == fence
        ack = lease.renew(body.renewal)
        if forged == "controller":
            ack["controller_epoch"] = "f" * 32
        if forged == "worker":
            ack["worker_epoch"] = "f" * 32
        if forged == "sequence":
            ack["sequence"] = 2
        return ack

    monkeypatch.setattr(client, "_request", request)
    if forged:
        with pytest.raises(ValueError):
            await client.renew_run(invocation, fence, lease.ack())
    else:
        result = await client.renew_run(invocation, fence, lease.ack())
        assert result["sequence"] == 1 and calls[0][0] == "renew-run"
    assert len(calls) == 1


async def test_service_checks_private_invocation_scope_before_run_renewal():
    invocation, fence, run, *_ = case()
    assert hasattr(server, "PoolRunRenewal"), "Typed run renewal route is missing"
    lease = RunLease(run_digest(invocation.model_dump(mode="json")), invocation.run_liveness, "e" * 32, 30)
    calls = []

    async def observe(profile, identity):
        return {"request": invocation.model_dump(mode="json")}

    async def renew(selected, selected_fence, request):
        calls.append((selected, selected_fence))
        return lease.renew(request)

    service = object.__new__(server.RunnerControllerService)
    service.settings = SimpleNamespace(
        active=True,
        authority=SimpleNamespace(profile=run["sandbox_snapshot"]["configuration"]["profile"]),
        token=SecretStr("fictional-private-controller-credential"),
    )
    service.ready, service.pools, service.executions = (
        True,
        SimpleNamespace(observe=observe),
        SimpleNamespace(renew_run=renew),
    )
    payload = {
        "execution_id": invocation.execution_id,
        "scope": "0" * 64,
        "fence": fence.model_dump(mode="json"),
        "renewal": {**lease.ack(), "protocol": "forge-run-renewal-v1", "sequence": 1},
    }
    with pytest.raises(PermissionError):
        await service.renew_run(server.PoolRunRenewal.model_validate(payload))
    assert calls == []
    payload["scope"] = server.invocation_scope(service.settings.token, invocation.model_dump(mode="json"))
    assert (await service.renew_run(server.PoolRunRenewal.model_validate(payload)))["sequence"] == 1
    assert calls == [(invocation, fence)]
    with pytest.raises(ValidationError):
        server.PoolRunRenewal.model_validate({**payload, "renewal": {**payload["renewal"], "sequence": True}})


def test_controller_advertises_run_renewal_only_for_the_explicit_profile():
    from test_runner_pools import profile

    from firefly_forge.runner_pool_models import RunnerPoolProfile

    _, _, run, *_ = case()
    service = object.__new__(server.RunnerControllerService)
    service.ready = True
    service.settings = SimpleNamespace(
        active=True,
        authority=SimpleNamespace(
            profile=RunnerPoolProfile.model_validate(run["sandbox_snapshot"]["configuration"]["profile"])
        ),
    )
    service.executions = SimpleNamespace(git_snapshot=None)
    assert service.profile()["run_liveness_protocols"] == ["forge-run-owned-liveness-v1"]
    service.settings.authority.profile = profile()
    assert "run_liveness_protocols" not in service.profile()
