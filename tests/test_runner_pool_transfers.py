# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Frozen receipt budgets and current native authority precede every artifact transfer."""

from uuid import uuid4

import pytest
from test_runner_pool_worker import fixture

from firefly_forge.runner_factory_models import FactoryPoolSelection, capture_factory_pool


@pytest.mark.parametrize("kind", ["output", "result"])
@pytest.mark.parametrize(
    "change",
    [
        {"bytes": True},
        {"bytes": -1},
        {"bytes": "10"},
        {"bytes": 10.0},
        {"sha256": None},
        {"sha256": "not-a-sha256"},
        {"kind": "source"},
        {"unexpected": "field"},
        {"bytes": 269_484_033},
    ],
)
async def test_invalid_artifact_receipt_prevents_both_transfers(store, tmp_path, kind, change):
    worker, controller, run, journal, workspace = await fixture(store, tmp_path)
    observed = controller.observe
    fetched = []

    async def invalid(invocation):
        result = await observed(invocation)
        result["receipt"][kind].update(change)
        return result

    async def artifact(invocation, name, receipt):
        fetched.append(name)
        return controller.returned if name == "result" else b"completed\n"

    controller.observe, controller.artifact = invalid, artifact
    with pytest.raises(RuntimeError, match="reconciliation"):
        await worker.execute(run["id"], workspace, workspace, ["git", "diff"], 5, "verify", ordinal=1)
    assert fetched == []
    assert (workspace / "main.txt").read_text() == "original\n"
    assert (await journal.sandbox_executions(run["id"]))[0]["effects"]["start"]["status"] == "intent"


async def test_factory_workspace_budget_is_stricter_than_the_profile_result_budget(store, tmp_path):
    worker, controller, run, journal, workspace = await fixture(store, tmp_path)
    snapshot = (await store.get_run(run["id"]))["sandbox_snapshot"]
    policy = FactoryPoolSelection.model_validate(snapshot["configuration"])
    smaller = policy.model_copy(update={"workspace_max_bytes": 1_048_576})
    await store.update(run["id"], sandbox_snapshot=capture_factory_pool(smaller))
    observed = controller.observe
    fetched = []

    async def excessive(invocation):
        result = await observed(invocation)
        result["receipt"]["result"]["bytes"] = smaller.workspace_max_bytes + 1_048_577
        return result

    async def artifact(invocation, kind, receipt):
        fetched.append(kind)
        return controller.returned if kind == "result" else b"completed\n"

    controller.observe, controller.artifact = excessive, artifact
    with pytest.raises(RuntimeError, match="reconciliation"):
        await worker.execute(run["id"], workspace, workspace, ["git", "diff"], 5, "verify", ordinal=1)
    assert fetched == []
    assert (workspace / "main.txt").read_text() == "original\n"


@pytest.mark.parametrize("boundary", ["completion", "output", "result"])
@pytest.mark.parametrize("lost", ["owner", "project"])
async def test_current_owner_and_project_grants_fence_each_transfer(store, tmp_path, boundary, lost):
    worker, controller, run, journal, workspace = await fixture(store, tmp_path)
    revoke = store.release_ownership
    if lost == "project":
        from firefly_forge.identity import Principal
        from firefly_forge.models import RunInput
        from firefly_forge.project_access import ProjectAccess
        from firefly_forge.project_models import ProjectDefinition
        from firefly_forge.projects import Projects

        projects = Projects(store)
        project = await projects.configure(
            ProjectDefinition(
                name="Owned project",
                description="Own transfers",
                topology="monorepo",
                owners=["Owner"],
                components=[
                    dict(id="service", name="Service", repository_id=run["repository_id"], responsibility="Own source")
                ],
            )
        )
        actor = Principal(uuid4(), "https://issuer.invalid", "owned-transfer-user")
        access = ProjectAccess(store)
        await access.set_membership(project["id"], actor, "developer")
        snapshot = await projects.component_snapshot(
            RunInput(
                repository_id=run["repository_id"],
                project_id=project["id"],
                component_id="service",
                brief="Own transfer",
            )
        )
        await store.update(
            run["id"],
            project_id=project["id"],
            component_id="service",
            component_snapshot=snapshot,
            agent="agentic",
            execution_actor=access.actor_snapshot(actor),
        )

        async def revoke():
            await access.set_membership(project["id"], actor, None)

    observed, transferred = controller.observe, controller.artifact
    observations, fetched = 0, []

    async def completion(invocation):
        nonlocal observations
        observations += 1
        result = await observed(invocation)
        if observations == 2 and boundary == "completion":
            await revoke()
        return result

    async def artifact(invocation, kind, receipt):
        fetched.append(kind)
        data = await transferred(invocation, kind, receipt)
        if boundary == kind:
            await revoke()
        return data

    controller.observe, controller.artifact = completion, artifact
    with pytest.raises(RuntimeError, match="reconciliation"):
        await worker.execute(run["id"], workspace, workspace, ["git", "diff"], 5, "verify", ordinal=1)
    assert fetched == {"completion": [], "output": ["output"], "result": ["output", "result"]}[boundary]
    assert (workspace / "main.txt").read_text() == "original\n"
    row = (await journal.sandbox_executions(run["id"]))[0]
    assert row["effects"]["start"]["status"] == "intent"
    assert not (worker.artifacts.directory / row["execution_id"] / "result.bin").exists()
    if boundary in {"completion", "output"}:
        assert not (worker.artifacts.directory / row["execution_id"] / "output.bin").exists()


@pytest.mark.parametrize(
    "change", [{"bytes": True}, {"bytes": -1}, {"sha256": "bad"}, {"kind": "source"}, {"bytes": 1_000_001}]
)
async def test_rpc_client_refuses_invalid_output_receipts_before_any_request(tmp_path, change):
    from test_runner_pool_artifacts import fixture_request

    from firefly_forge.runner_controller_client import RunnerControllerClient
    from firefly_forge.runner_controller_config import RunnerWorkerTransport

    profile, invocation, archive, root = fixture_request(tmp_path)
    transport = RunnerWorkerTransport(
        active=True,
        endpoint="https://127.0.0.1:9917",
        certificate=tmp_path / "client.pem",
        private_key=tmp_path / "client.key",
        ca=tmp_path / "ca.pem",
        token="private-direct-client-test-token-32chars",
        profile_id=profile.profile_id,
        profile_revision=profile.revision,
    )
    client = RunnerControllerClient(transport)
    requests = []

    async def request(*args, **kwargs):
        requests.append(args)
        raise AssertionError("Invalid artifact receipt dispatched a request")

    client._request = request
    receipt = {"kind": "output", "bytes": 0, "sha256": "a" * 64, **change}
    with pytest.raises(ValueError):
        await client.artifact(invocation, "output", receipt)
    assert requests == []


async def test_empty_output_receipt_keeps_its_exact_identity_and_minimum_response_bound(tmp_path):
    import hashlib

    from test_runner_pool_artifacts import fixture_request

    from firefly_forge.runner_controller_client import RunnerControllerClient
    from firefly_forge.runner_controller_config import RunnerWorkerTransport

    profile, invocation, archive, root = fixture_request(tmp_path)
    client = RunnerControllerClient(
        RunnerWorkerTransport(
            active=True,
            endpoint="https://127.0.0.1:9917",
            certificate=tmp_path / "client.pem",
            private_key=tmp_path / "client.key",
            ca=tmp_path / "ca.pem",
            token="private-direct-client-test-token-32chars",
            profile_id=profile.profile_id,
            profile_revision=profile.revision,
        )
    )
    receipt = {"kind": "output", "bytes": 0, "sha256": hashlib.sha256(b"").hexdigest()}
    limits = []

    async def request(path, payload, *, limit):
        assert path == "artifact" and payload["kind"] == "output"
        limits.append(limit)
        return {"receipt": receipt, "data_base64": ""}

    client._request = request
    assert await client.artifact(invocation, "output", receipt) == b"" and limits == [4096]
