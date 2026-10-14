# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Native PyFly control boundary. ASGI tests are separate from live mTLS proof."""

import importlib

import pytest
from pydantic import SecretStr
from pyfly.data.relational.sqlalchemy import SessionProvider
from runner_test_support import installed_docker_binary
from starlette.testclient import TestClient

from firefly_forge.runner_controller_config import RunnerServerTLS
from firefly_forge.runner_pool_dispatch import RunnerDockerAuthority
from firefly_forge.runner_pool_models import RunnerPoolProfile
from firefly_forge.runner_pools import RunnerPools

TOKEN = "test-only-controller-credential-32-characters"
IMAGE = "example/worker@sha256:" + "a" * 64


def controller_module():
    try:
        return importlib.import_module("firefly_forge.runner_controller")
    except ModuleNotFoundError:
        pytest.fail("Native trusted controller application is not implemented")


def settings(tmp_path, schema):
    module = controller_module()
    config = tmp_path / "docker"
    config.mkdir(mode=0o700)
    state = tmp_path / "runner-artifacts"
    state.mkdir(mode=0o700)
    tls = []
    for name in ("server.pem", "server.key", "ca.pem"):
        path = tmp_path / name
        path.write_text("ASGI-test-placeholder")
        path.chmod(0o600)
        tls.append(path)
    profile = RunnerPoolProfile(
        profile_id="owned-local", daemon_id="owned-daemon", approved_images=(IMAGE,), transfer_image=IMAGE
    )
    authority = RunnerDockerAuthority(
        profile,
        installed_docker_binary(),
        "colima-forge-tests",
        config,
        state,
        "forge-runner-m2-controller-test",
        10001,
        10001,
    )
    return module.RunnerControllerSettings(
        authority=authority,
        runner_schema=schema + "_runner",
        factory_schema=schema,
        token=SecretStr(TOKEN),
        tls=RunnerServerTLS(certificate=tls[0], private_key=tls[1], client_ca=tls[2]),
    )


def test_native_application_exposes_only_authenticated_typed_runner_operations(
    tmp_path, postgres_schema, controller_schema, monkeypatch
):
    import os

    module = controller_module()
    monkeypatch.setenv("FORGE_RUNNER_DATABASE_URL", os.environ["FORGE_DATABASE_URL"])
    cfg = settings(tmp_path, postgres_schema)
    app = module.create_runner_application(cfg)
    with TestClient(app, base_url="https://testserver") as client:
        runtime = app.state.runtime
        pools = runtime.context.get_bean(RunnerPools)
        assert pools.sessions is runtime.context.get_bean(SessionProvider)
        assert client.get("/runner/v1/profile").status_code == 401
        headers = {"Authorization": "Bearer " + TOKEN}
        result = client.get("/runner/v1/profile", headers=headers)
        assert result.status_code == 200 and result.json()["status"] == "inactive"
        assert result.json()["profile"]["profile_id"] == "owned-local"
        assert TOKEN not in result.text and str(tmp_path) not in result.text
        assert (
            client.post(
                "/runner/v1/invocations", headers=headers, json={"docker_argv": ["run", "--privileged"]}
            ).status_code
            == 422
        )
        assert (
            client.post(
                "/runner/v1/invocations", headers=headers, json={"invocation": {}, "fence": {}, "archive_base64": ""}
            ).status_code
            == 422
        )
        assert client.get("/api/v1/repositories", headers=headers).status_code == 404
        from firefly_forge.store import Store

        assert not runtime.context.container.contains_type(Store)
    with TestClient(app, base_url="http://testserver") as client:
        assert client.get("/runner/v1/profile", headers={"Authorization": "Bearer " + TOKEN}).status_code == 403


def test_invocation_scope_signature_cannot_be_used_for_another_request(tmp_path, postgres_schema):
    module = controller_module()
    cfg = settings(tmp_path, postgres_schema)
    first = {"execution_id": "a" * 32, "repository_id": "b" * 32, "argv": ["python", "test.py"]}
    other = {**first, "execution_id": "c" * 32}
    one = module.invocation_scope(cfg.token, first)
    assert one != module.invocation_scope(cfg.token, other)
    assert one != module.invocation_scope(SecretStr("other-test-token-32-characters-long"), first)


@pytest.fixture
async def controller_schema(postgres_schema):
    import os

    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    schema = postgres_schema + "_runner"
    yield schema
    engine = create_async_engine(os.environ["FORGE_TEST_DATABASE_URL"])
    try:
        async with engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
    finally:
        await engine.dispose()


def test_streaming_runner_body_is_bounded_before_json_parsing(
    tmp_path, postgres_schema, controller_schema, monkeypatch
):
    import os

    module = controller_module()
    monkeypatch.setenv("FORGE_RUNNER_DATABASE_URL", os.environ["FORGE_DATABASE_URL"])
    cfg = settings(tmp_path, postgres_schema)
    app = module.create_runner_application(cfg)
    with TestClient(app, base_url="https://testserver") as client:
        response = client.post(
            "/runner/v1/invocations",
            headers={"Authorization": "Bearer " + TOKEN, "Content-Type": "application/json"},
            content=iter([b" " * (cfg.authority.profile.max_source_bytes * 2)]),
        )
        assert response.status_code == 413


async def test_authenticated_upload_has_an_absolute_deadline_before_parsing(tmp_path, postgres_schema, monkeypatch):
    import asyncio

    module = controller_module()
    monkeypatch.setattr(module, "UPLOAD_DEADLINE_SECONDS", 0.01, raising=False)
    cfg = settings(tmp_path, postgres_schema)
    reached, messages = [], []

    async def app(scope, receive, send):
        reached.append(True)

    async def receive():
        await asyncio.sleep(0.05)
        return {"type": "http.request", "body": b"{}", "more_body": False}

    async def send(message):
        messages.append(message)

    middleware = module.RunnerRequestLimits(app, cfg)
    await middleware(
        {"type": "http", "scheme": "https", "headers": [(b"authorization", ("Bearer " + TOKEN).encode())]},
        receive,
        send,
    )
    assert not reached
    assert next(message["status"] for message in messages if message["type"] == "http.response.start") == 408
