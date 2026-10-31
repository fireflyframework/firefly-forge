# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""One-shot Kubernetes v5 control channels; project argv never becomes an exec request."""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterable, Awaitable, Callable
from contextlib import suppress
from typing import Protocol, cast
from urllib.parse import urlencode, urlsplit

from pyfly.container import service

from firefly_forge.runner_kubernetes import (
    Guard,
    KubernetesAuthority,
    KubernetesControlClient,
    KubernetesEffectUnknown,
)
from firefly_forge.runner_kubernetes_models import KubernetesJobReceipt, KubernetesPodReceipt
from firefly_forge.runner_object_artifacts import RunnerObjectReference
from firefly_forge.runner_pool_launcher import OWNER_READER
from firefly_forge.runner_pool_models import KubernetesProjectInvocation, pool_digest

PROTOCOL = "v5.channel.k8s.io"
CONTROL_LIMIT = 4096


class ExecSocket(Protocol):
    @property
    def subprotocol(self) -> str | None: ...

    async def send(self, value: bytes) -> None: ...
    async def recv(self) -> str | bytes: ...
    async def close(self) -> None: ...


Connector = Callable[[KubernetesAuthority, str], Awaitable[ExecSocket]]


async def connect_control(authority: KubernetesAuthority, url: str) -> ExecSocket:
    from websockets.asyncio.client import connect
    from websockets.typing import Subprotocol

    class PinnedConnect(connect):
        def process_redirect(self, exc: Exception) -> Exception | str:
            return RuntimeError("The configured Kubernetes origin cannot redirect control requests")

    authority.verify()
    expected = urlsplit(authority.api_origin.replace("https://", "wss://", 1))
    observed = urlsplit(url)
    if (observed.scheme, observed.netloc) != (expected.scheme, expected.netloc) or observed.fragment:
        raise ValueError("Kubernetes streaming must retain the configured origin")
    return cast(
        ExecSocket,
        await PinnedConnect(
            url,
            ssl=authority.tls,
            additional_headers={"Authorization": "Bearer " + authority.token.get_secret_value()},
            subprotocols=[Subprotocol(PROTOCOL)],
            proxy=None,
            compression=None,
            open_timeout=15,
            close_timeout=5,
            max_size=1048576,
            max_queue=1,
            write_limit=65536,
        ),
    )


def control_program(pod: KubernetesPodReceipt, operation: str) -> str:
    programs = {
        "owner": OWNER_READER,
        "ready": "import stat\nassert stat.S_ISSOCK(os.lstat('/tmp/forge-owner.sock').st_mode)\n",
    }
    if operation not in programs:
        raise ValueError("Only the fixed native Kubernetes control programs are available")
    return "import os\n" + "assert os.environ.get('FORGE_POD_UID') == " + repr(pod.uid) + "\n" + programs[operation]


def transfer_program(pod: KubernetesPodReceipt, reference: RunnerObjectReference) -> str:
    names = {"source": "source.bundle", "stdin": "stdin.bin"}
    if reference.kind not in names:
        raise ValueError("Only invocation inputs can enter the transfer volume")
    return (
        "import os,sys,json,hashlib\n"
        + "assert os.environ.get('FORGE_POD_UID') == "
        + repr(pod.uid)
        + "\n"
        + "expected="
        + repr(reference.receipt())
        + "\n"
        + "target="
        + repr(names[reference.kind])
        + "\n"
        + "header=sys.stdin.buffer.readline(4097)\n"
        "assert len(header)<=4096 and header.endswith(b'\\n') and json.loads(header)==expected\n"
        "parent=os.open('/forge-input',os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)\n"
        "try:\n"
        " fd=os.open(target+'.incoming',os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=parent)\n"
        " with os.fdopen(fd,'wb') as output:\n"
        "  size=0; checksum=hashlib.sha256()\n"
        "  while True:\n"
        "   part=sys.stdin.buffer.read(65536)\n"
        "   if not part: break\n"
        "   size+=len(part); assert size<=expected['bytes']\n"
        "   checksum.update(part); output.write(part)\n"
        "  assert size==expected['bytes'] and checksum.hexdigest()==expected['sha256']\n"
        "  output.flush(); os.fsync(output.fileno())\n"
        " os.link(target+'.incoming',target,src_dir_fd=parent,dst_dir_fd=parent,follow_symlinks=False)\n"
        " os.fsync(parent)\n"
        " os.unlink(target+'.incoming',dir_fd=parent); os.fsync(parent)\n"
        "finally: os.close(parent)\n"
        "print(json.dumps(expected,separators=(',',':')),flush=True)\n"
    )


@service
class KubernetesExecChannel:
    def __init__(self, control: KubernetesControlClient, *, connector: Connector | None = None) -> None:
        self.control_client = control
        self.connector = connector or connect_control

    async def control(
        self,
        invocation: KubernetesProjectInvocation,
        job: KubernetesJobReceipt,
        pod: KubernetesPodReceipt,
        operation: str,
        body: bytes,
        guard: Guard,
    ) -> bytes:
        program = control_program(pod, operation)
        if type(body) is not bytes or len(body) > CONTROL_LIMIT or operation == "ready" and body:
            raise ValueError("Native Kubernetes control requires its bounded exact input")

        async def chunks() -> AsyncIterable[bytes]:
            if body:
                yield body

        return await self._exchange(invocation, job, pod, "command", program, chunks(), guard)

    async def stage_object(
        self,
        invocation: KubernetesProjectInvocation,
        job: KubernetesJobReceipt,
        pod: KubernetesPodReceipt,
        reference: RunnerObjectReference,
        source: AsyncIterable[bytes],
        guard: Guard,
    ) -> RunnerObjectReference:
        receipt = reference.receipt()
        expected = {
            "source": (invocation.source_bytes, invocation.archive_digest),
            "stdin": (invocation.stdin_bytes, invocation.stdin_digest or hashlib.sha256(b"").hexdigest()),
        }
        if (
            job.phase != "transfer"
            or invocation.project_id is None
            or reference.execution_id != invocation.execution_id
            or reference.invocation_digest != pool_digest(invocation.model_dump(mode="json"))
            or reference.kind not in expected
            or (reference.bytes, reference.sha256) != expected[reference.kind]
        ):
            raise ValueError("Object transfer requires its exact invocation reference and separate phase")
        self.control_client.authority.profile.validate_invocation(invocation)

        async def chunks() -> AsyncIterable[bytes]:
            yield json.dumps(receipt, separators=(",", ":")).encode() + b"\n"
            size = 0
            checksum = hashlib.sha256()
            async for part in source:
                await guard()
                if type(part) is not bytes:
                    raise ValueError("Object transfer requires exact bytes")
                size += len(part)
                if size > reference.bytes:
                    raise ValueError("Object transfer exceeds its admitted size")
                checksum.update(part)
                for start in range(0, len(part), 65536):
                    yield part[start : start + 65536]
            if size != reference.bytes or checksum.hexdigest() != reference.sha256:
                raise ValueError("Object transfer differs from its admitted content")

        try:
            output = await self._exchange(
                invocation, job, pod, "transfer", transfer_program(pod, reference), chunks(), guard
            )
            observed = json.loads(output)
            if type(observed) is not dict or pool_digest(observed) != reference.receipt_sha256:
                raise KubernetesEffectUnknown("The transfer receipt is not the admitted object")
            return reference
        except (ValueError, TypeError):
            raise KubernetesEffectUnknown(
                "Object transfer outcome is unconfirmed; preserve its native intent"
            ) from None
        finally:
            close = getattr(source, "aclose", None)
            if close is not None:
                await close()

    async def _exchange(
        self,
        invocation: KubernetesProjectInvocation,
        job: KubernetesJobReceipt,
        pod: KubernetesPodReceipt,
        container: str,
        program: str,
        body: AsyncIterable[bytes],
        guard: Guard,
    ) -> bytes:
        if job.phase != container:
            raise ValueError("The native stream cannot cross Kubernetes execution phases")
        await self.control_client.observe_pod(invocation, job, pod, guard)
        authority = self.control_client.authority
        authority.verify()
        await guard()
        query = urlencode(
            [
                ("container", container),
                *[("command", arg) for arg in ("/app/.venv/bin/python", "-I", "-S", "-c", program)],
                ("stdin", "true"),
                ("stdout", "true"),
                ("stderr", "true"),
                ("tty", "false"),
            ]
        )
        url = authority.api_origin.replace("https://", "wss://", 1)
        url += "/api/v1/namespaces/" + pod.namespace + "/pods/" + pod.name + "/exec?" + query
        connection: ExecSocket | None = None
        try:
            connection = await self.connector(authority, url)
            if connection.subprotocol != PROTOCOL:
                raise KubernetesEffectUnknown("The Kubernetes control protocol was not confirmed")
            await guard()
            async for part in body:
                await guard()
                await connection.send(b"\x00" + part)
                await guard()
            await connection.send(b"\xff\x00")
            output = bytearray()
            while True:
                await guard()
                frame = await connection.recv()
                await guard()
                if type(frame) is not bytes or not frame:
                    raise KubernetesEffectUnknown("The Kubernetes control stream is unconfirmed")
                channel, data = frame[0], frame[1:]
                if channel == 1:
                    output.extend(data)
                    if len(output) > CONTROL_LIMIT:
                        raise KubernetesEffectUnknown("The native control response exceeds its bound")
                elif channel == 3:
                    if len(data) > CONTROL_LIMIT:
                        raise KubernetesEffectUnknown("The native control status exceeds its bound")
                    status = json.loads(data)
                    if type(status) is not dict or status.get("status") != "Success":
                        raise KubernetesEffectUnknown("The native Kubernetes control result is unconfirmed")
                    await self.control_client.observe_pod(invocation, job, pod, guard)
                    authority.verify()
                    return bytes(output)
                else:
                    raise KubernetesEffectUnknown("The native Kubernetes control channel is unconfirmed")
        except Exception:
            raise KubernetesEffectUnknown("Kubernetes control outcome is unknown; preserve its native intent") from None
        finally:
            close = getattr(body, "aclose", None)
            if close is not None:
                with suppress(Exception):
                    await close()
            if connection is not None:
                with suppress(Exception):
                    await connection.close()
