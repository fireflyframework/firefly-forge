# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Actual TLS handshakes against the native controller, separate from ASGI authorization."""

import asyncio
import datetime
import ipaddress
import socket
import ssl
from contextlib import asynccontextmanager
from dataclasses import replace

import httpx
import pytest
import uvicorn
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from test_runner_controller import (
    TOKEN,
    controller_schema,  # noqa: F401
    settings,
)

from firefly_forge.runner_controller import create_runner_application
from firefly_forge.runner_controller_config import RunnerServerTLS, RunnerWorkerTransport


def rpc_client(transport):
    import importlib

    try:
        return importlib.import_module("firefly_forge.runner_controller_client").RunnerControllerClient(transport)
    except ModuleNotFoundError:
        pytest.fail("Bounded native controller RPC client is not implemented")


def certificates(directory):
    directory.mkdir(mode=0o700)
    now = datetime.datetime.now(datetime.UTC)
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Owned runner test CA")])
    ca = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=1))
        .not_valid_after(now + datetime.timedelta(hours=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(x509.KeyUsage(True, False, False, False, False, True, True, False, False), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    (directory / "ca.pem").write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    for name, purpose in (("server", ExtendedKeyUsageOID.SERVER_AUTH), ("client", ExtendedKeyUsageOID.CLIENT_AUTH)):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        builder = (
            x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)]))
            .issuer_name(subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(minutes=1))
            .not_valid_after(now + datetime.timedelta(hours=1))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.KeyUsage(True, False, True, False, False, False, False, False, False), critical=True)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
            .add_extension(x509.ExtendedKeyUsage([purpose]), critical=False)
        )
        if name == "server":
            builder = builder.add_extension(
                x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False
            )
        cert = builder.sign(ca_key, hashes.SHA256())
        (directory / (name + ".pem")).write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        (directory / (name + ".key")).write_bytes(
            key.private_bytes(
                serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
            )
        )
    for path in directory.iterdir():
        path.chmod(0o600)
    return directory


@asynccontextmanager
async def tls_controller(cfg, app=None):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(64)
    port = listener.getsockname()[1]
    config = uvicorn.Config(
        app or create_runner_application(cfg),
        host="127.0.0.1",
        port=port,
        log_level="error",
        **cfg.tls.uvicorn_settings(),
    )
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        async with asyncio.timeout(10):
            while not server.started:
                if task.done():
                    await task
                    raise RuntimeError("Native controller did not start")
                await asyncio.sleep(0.01)
        yield "https://127.0.0.1:" + str(port)
    finally:
        server.should_exit = True
        await task
        listener.close()


@pytest.mark.usefixtures("controller_schema")
async def test_actual_controller_tls_rejects_missing_or_untrusted_client_certificate(
    tmp_path, postgres_schema, monkeypatch
):
    import os

    monkeypatch.setenv("FORGE_RUNNER_DATABASE_URL", os.environ["FORGE_DATABASE_URL"])
    trusted = certificates(tmp_path / "trusted")
    untrusted = certificates(tmp_path / "untrusted")
    cfg = replace(
        settings(tmp_path, postgres_schema),
        tls=RunnerServerTLS(
            certificate=trusted / "server.pem", private_key=trusted / "server.key", client_ca=trusted / "ca.pem"
        ),
    )
    async with tls_controller(cfg) as endpoint:
        transport = RunnerWorkerTransport(
            active=True,
            endpoint=endpoint,
            certificate=trusted / "client.pem",
            private_key=trusted / "client.key",
            ca=trusted / "ca.pem",
            token=TOKEN,
            profile_id=cfg.authority.profile.profile_id,
            profile_revision=cfg.authority.profile.revision,
        )
        rpc = rpc_client(transport)
        assert (await rpc.profile())["status"] == "inactive"
        wrong = rpc_client(transport.model_copy(update={"profile_revision": "a" * 64}))
        with pytest.raises(RuntimeError, match="profile"):
            await wrong.profile()
        async with httpx.AsyncClient(verify=transport.client_context(), trust_env=False, timeout=3) as client:
            response = await client.get(endpoint + "/runner/v1/profile", headers={"Authorization": "Bearer " + TOKEN})
            assert response.status_code == 200 and response.json()["status"] == "inactive"
            assert (await client.get(endpoint + "/runner/v1/profile")).status_code == 401
        contexts = [
            ssl.create_default_context(cafile=str(trusted / "ca.pem")),
            ssl.create_default_context(cafile=str(trusted / "ca.pem")),
        ]
        contexts[1].load_cert_chain(str(untrusted / "client.pem"), str(untrusted / "client.key"))
        for context in contexts:
            async with httpx.AsyncClient(verify=context, trust_env=False, timeout=3) as client:
                with pytest.raises(httpx.TransportError):
                    await client.get(endpoint + "/runner/v1/profile", headers={"Authorization": "Bearer " + TOKEN})


@pytest.mark.usefixtures("controller_schema")
async def test_actual_tls_rpc_observation_is_bound_to_complete_invocation(tmp_path, store, monkeypatch):
    import os
    from uuid import uuid4

    from sqlalchemy import text

    from firefly_forge.runner_pool_models import FactoryFence, RunnerInvocation
    from firefly_forge.runner_pools import RunnerPools

    monkeypatch.setenv("FORGE_RUNNER_DATABASE_URL", os.environ["FORGE_DATABASE_URL"])
    trusted = certificates(tmp_path / "trusted")
    cfg = replace(
        settings(tmp_path, store.schema),
        tls=RunnerServerTLS(
            certificate=trusted / "server.pem", private_key=trusted / "server.key", client_ca=trusted / "ca.pem"
        ),
    )
    await store.acquire_ownership()
    async with store._unit() as session:
        started = await session.scalar(
            text("SELECT backend_start FROM pg_stat_activity WHERE pid=:pid"), {"pid": store._ownership_pid}
        )
    fence = FactoryFence(factory_schema=store.schema, pid=store._ownership_pid, backend_started_at=started)
    app = create_runner_application(cfg)
    async with tls_controller(cfg, app=app) as endpoint:
        transport = RunnerWorkerTransport(
            active=True,
            endpoint=endpoint,
            certificate=trusted / "client.pem",
            private_key=trusted / "client.key",
            ca=trusted / "ca.pem",
            token=TOKEN,
            profile_id=cfg.authority.profile.profile_id,
            profile_revision=cfg.authority.profile.revision,
        )
        rpc = rpc_client(transport)
        profile = cfg.authority.profile
        request = RunnerInvocation(
            execution_id=uuid4().hex,
            profile_id=profile.profile_id,
            profile_revision=profile.revision,
            project_id=uuid4(),
            repository_id=uuid4(),
            run_id=uuid4(),
            image=profile.approved_images[0],
            argv=("true",),
            source_digest="b" * 64,
            archive_digest="c" * 64,
            source_bytes=10240,
        )
        pools = app.state.runtime.context.get_bean(RunnerPools)
        await pools.submit(profile, request, fence=fence)
        assert (await rpc.observe(request))["status"] == "queued"
        other_project = request.model_copy(update={"project_id": uuid4()})
        with pytest.raises(RuntimeError, match="refused"):
            await rpc.observe(other_project)


@pytest.mark.usefixtures("controller_schema")
async def test_rpc_absolute_deadline_bounds_a_stalled_authenticated_response(tmp_path, postgres_schema, monkeypatch):
    import importlib
    import os

    from firefly_forge.runner_controller import RunnerControllerService

    monkeypatch.setenv("FORGE_RUNNER_DATABASE_URL", os.environ["FORGE_DATABASE_URL"])
    module = importlib.import_module("firefly_forge.runner_controller_client")
    monkeypatch.setattr(module, "RPC_DEADLINE_SECONDS", 0.02, raising=False)

    async def stalled(self, request):
        await asyncio.sleep(0.15)
        return {"status": "delayed"}

    monkeypatch.setattr(RunnerControllerService, "observe", stalled)
    trusted = certificates(tmp_path / "trusted")
    cfg = replace(
        settings(tmp_path, postgres_schema),
        tls=RunnerServerTLS(
            certificate=trusted / "server.pem", private_key=trusted / "server.key", client_ca=trusted / "ca.pem"
        ),
    )
    async with tls_controller(cfg) as endpoint:
        transport = RunnerWorkerTransport(
            active=True,
            endpoint=endpoint,
            certificate=trusted / "client.pem",
            private_key=trusted / "client.key",
            ca=trusted / "ca.pem",
            token=TOKEN,
            profile_id=cfg.authority.profile.profile_id,
            profile_revision=cfg.authority.profile.revision,
        )
        with pytest.raises(TimeoutError):
            await module.RunnerControllerClient(transport)._request(
                "observe", {"execution_id": "a" * 32, "scope": "b" * 64}
            )
