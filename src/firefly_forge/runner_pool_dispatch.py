# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Trusted local daemon transport. Projects cannot select its authority or Docker argv."""

from __future__ import annotations

import json
import os
import re
import stat
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pyfly.container import service

from firefly_forge.execution_environment import container_python_environment
from firefly_forge.runner_backends import _bounded_process
from firefly_forge.runner_pool_launcher import LAUNCHER, OWNER_READER, RESULT_READER, owned_launcher
from firefly_forge.runner_pool_models import RunnerInvocation, RunnerPoolProfile

Guard = Callable[[], Awaitable[None]]
Transport = Callable[[list[str], int, int, bytes | None], Awaitable[tuple[int, bytes]]]


@dataclass(frozen=True)
class RunnerDockerAuthority:
    profile: RunnerPoolProfile
    docker_binary: Path
    context: str
    config_directory: Path
    state_directory: Path
    namespace: str
    uid: int
    gid: int

    def __post_init__(self) -> None:
        if not self.docker_binary.is_absolute() or not self.docker_binary.is_file():
            raise ValueError("An explicit installed Docker executable is required")
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,99}", self.context):
            raise ValueError("An explicit Docker context is required")
        if not re.fullmatch(r"forge-runner-[a-z0-9-]{1,48}", self.namespace):
            raise ValueError("Runner namespace must be an owned resource prefix")
        if self.uid < 1 or self.gid < 1:
            raise ValueError("Project containers must run as a non-root account")
        for path in (self.config_directory, self.state_directory):
            info = path.stat(follow_symlinks=False)
            if not path.is_absolute() or not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
                raise ValueError("Controller directories must be real owner-controlled absolute paths")
            if stat.S_IMODE(info.st_mode) != 0o700:
                raise ValueError("Controller directories must be private (0700)")


@service
class DockerPoolDispatcher:
    def __init__(self, authority: RunnerDockerAuthority, *, transport: Transport | None = None) -> None:
        self.authority = authority
        self.transport = transport or self._transport

    async def _transport(
        self, argv: list[str], timeout: int, limit: int, input_data: bytes | None = None
    ) -> tuple[int, bytes]:
        admin = self.authority
        return await _bounded_process(
            [str(admin.docker_binary), "--config", str(admin.config_directory), "--context", admin.context, *argv],
            admin.state_directory,
            timeout,
            limit,
            input_data=input_data,
        )

    async def verify_daemon(self) -> None:
        code, output = await self.transport(["info", "--format", "{{.ID}}"], 10, 1024, None)
        if code or output.decode().strip() != self.authority.profile.daemon_id:
            raise RuntimeError("Runner daemon identity differs from the administrator profile")

    async def verify_images(self) -> None:
        await self.verify_daemon()
        for image in sorted(set((*self.authority.profile.approved_images, self.authority.profile.transfer_image))):
            code, data = await self.transport(["image", "inspect", image, "--format", "{{.Id}}"], 10, 1024, None)
            if code or not re.fullmatch(rb"sha256:[a-f0-9]{64}", data.strip()):
                raise RuntimeError("Approved runner images must already be cached on the pinned daemon")

    async def fenced_control(
        self, argv: list[str], guard: Guard, *, timeout: int = 30, limit: int = 500_000, input_data: bytes | None = None
    ) -> tuple[int, bytes]:
        await guard()
        await self.verify_daemon()
        # This check closes loss during identity validation. An in-flight daemon
        # request still has an unknown outcome on fence/transport loss; never retry it.
        await guard()
        return await self.transport(argv, timeout, limit, input_data)

    def name(self, invocation: RunnerInvocation) -> str:
        self.authority.profile.validate_invocation(invocation)
        return self.authority.namespace + "-" + invocation.execution_id

    def labels(self, invocation: RunnerInvocation) -> dict[str, str]:
        return {
            "org.firefly.forge.runner.namespace": self.authority.namespace,
            "org.firefly.forge.runner.profile": self.authority.profile.revision,
            "org.firefly.forge.runner.execution": invocation.execution_id,
        }

    def container_plan(self, invocation: RunnerInvocation, source_volume: str, git_volume: str) -> list[str]:
        self.authority.profile.validate_invocation(invocation)
        for value in (source_volume, git_volume):
            if not re.fullmatch(r"[a-f0-9]{64}", value):
                raise ValueError("Runner volume must be an observed anonymous daemon volume")
        admin = self.authority
        command = [
            "create",
            "--pull=never",
            "--name",
            self.name(invocation),
            "--read-only",
            "--network=none",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges:true",
            "--ipc=private",
            "--user=" + str(admin.uid) + ":" + str(admin.gid),
            "--cpus=" + str(invocation.cpus),
            "--memory=" + str(invocation.memory_mb) + "m",
            "--memory-swap=" + str(invocation.memory_mb) + "m",
            "--pids-limit=" + str(invocation.pids_limit),
            "--log-driver=local",
            "--log-opt=max-size=2m",
            "--log-opt=max-file=2",
            "--tmpfs=/tmp:rw,nosuid,nodev,size=134217728,mode=1777",
            "--env=HOME=/tmp/forge-home",
            "--env=XDG_CACHE_HOME=/tmp/forge-cache",
            "--env=GIT_CONFIG_NOSYSTEM=1",
            "--env=GIT_CONFIG_GLOBAL=/dev/null",
            "--env=GIT_ATTR_NOSYSTEM=1",
            "--mount",
            "type=volume,src=" + source_volume + ",dst=/forge-input,readonly,volume-nocopy",
            "--tmpfs=/workspace:rw,nosuid,nodev,exec,size="
            + str(admin.profile.max_source_bytes)
            + ",mode=0755,uid="
            + str(admin.uid)
            + ",gid="
            + str(admin.gid),
            "--mount",
            "type=volume,src=" + git_volume + ",dst=/workspace/.git,readonly,volume-nocopy",
            "--workdir",
            "/",
        ]
        for key, value in container_python_environment().items():
            command += ["--env=" + key + "=" + value]
        for key, value in self.labels(invocation).items():
            command += ["--label", key + "=" + value]
        launcher = LAUNCHER if invocation.work_duration is None else owned_launcher()
        return [*command, "--entrypoint", "/app/.venv/bin/python", invocation.image, "-I", "-S", "-c", launcher]

    async def inspect(self, invocation: RunnerInvocation, name_or_id: str) -> dict[str, Any] | None:
        allowed_name = self.name(invocation)
        if name_or_id not in {allowed_name, allowed_name + "-transfer"} and not re.fullmatch(
            r"[a-f0-9]{64}", name_or_id
        ):
            raise ValueError("Inspection only accepts an owned name or exact daemon resource ID")
        await self.verify_daemon()
        code, output = await self.transport(["container", "inspect", name_or_id], 10, 500_000, None)
        if code:
            if b"no such container" in output.lower() or b"no such object" in output.lower():
                return None
            raise RuntimeError("Runner inspection did not establish resource absence")
        value: dict[str, Any] = json.loads(output)[0]
        if any(
            value.get("Config", {}).get("Labels", {}).get(key) != expected
            for key, expected in self.labels(invocation).items()
        ):
            raise RuntimeError("Runner resource ownership differs from the immutable invocation")
        if not re.fullmatch(r"[a-f0-9]{64}", value.get("Id", "")):
            raise RuntimeError("Runner daemon did not return an exact resource identity")
        return value

    def transfer_plan(self, invocation: RunnerInvocation) -> list[str]:
        self.authority.profile.validate_invocation(invocation)
        admin = self.authority
        script = (
            "import os,signal\n"
            "signal.signal(signal.SIGALRM,lambda *args:os._exit(124))\n"
            "signal.alarm(30)\n"
            "for root in ('/workspace','/forge-git'):\n"
            f" os.chown(root,{admin.uid},{admin.gid},follow_symlinks=False)\n"
            " for base,dirs,files in os.walk(root,followlinks=False):\n"
            "  for name in dirs+files:\n"
            f"   os.chown(os.path.join(base,name),{admin.uid},{admin.gid},follow_symlinks=False)\n"
        )
        command = [
            "create",
            "--pull=never",
            "--name",
            self.name(invocation) + "-transfer",
            "--read-only",
            "--network=none",
            "--cap-drop=ALL",
            "--cap-add=CHOWN",
            "--user=0:0",
            "--security-opt=no-new-privileges:true",
            "--cpus=" + str(invocation.cpus),
            "--memory=" + str(invocation.memory_mb) + "m",
            "--memory-swap=" + str(invocation.memory_mb) + "m",
            "--pids-limit=" + str(invocation.pids_limit),
            "--ipc=private",
            "--mount",
            "type=volume,dst=/workspace,volume-nocopy",
            "--mount",
            "type=volume,dst=/forge-git,volume-nocopy",
        ]
        for key, value in self.labels(invocation).items():
            command += ["--label", key + "=" + value]
        return [
            *command,
            "--entrypoint",
            "/app/.venv/bin/python",
            admin.profile.transfer_image,
            "-I",
            "-S",
            "-c",
            script,
        ]

    def release_plan(self, invocation: RunnerInvocation, container_id: str) -> list[str]:
        self.authority.profile.validate_invocation(invocation)
        if invocation.work_duration is not None:
            raise ValueError("New work requires authenticated ownership release")
        if not re.fullmatch(r"[a-f0-9]{64}", container_id):
            raise ValueError("Release requires an exact observed container identity")
        script = (
            "import os,stat\n"
            "fd=os.open('/tmp/forge-start',os.O_WRONLY|os.O_NONBLOCK|os.O_NOFOLLOW)\n"
            "info=os.fstat(fd)\n"
            f"assert stat.S_ISFIFO(info.st_mode) and info.st_uid=={self.authority.uid}\n"
            "assert os.write(fd,b'1')==1\n"
            "os.close(fd)\n"
        )
        return ["exec", container_id, "/app/.venv/bin/python", "-I", "-S", "-c", script]

    def result_plan(self, invocation: RunnerInvocation, container_id: str) -> list[str]:
        self.authority.profile.validate_invocation(invocation)
        if invocation.work_duration is not None:
            raise ValueError("New work requires authenticated ownership status")
        if not re.fullmatch(r"[a-f0-9]{64}", container_id):
            raise ValueError("Result read requires an exact observed container identity")
        return ["exec", container_id, "/app/.venv/bin/python", "-I", "-S", "-c", RESULT_READER]

    def owner_plan(self, invocation: RunnerInvocation, container_id: str) -> list[str]:
        self.authority.profile.validate_invocation(invocation)
        if invocation.work_duration is None or not re.fullmatch(r"[a-f0-9]{64}", container_id):
            raise ValueError("Ownership read requires a new-work policy and exact observed container identity")
        return ["exec", "--interactive", container_id, "/app/.venv/bin/python", "-I", "-S", "-c", OWNER_READER]

    def collect_plan(self, invocation: RunnerInvocation, container_id: str) -> list[str]:
        from firefly_forge.runner_pool_launcher import workspace_collector

        self.authority.profile.validate_invocation(invocation)
        if not re.fullmatch(r"[a-f0-9]{64}", container_id):
            raise ValueError("Collection requires an exact observed container identity")
        return [
            "exec",
            container_id,
            "/app/.venv/bin/python",
            "-I",
            "-S",
            "-c",
            workspace_collector(self.authority.profile.max_source_bytes),
        ]
