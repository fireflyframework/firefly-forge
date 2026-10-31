# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Recorded v5 channels exercise control semantics, never live cluster acceptance."""

import importlib
import json
import ssl
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import pytest
from pydantic import SecretStr
from test_runner_kubernetes_control_host import configured, request

from firefly_forge.runner_kubernetes import KubernetesAuthority, KubernetesEffectUnknown
from firefly_forge.runner_kubernetes_models import KubernetesJobReceipt, KubernetesPodReceipt
from firefly_forge.runner_pool_models import pool_digest


class Socket:
    subprotocol = "v5.channel.k8s.io"

    def __init__(self, frames):
        self.frames = iter(frames)
        self.sent = []
        self.closed = False

    async def send(self, value):
        self.sent.append(value)

    async def recv(self):
        try:
            value = next(self.frames)
        except StopIteration:
            raise EOFError("Controlled channel ended") from None
        if isinstance(value, BaseException):
            raise value
        return value

    async def close(self):
        self.closed = True


def prepared(tmp_path, *, phase="command", payload=None):
    profile = configured()
    invocation = request(profile)
    if payload is not None:
        import hashlib

        from firefly_forge.runner_pool_models import KubernetesProjectInvocation

        raw = invocation.model_dump(mode="json")
        raw.update(
            project_id=uuid4().hex, archive_digest=hashlib.sha256(payload).hexdigest(), source_bytes=len(payload)
        )
        raw["bundle"].update(archive_digest=raw["archive_digest"], archive_bytes=len(payload))
        invocation = KubernetesProjectInvocation.model_validate(raw)
    authority = KubernetesAuthority(
        profile=profile,
        api_origin="https://kubernetes.fixture",
        tls=ssl.create_default_context(),
        token=SecretStr("public-channel-fixture"),
        state_directory=tmp_path,
    )
    common = dict(
        namespace=profile.kubernetes.namespace,
        execution_id=invocation.execution_id,
        invocation_digest=pool_digest(invocation.model_dump(mode="json")),
        profile_revision=profile.revision,
        spec_digest="a" * 64,
    )
    job = KubernetesJobReceipt(
        name="forge-" + invocation.execution_id + "-" + phase, phase=phase, uid=str(uuid4()), **common
    )
    pod = KubernetesPodReceipt(
        name="owned-pod", uid=str(uuid4()), job_uid=job.uid, node_name=profile.kubernetes.node_names[0], **common
    )
    observed = []

    async def observe(current, parent, bound, guard):
        await guard()
        assert current == invocation and parent == job and bound == pod
        observed.append(pod.uid)
        return {"metadata": {"uid": pod.uid}}

    return SimpleNamespace(authority=authority, observe_pod=observe), invocation, job, pod, observed


async def test_fixed_control_uses_v5_uid_gate_stdin_close_and_exact_success(tmp_path):
    module = importlib.import_module("firefly_forge.runner_kubernetes_exec")
    control, invocation, job, pod, observed = prepared(tmp_path)
    socket = Socket([b'\x01{"state":"parked"}', b'\x03{"status":"Success"}'])
    connected = []

    async def connector(authority, url):
        connected.append((authority, url))
        return socket

    async def guard():
        return None

    result = await module.KubernetesExecChannel(control, connector=connector).control(
        invocation,
        job,
        pod,
        "owner",
        b'{"operation":"status"}',
        guard,
    )
    assert result == b'{"state":"parked"}'
    assert socket.sent == [b'\x00{"operation":"status"}', b"\xff\x00"] and socket.closed
    assert len(connected) == 1 and observed == [pod.uid, pod.uid]
    url = urlsplit(connected[0][1])
    query = parse_qs(url.query)
    assert url.scheme == "wss" and url.hostname == "kubernetes.fixture"
    assert query["container"] == ["command"] and query["tty"] == ["false"]
    assert query["command"][:4] == ["/app/.venv/bin/python", "-I", "-S", "-c"]
    script = query["command"][-1]
    assert pod.uid in script and "FORGE_POD_UID" in script and "SO_PEERCRED" in script
    assert "public-channel-fixture" not in connected[0][1]


async def test_workspace_collection_streams_fixed_audited_program_without_control_buffer(tmp_path):
    module = importlib.import_module("firefly_forge.runner_kubernetes_exec")
    control, invocation, job, pod, observed = prepared(tmp_path)
    parts = [b"a" * 65536, b"b" * 17]
    socket = Socket([*(b"\x01" + part for part in parts), b'\x03{"status":"Success"}'])
    connected, received = [], []

    async def connector(authority, url):
        connected.append(url)
        return socket

    async def guard():
        return None

    async def sink(part):
        received.append(part)

    await module.KubernetesExecChannel(control, connector=connector).collect_workspace(
        invocation, job, pod, sink, guard
    )
    assert received == parts and socket.closed and observed == [pod.uid, pod.uid]
    script = parse_qs(urlsplit(connected[0]).query)["command"][-1]
    assert pod.uid in script and "pack_workspace(Path('/workspace')" in script
    assert "import firefly_forge" not in script and "sys.stdout.buffer.write(data)" in script


async def test_workspace_stream_stops_at_admitted_result_bound_and_closes_channel(tmp_path):
    module = importlib.import_module("firefly_forge.runner_kubernetes_exec")
    control, invocation, job, pod, _ = prepared(tmp_path)
    socket = Socket([b"\x01" + b"x" * (control.authority.profile.max_source_bytes + 1048577)])
    received = []

    async def connector(authority, url):
        return socket

    async def guard():
        return None

    async def sink(part):
        received.append(part)

    with pytest.raises(KubernetesEffectUnknown):
        await module.KubernetesExecChannel(control, connector=connector).collect_workspace(
            invocation, job, pod, sink, guard
        )
    assert received == [] and socket.closed


@pytest.mark.parametrize(
    "frames",
    [
        [b"\x01unconfirmed"],
        [b'\x03{"status":"Failure","message":"private-control-error"}'],
        [b'\x03{"status":true}'],
        [b"\x03not-json"],
        [b"\x05unknown-channel"],
        [b"\x01" + b"x" * 4097],
        [OSError("private-transport-error")],
    ],
)
async def test_uncertain_or_invalid_control_never_retries_or_returns_a_partial_result(tmp_path, frames):
    module = importlib.import_module("firefly_forge.runner_kubernetes_exec")
    control, invocation, job, pod, _ = prepared(tmp_path)
    socket = Socket(frames)
    attempts = []

    async def connector(authority, url):
        attempts.append(url)
        return socket

    async def guard():
        return None

    with pytest.raises(KubernetesEffectUnknown) as error:
        await module.KubernetesExecChannel(control, connector=connector).control(
            invocation,
            job,
            pod,
            "owner",
            b"{}",
            guard,
        )
    assert len(attempts) == 1 and socket.closed
    assert "private-control-error" not in str(error.value) and "private-transport-error" not in str(error.value)


async def test_wrong_negotiated_protocol_is_not_downgraded_or_sent_input(tmp_path):
    module = importlib.import_module("firefly_forge.runner_kubernetes_exec")
    control, invocation, job, pod, _ = prepared(tmp_path)
    socket = Socket([])
    socket.subprotocol = "v4.channel.k8s.io"

    async def connector(authority, url):
        return socket

    async def guard():
        return None

    with pytest.raises(KubernetesEffectUnknown):
        await module.KubernetesExecChannel(control, connector=connector).control(
            invocation, job, pod, "owner", b"{}", guard
        )
    assert socket.sent == [] and socket.closed


async def test_revoked_owner_or_unknown_program_refuses_before_connect(tmp_path):
    module = importlib.import_module("firefly_forge.runner_kubernetes_exec")
    control, invocation, job, pod, _ = prepared(tmp_path)
    attempts = []

    async def connector(authority, url):
        attempts.append(url)
        raise AssertionError("An unauthorised operation attempted a connection")

    async def guard():
        raise RuntimeError("Current lease revoked")

    service = module.KubernetesExecChannel(control, connector=connector)
    with pytest.raises(RuntimeError, match="lease revoked"):
        await service.control(invocation, job, pod, "owner", b"{}", guard)
    with pytest.raises(ValueError):
        await service.control(invocation, job, pod, "arbitrary-command", b"", guard)
    assert not attempts


async def test_lease_loss_after_a_sent_frame_closes_without_more_input(tmp_path):
    module = importlib.import_module("firefly_forge.runner_kubernetes_exec")
    control, invocation, job, pod, _ = prepared(tmp_path)
    alive = True

    class RevokingSocket(Socket):
        async def send(self, value):
            nonlocal alive
            await super().send(value)
            alive = False

    socket = RevokingSocket([])

    async def connector(authority, url):
        return socket

    async def guard():
        if not alive:
            raise RuntimeError("Current owner was replaced")

    with pytest.raises(KubernetesEffectUnknown):
        await module.KubernetesExecChannel(control, connector=connector).control(
            invocation, job, pod, "owner", b"{}", guard
        )
    assert socket.sent == [b"\x00{}"] and socket.closed


async def test_worker_cancellation_closes_its_owned_control_socket(tmp_path):
    import asyncio

    module = importlib.import_module("firefly_forge.runner_kubernetes_exec")
    control, invocation, job, pod, _ = prepared(tmp_path)
    socket = Socket([asyncio.CancelledError()])

    async def connector(authority, url):
        return socket

    async def guard():
        return None

    with pytest.raises(asyncio.CancelledError):
        await module.KubernetesExecChannel(control, connector=connector).control(
            invocation, job, pod, "owner", b"{}", guard
        )
    assert socket.closed


async def test_real_connector_pins_tls_protocol_origin_and_disables_redirects_and_proxy(tmp_path, monkeypatch):
    from websockets.asyncio import client

    module = importlib.import_module("firefly_forge.runner_kubernetes_exec")
    control, _, _, _, _ = prepared(tmp_path)
    calls = []
    socket = Socket([])

    class Connect:
        def __init__(self, url, **kwargs):
            calls.append((url, kwargs, self))

        def __await__(self):
            async def ready():
                return socket

            return ready().__await__()

    monkeypatch.setattr(client, "connect", Connect)
    url = "wss://kubernetes.fixture/api/v1/namespaces/project-workers/pods/owned-pod/exec?container=command"
    assert await module.connect_control(control.authority, url) is socket
    assert len(calls) == 1
    _, options, connection = calls[0]
    assert options["ssl"] is control.authority.tls and options["proxy"] is None and options["compression"] is None
    assert options["subprotocols"] == ["v5.channel.k8s.io"]
    assert options["additional_headers"] == {"Authorization": "Bearer public-channel-fixture"}
    assert isinstance(connection.process_redirect(RuntimeError("private redirect")), Exception)
    with pytest.raises(ValueError, match="origin"):
        await module.connect_control(control.authority, "wss://foreign.fixture/pods/exec")
    assert len(calls) == 1


async def test_object_transfer_streams_only_the_bound_reference_under_current_lease(tmp_path):
    import hashlib

    from firefly_forge.runner_object_artifacts import RunnerObjectReference

    module = importlib.import_module("firefly_forge.runner_kubernetes_exec")
    data = b"x" * 200000
    control, invocation, job, pod, _ = prepared(tmp_path, phase="transfer", payload=data)
    receipt = {"kind": "source", "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
    reference = RunnerObjectReference(
        execution_id=invocation.execution_id,
        invocation_digest=pool_digest(invocation.model_dump(mode="json")),
        receipt_sha256=pool_digest(receipt),
        **receipt,
    )
    socket = Socket([b"\x01" + json.dumps(receipt).encode(), b'\x03{"status":"Success"}'])
    calls = []

    async def source():
        for start in range(0, len(data), 10000):
            calls.append("read")
            yield data[start : start + 10000]

    async def connector(authority, url):
        assert parse_qs(urlsplit(url).query)["container"] == ["transfer"]
        assert "source.bundle" in url and "public-channel-fixture" not in url
        return socket

    async def guard():
        calls.append("guard")

    result = await module.KubernetesExecChannel(control, connector=connector).stage_object(
        invocation, job, pod, reference, source(), guard
    )
    assert result == reference and socket.closed
    stdin = b"".join(part[1:] for part in socket.sent if part[:1] == b"\x00")
    header, payload = stdin.split(b"\n", 1)
    assert json.loads(header) == receipt and payload == data and socket.sent[-1] == b"\xff\x00"
    assert calls.count("guard") > calls.count("read")


async def test_foreign_reference_refuses_before_consuming_data_or_opening_socket(tmp_path):
    import hashlib

    from firefly_forge.runner_object_artifacts import RunnerObjectReference

    module = importlib.import_module("firefly_forge.runner_kubernetes_exec")
    control, invocation, job, pod, _ = prepared(tmp_path, phase="transfer", payload=b"x")
    receipt = {"kind": "source", "bytes": 1, "sha256": hashlib.sha256(b"x").hexdigest()}
    ref = RunnerObjectReference(
        execution_id=uuid4().hex, invocation_digest="a" * 64, receipt_sha256=pool_digest(receipt), **receipt
    )
    effects = []

    async def source():
        effects.append("read")
        yield b"x"

    async def connector(authority, url):
        effects.append("connect")
        raise AssertionError("Foreign reference opened a channel")

    async def guard():
        return None

    with pytest.raises(ValueError):
        await module.KubernetesExecChannel(control, connector=connector).stage_object(
            invocation, job, pod, ref, source(), guard
        )
    assert effects == []


async def test_transfer_receipt_cannot_coerce_boolean_byte_count(tmp_path):
    import hashlib

    from firefly_forge.runner_object_artifacts import RunnerObjectReference

    module = importlib.import_module("firefly_forge.runner_kubernetes_exec")
    control, invocation, job, pod, _ = prepared(tmp_path, phase="transfer", payload=b"x")
    receipt = {"kind": "source", "bytes": 1, "sha256": hashlib.sha256(b"x").hexdigest()}
    ref = RunnerObjectReference(
        execution_id=invocation.execution_id,
        invocation_digest=pool_digest(invocation.model_dump(mode="json")),
        receipt_sha256=pool_digest(receipt),
        **receipt,
    )
    forged = dict(receipt, bytes=True)
    socket = Socket([b"\x01" + json.dumps(forged).encode(), b'\x03{"status":"Success"}'])

    async def connector(*args):
        return socket

    async def source():
        yield b"x"

    async def guard():
        return None

    with pytest.raises(KubernetesEffectUnknown):
        await module.KubernetesExecChannel(control, connector=connector).stage_object(
            invocation, job, pod, ref, source(), guard
        )
    assert socket.closed


def test_fixed_transfer_receiver_publishes_exact_bytes_without_overwriting(tmp_path, monkeypatch):
    import hashlib
    import io
    import os
    import stat
    import sys

    from firefly_forge.runner_object_artifacts import RunnerObjectReference

    module = importlib.import_module("firefly_forge.runner_kubernetes_exec")
    data = b"bounded fixture input"
    _, invocation, _, pod, _ = prepared(tmp_path, phase="transfer", payload=data)
    receipt = {"kind": "source", "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
    ref = RunnerObjectReference(
        execution_id=invocation.execution_id,
        invocation_digest=pool_digest(invocation.model_dump(mode="json")),
        receipt_sha256=pool_digest(receipt),
        **receipt,
    )
    code = compile(module.transfer_program(pod, ref), "<fixed-native-transfer>", "exec")
    original_open = os.open

    def opened(path, flags, *args, **kwargs):
        return original_open(tmp_path if path == "/forge-input" else path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", opened)
    monkeypatch.setenv("FORGE_POD_UID", pod.uid)
    output = io.StringIO()
    monkeypatch.setattr(sys, "stdout", output)
    body = json.dumps(receipt).encode() + b"\n" + data
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=io.BytesIO(body)))
    exec(code, {})
    target = tmp_path / "source.bundle"
    assert target.read_bytes() == data and stat.S_IMODE(target.stat().st_mode) == 0o600
    assert json.loads(output.getvalue()) == receipt and not (tmp_path / "source.bundle.incoming").exists()
    before = target.stat()
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=io.BytesIO(body)))
    with pytest.raises(FileExistsError):
        exec(code, {})
    assert target.read_bytes() == data and target.stat().st_ino == before.st_ino


def test_fixed_prepared_input_receiver_uses_native_confined_extractor_and_never_overwrites(tmp_path, monkeypatch):
    import hashlib
    import io
    import os
    import sys
    import tarfile

    module = importlib.import_module("firefly_forge.runner_kubernetes_exec")
    _, _, _, pod, _ = prepared(tmp_path, phase="transfer")
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w", format=tarfile.USTAR_FORMAT) as stream:
        for name, data in (
            ("source/main.py", b"print('fixture')\n"),
            ("git/config", b"[core]\n bare = false\n"),
            ("stdin.bin", b""),
            ("invocation.json", b"{}"),
        ):
            item = tarfile.TarInfo(name)
            item.mode, item.size = 0o644, len(data)
            stream.addfile(item, io.BytesIO(data))
    data = archive.getvalue()
    receipt = {"kind": "prepared_input", "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
    program = module.input_program(pod, receipt, 1048576)
    assert program.count("Path('/forge-input/payload')") == 1
    assert "def unpack_workspace(" in program and "os.O_EXCL | os.O_NOFOLLOW" in program
    program = program.replace("Path('/forge-input/payload')", "Path(" + repr(str(tmp_path / "payload")) + ")")
    original_open = os.open

    def opened(path, flags, *args, **kwargs):
        return original_open(tmp_path if path == "/tmp" else path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", opened)
    monkeypatch.setenv("FORGE_POD_UID", pod.uid)
    output = io.StringIO()
    monkeypatch.setattr(sys, "stdout", output)
    body = json.dumps(receipt).encode() + b"\n" + data
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=io.BytesIO(body)))
    exec(compile(program, "<fixed-input-carrier>", "exec"), {})
    target = tmp_path / "payload/source/main.py"
    assert target.read_bytes() == b"print('fixture')\n"
    assert (tmp_path / "forge-input-ready").is_file() and json.loads(output.getvalue()) == receipt
    before = target.stat()
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=io.BytesIO(body)))
    with pytest.raises(FileExistsError):
        exec(compile(program, "<fixed-input-carrier>", "exec"), {})
    assert target.stat().st_ino == before.st_ino and target.read_bytes() == b"print('fixture')\n"


async def test_transfer_source_mismatch_closes_stream_and_never_confirms_input(tmp_path):
    import hashlib

    from firefly_forge.runner_object_artifacts import RunnerObjectReference

    module = importlib.import_module("firefly_forge.runner_kubernetes_exec")
    control, invocation, job, pod, _ = prepared(tmp_path, phase="transfer", payload=b"admitted")
    receipt = {"kind": "source", "bytes": 8, "sha256": hashlib.sha256(b"admitted").hexdigest()}
    ref = RunnerObjectReference(
        execution_id=invocation.execution_id,
        invocation_digest=pool_digest(invocation.model_dump(mode="json")),
        receipt_sha256=pool_digest(receipt),
        **receipt,
    )
    socket = Socket([])
    closed = []

    async def connector(*args):
        return socket

    async def source():
        try:
            yield b"changed!"
        finally:
            closed.append(True)

    async def guard():
        return None

    with pytest.raises(KubernetesEffectUnknown):
        await module.KubernetesExecChannel(control, connector=connector).stage_object(
            invocation, job, pod, ref, source(), guard
        )
    assert closed == [True] and socket.closed and b"\xff\x00" not in socket.sent
