# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Own one Linux CI PostgreSQL container with runner-visible native control files."""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import shutil
import socket
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

LABEL = "org.firefly.forge.ci-postgres"
IMAGE = "postgres:17"
SQL = """SELECT json_build_object(
'data_directory', current_setting('data_directory'), 'database', current_database(),
'port', inet_server_port(), 'address', host(inet_server_addr()),
'started', floor(extract(epoch FROM pg_postmaster_start_time()))::bigint,
'system_identifier', (pg_control_system()).system_identifier::text)"""


def docker(
    args: list[str], *, required: bool = True, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=30, env=env)
    if required and result.returncode:
        raise RuntimeError("The owned CI Docker control operation did not confirm")
    return result


def container_command(state: dict[str, Any]) -> list[str]:
    uid, gid, data = state["uid"], state["gid"], state["data"]
    # Initialization psql clears PGHOST, so the image's default socket path must be writable by this UID.
    return [
        "create",
        "--name",
        state["name"],
        "--label",
        LABEL + "=" + state["nonce"],
        "--user",
        f"{uid}:{gid}",
        "--network",
        "host",
        "--mount",
        f"type=bind,src={data},dst={data}",
        "--tmpfs",
        f"/var/run/postgresql:rw,noexec,nosuid,nodev,uid={uid},gid={gid},mode=0700",
        "--env",
        "POSTGRES_PASSWORD",
        "--env",
        "POSTGRES_USER=forge_test",
        "--env",
        "POSTGRES_DB=forge_test",
        "--env",
        "POSTGRES_HOST_AUTH_METHOD=scram-sha-256",
        "--env",
        "PGDATA=" + data,
        "--env",
        "PGPORT=" + str(state["port"]),
        state["image_id"],
        "postgres",
        "-c",
        "listen_addresses=127.0.0.1",
        "-c",
        "unix_socket_permissions=0700",
        "-p",
        str(state["port"]),
    ]


def validate_container(raw: dict[str, Any], state: dict[str, Any]) -> str:
    cid = raw.get("Id", "")
    binds = [value for value in raw.get("Mounts", []) if value.get("Type") == "bind"]
    foreign_mounts = [
        value
        for value in raw.get("Mounts", [])
        if value.get("Type") != "bind"
        and (value.get("Type"), value.get("Destination"))
        not in {
            ("tmpfs", "/var/run/postgresql"),
            ("volume", "/var/lib/postgresql/data"),
        }
    ]
    if (
        not isinstance(cid, str)
        or not re.fullmatch(r"[a-f0-9]{64}", cid)
        or (state.get("container_id") is not None and cid != state["container_id"])
        or raw.get("Name") != "/" + state["name"]
        or raw.get("Image") != state["image_id"]
        or raw.get("Config", {}).get("User") != f"{state['uid']}:{state['gid']}"
        or raw.get("Config", {}).get("Labels", {}).get(LABEL) != state["nonce"]
        or raw.get("HostConfig", {}).get("NetworkMode") != "host"
        or raw.get("HostConfig", {}).get("Privileged", False) is not False
        or len(binds) != 1
        or binds[0].get("Source") != state["data"]
        or binds[0].get("Destination") != state["data"]
        or binds[0].get("RW") is not True
        or foreign_mounts
    ):
        raise ValueError("CI PostgreSQL container identity, owner or native data mount changed")
    return cid


def owned_file(path: Path, limit: int) -> bytes:
    before = path.lstat()
    if (
        path.resolve() != path
        or not stat.S_ISREG(before.st_mode)
        or before.st_uid != os.getuid()
        or before.st_nlink != 1
        or before.st_mode & 0o077
        or before.st_size > limit
        or getattr(before, "st_flags", 0) & 0x40000000
    ):
        raise ValueError("CI PostgreSQL evidence must be owned ordinary private local data")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as source:
        opened = os.fstat(source.fileno())
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise ValueError("CI PostgreSQL evidence changed before opening")
        value = source.read(limit + 1)
    if len(value) > limit or path.lstat() != before:
        raise ValueError("CI PostgreSQL evidence changed while reading")
    return value


def validate_native_files(state: dict[str, Any], sql: dict[str, Any]) -> dict[str, Any]:
    data = Path(state["data"])
    folder = data.lstat()
    if (
        data.resolve() != data
        or not stat.S_ISDIR(folder.st_mode)
        or folder.st_uid != state["uid"]
        or stat.S_IMODE(folder.st_mode) != 0o700
    ):
        raise ValueError("CI PostgreSQL requires its exact private runner-owned data directory")
    if owned_file(data / "PG_VERSION", 32) != b"17\n":
        raise ValueError("CI PostgreSQL must supply its actual version17 control files")
    control = owned_file(data / "global/pg_control", 8192)
    lines = owned_file(data / "postmaster.pid", 8192).decode().splitlines()
    if len(control) != 8192 or len(lines) < 4:
        raise ValueError("CI PostgreSQL native control evidence is incomplete")
    if (
        int(lines[0]) <= 0
        or lines[1] != str(data)
        or int(lines[3]) != state["port"]
        or sql.get("data_directory") != str(data)
        or sql.get("database") != "forge_test"
        or type(sql.get("port")) is not int
        or sql["port"] != state["port"]
        or type(sql.get("started")) is not int
        or sql["started"] != int(lines[2])
        or not ipaddress.ip_address(sql.get("address", "")).is_loopback
        or sql.get("system_identifier") != str(int.from_bytes(control[:8], sys.byteorder))
    ):
        raise ValueError("CI PostgreSQL SQL identity differs from the actual runner-visible native cluster")
    return {**sql, "postmaster_pid_in_container": int(lines[0]), "host_pid_signal_used": False}


def private_write(path: Path, value: dict[str, Any]) -> None:
    if path.exists() or path.is_symlink():
        raise ValueError("CI ownership evidence cannot overwrite an existing receipt")
    temporary = path.with_name("." + path.name + "-" + uuid4().hex)
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as output:
        json.dump(value, output, sort_keys=True, indent=2)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    temporary.rename(path)


def validate_state(root: Path, state: dict[str, Any]) -> None:
    if (
        not re.fullmatch(r"[a-f0-9]{32}", state.get("nonce", ""))
        or state.get("name") != "forge-python-postgres-" + state["nonce"]
        or type(state.get("uid")) is not int
        or state["uid"] != os.getuid()
        or type(state.get("gid")) is not int
        or state["gid"] != os.getgid()
        or type(state.get("port")) is not int
        or state["port"] != 5432
        or state.get("data") != str(root / "data")
        or not re.fullmatch(r"sha256:[a-f0-9]{64}", state.get("image_id", ""))
        or (state.get("container_id") is not None and not re.fullmatch(r"[a-f0-9]{64}", state["container_id"]))
    ):
        raise ValueError("CI PostgreSQL cleanup requires its exact private original reservation")


def fixture_root() -> Path:
    if sys.platform != "linux" or os.environ.get("GITHUB_ACTIONS") != "true" or os.getuid() == 0:
        raise ValueError("The owned PostgreSQL fixture requires a non-root Linux GitHub runner")
    if any(os.environ.get(key) for key in ("DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH")):
        raise ValueError("The CI fixture cannot use an alternate Docker host or context")
    base = Path(os.environ["RUNNER_TEMP"]).resolve(strict=True)
    info = base.lstat()
    if info.st_uid != os.getuid() or info.st_mode & 0o022:
        raise ValueError("The CI fixture requires its owned runner temporary directory")
    run, attempt = os.environ["GITHUB_RUN_ID"], os.environ["GITHUB_RUN_ATTEMPT"]
    if not run.isdigit() or not attempt.isdigit() or any(c in str(base) for c in "\r\n\x00,"):
        raise ValueError("The CI fixture requires exact safe runner identifiers and paths")
    return base / ("forge-python-postgres-" + run + "-" + attempt)


def inspect_container(identifier: str) -> dict[str, Any] | None:
    result = docker(["inspect", identifier], required=False)
    if result.returncode:
        return None
    values = json.loads(result.stdout)
    if not isinstance(values, list) or len(values) != 1:
        raise ValueError("CI PostgreSQL container inspection is ambiguous")
    value = values[0]
    if not isinstance(value, dict):
        raise ValueError("CI PostgreSQL container inspection is malformed")
    return value


def start(root: Path) -> None:
    password = os.environ.get("POSTGRES_PASSWORD", "")
    if not password:
        raise ValueError("The CI PostgreSQL password must be explicitly supplied")
    for key in ("FORGE_TEST_DATABASE_URL", "FORGE_DATABASE_URL"):
        url = urlsplit(os.environ.get(key, ""))
        if (
            url.scheme != "postgresql+asyncpg"
            or url.hostname != "127.0.0.1"
            or url.port != 5432
            or url.username != "forge_test"
            or url.password != password
            or url.path != "/forge_test"
            or url.query
            or url.fragment
        ):
            raise ValueError("The CI fixture requires its exact dedicated local test database")
    if root.exists() or root.is_symlink():
        raise ValueError("The CI PostgreSQL fixture must be fresh; retained state cannot be adopted")
    with socket.socket() as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind(("127.0.0.1", 5432))
    free = shutil.disk_usage(root.parent).free
    if free < 1073741824:
        raise ValueError("CI PostgreSQL preparation reached the existing one-GiB source-processing free-space floor")
    root.mkdir(mode=0o700)
    data = root / "data"
    data.mkdir(mode=0o700)
    image = docker(["image", "inspect", IMAGE, "--format", "{{.Id}}"]).stdout.strip()
    if not re.fullmatch(r"sha256:[a-f0-9]{64}", image):
        raise ValueError("The existing PostgreSQL17 image did not resolve to an exact local image identity")
    nonce = uuid4().hex
    state = {
        "name": "forge-python-postgres-" + nonce,
        "nonce": nonce,
        "uid": os.getuid(),
        "gid": os.getgid(),
        "data": str(data),
        "port": 5432,
        "image_id": image,
        "free_bytes_before": free,
    }
    private_write(root / "reservation.json", state)
    cid = docker(container_command(state)).stdout.strip()
    if not re.fullmatch(r"[a-f0-9]{64}", cid):
        raise ValueError("CI PostgreSQL creation outcome is unknown; cleanup must inspect its original reservation")
    state["container_id"] = cid
    private_write(root / "state.json", state)
    raw = inspect_container(cid)
    if raw is None:
        raise ValueError("The exact created CI PostgreSQL container is unavailable")
    validate_container(raw, state)
    docker(["start", cid])
    deadline = time.monotonic() + 60
    while True:
        raw = inspect_container(cid)
        if raw is None or not raw.get("State", {}).get("Running"):
            raise ValueError("CI PostgreSQL exited before readiness; inspect its retained logs")
        validate_container(raw, state)
        result = docker(
            [
                "exec",
                "--env",
                "PGPASSWORD",
                cid,
                "psql",
                "-XAt",
                "-h",
                "127.0.0.1",
                "-p",
                "5432",
                "-U",
                "forge_test",
                "-d",
                "forge_test",
                "-v",
                "ON_ERROR_STOP=1",
                "-c",
                SQL,
            ],
            required=False,
            env={**os.environ, "PGPASSWORD": password},
        )
        if result.returncode == 0:
            proof = validate_native_files(state, json.loads(result.stdout))
            private_write(root / "ready.json", proof)
            with Path(os.environ["GITHUB_ENV"]).open("a") as output:
                output.write("FORGE_TEST_POSTGRES_DATA=" + str(data) + "\n")
            print("Owned PostgreSQL17 ready: real runner-visible control files; no host PID signaling")
            return
        if time.monotonic() >= deadline:
            raise TimeoutError("CI PostgreSQL readiness control window expired; test workload was not started")
        time.sleep(0.5)


def stop(root: Path) -> None:
    state_file = root / "state.json" if (root / "state.json").exists() else root / "reservation.json"
    if not state_file.exists():
        print("No owned CI PostgreSQL reservation was created")
        return
    state = json.loads(owned_file(state_file, 16384))
    validate_state(root, state)
    raw = inspect_container(state.get("container_id") or state["name"])
    if raw is None:
        raise ValueError("CI PostgreSQL cleanup cannot confirm its exact reserved container")
    cid = validate_container(raw, state)
    if raw.get("State", {}).get("Running") is not False:
        docker(["stop", "--time", "15", cid])
    after = inspect_container(cid)
    if after is None or validate_container(after, state) != cid or after.get("State", {}).get("Running") is not False:
        raise ValueError("Owned CI PostgreSQL shutdown did not confirm")
    ended = after["State"]
    clean_exit = (
        ended.get("OOMKilled") is False
        and type(ended.get("ExitCode")) is int
        and ended["ExitCode"] == 0
        and ended.get("Error") == ""
    )
    diagnostics_error: BaseException | None = None
    try:
        logs = docker(["logs", cid])
        log = root / "postgres.log"
        fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as output:
            output.write(logs.stdout + logs.stderr)
    except BaseException as error:
        diagnostics_error = error
    docker(["rm", "--volumes", cid])
    if inspect_container(cid) is not None or (Path(state["data"]) / "postmaster.pid").exists():
        raise ValueError("Owned CI PostgreSQL cleanup left a container or active native PID file")
    with socket.socket() as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind(("127.0.0.1", state["port"]))
    private_write(
        root / "cleanup.json",
        {
            "container_removed": True,
            "postmaster_absent": True,
            "port_free": True,
            "data_retained": True,
            "host_pid_signal_used": False,
            "clean_exit": clean_exit,
            "diagnostics_unavailable": diagnostics_error is not None,
            "diagnostics_error_type": type(diagnostics_error).__name__ if diagnostics_error is not None else None,
        },
    )
    if diagnostics_error is not None:
        if not isinstance(diagnostics_error, Exception):
            raise diagnostics_error
        raise ValueError("Owned CI PostgreSQL drained but diagnostics are unavailable; inspect its receipts") from None
    if not clean_exit:
        raise ValueError("Owned CI PostgreSQL was removed after an unclean exit; inspect its retained logs")
    print("Owned CI PostgreSQL drained; only its exact container and anonymous image volumes were removed")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("start", "stop"))
    args = parser.parse_args()
    root = fixture_root()
    if args.action == "start":
        start(root)
    else:
        stop(root)


if __name__ == "__main__":
    main()
