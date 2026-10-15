# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Boot the image's default command and check its own init, readiness and shutdown."""

import argparse
import json
import os
import re
import secrets
import subprocess
import time
import uuid
from pathlib import Path
from urllib.parse import unquote, urlsplit


def require_test_database(url: str) -> None:
    parsed = urlsplit(url)
    database = unquote(parsed.path.removeprefix("/"))
    assert (
        parsed.scheme in {"postgresql", "postgresql+asyncpg"}
        and parsed.hostname
        and re.fullmatch(r"[A-Za-z0-9_]+_test", database)
        and not parsed.query
        and not parsed.fragment
    ), "Use an explicit dedicated PostgreSQL database ending in _test, without query overrides"
    _ = parsed.port


def require_ready_health(payload: object) -> None:
    assert isinstance(payload, dict), "Health must return its readiness contract"
    execution = payload.get("execution")
    assert (
        payload.get("status") == "ok"
        and payload.get("detail") in (None, "")
        and payload.get("service_role") == "combined"
        and payload.get("admission_ready") is True
        and payload.get("execution_ready") is True
        and isinstance(execution, dict)
        and execution.get("state") == "ready"
        and execution.get("local_enabled") is True
    ), "Default command must be healthy and ready for combined admission and execution"


def require_graceful_shutdown(state: dict[str, object], logs: str) -> None:
    assert (
        state.get("Running") is False
        and type(state.get("ExitCode")) is int
        and state["ExitCode"] in {0, 143}
        and state.get("OOMKilled") is False
        and state.get("Error") in (None, "")
    ), "Container must exit without an OOM, runtime error or forced kill"
    completed = logs.find("Application shutdown complete.")
    finished = logs.find("Finished server process", completed)
    assert completed >= 0 and finished > completed, "Require completed application shutdown before process exit"


def require_owned_schema(schema: str) -> None:
    assert re.fullmatch(r"forge_default_smoke_[a-f0-9]{32}", schema), "Refuse cleanup outside this smoke's UUID schema"


def cleanup_program(schema: str) -> str:
    require_owned_schema(schema)
    return f"""import asyncio,os,re
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
async def clean():
    schema = {schema!r}
    assert re.fullmatch(r"forge_default_smoke_[a-f0-9]{{32}}", schema)
    url = make_url(os.environ["FORGE_DATABASE_URL"])
    assert url.get_backend_name() == "postgresql" and re.fullmatch(r"[A-Za-z0-9_]+_test", url.database or "")
    assert not url.query
    engine = create_async_engine(url.set(drivername="postgresql+asyncpg"))
    try:
        async with engine.begin() as connection:
            await connection.execute(text('DROP SCHEMA IF EXISTS "' + schema + '" CASCADE'))
            remaining = await connection.scalar(text("SELECT to_regnamespace(:name)"), {{"name": schema}})
            assert remaining is None
    finally:
        await engine.dispose()
asyncio.run(clean())
"""


def cleanup_owned_resources(
    docker: list[str],
    image: str,
    name: str,
    schema: str,
    environment: dict[str, str],
    original_error: BaseException | None,
) -> None:
    try:
        require_owned_schema(schema)
        assert re.fullmatch(r"forge-default-command-smoke-[a-f0-9]{32}", name)
        inspected = subprocess.run(docker + ["inspect", name], capture_output=True, timeout=10)
        if inspected.returncode != 0:
            raise RuntimeError("Container absence is unproven; retain its schema for reconciliation")
        subprocess.run(docker + ["rm", "--force", name], check=True, capture_output=True, timeout=30)
        subprocess.run(
            docker
            + [
                "run",
                "--rm",
                "--network",
                "host",
                "--env",
                "FORGE_DATABASE_URL",
                image,
                "/app/.venv/bin/python",
                "-c",
                cleanup_program(schema),
            ],
            env=environment,
            check=True,
            capture_output=True,
            timeout=30,
        )
    except BaseException as cleanup_error:
        note = f"Cleanup incomplete; retain container {name} and schema {schema} for reconciliation"
        if original_error is not None:
            original_error.add_note(note)
            raise original_error from cleanup_error
        cleanup_error.add_note(note)
        raise
    if original_error is not None:
        raise original_error


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image")
    args = parser.parse_args()
    require_test_database(os.environ.get("FORGE_DATABASE_URL", ""))
    docker = ["docker"]
    environment = {**os.environ, "FORGE_API_TOKEN": secrets.token_urlsafe(32)}
    name = "forge-default-command-smoke-" + uuid.uuid4().hex
    schema = "forge_default_smoke_" + uuid.uuid4().hex
    require_owned_schema(schema)
    probe = Path(__file__).with_name("container_smoke.py").resolve()
    metadata = json.loads(subprocess.check_output(docker + ["image", "inspect", args.image], text=True))[0]["Config"]
    assert metadata["Cmd"] == ["/app/.venv/bin/forge", "serve", "--host", "0.0.0.0"]
    assert metadata["User"] == "forge"
    original_error: BaseException | None = None
    try:
        subprocess.run(
            docker
            + [
                "run",
                "--detach",
                "--name",
                name,
                "--network",
                "host",
                "--env",
                "FORGE_DATABASE_URL",
                "--env",
                "FORGE_API_TOKEN",
                "--env",
                "FORGE_AUTH_MODE=local_bootstrap",
                "--env",
                "FORGE_DATABASE_SCHEMA=" + schema,
                "--volume",
                f"{probe}:/tmp/container_smoke.py:ro",
                args.image,
            ],
            env=environment,
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        # Neither an entrypoint override nor Docker --init can supply the behavior being tested.
        readiness = (
            "import os,json,urllib.request; "
            "request=urllib.request.Request('http://127.0.0.1:8787/api/v1/health', "
            "headers={'Authorization':'Bearer '+os.environ['FORGE_API_TOKEN']}); "
            "response=urllib.request.urlopen(request,timeout=2); "
            "assert response.status==200; print(response.read().decode())"
        )
        deadline = time.monotonic() + 60
        while True:
            status = subprocess.run(
                docker + ["exec", name, "/app/.venv/bin/python", "-c", readiness],
                capture_output=True,
                timeout=5,
            )
            if status.returncode == 0:
                try:
                    require_ready_health(json.loads(status.stdout))
                except (AssertionError, ValueError):
                    pass
                else:
                    break
            running = subprocess.check_output(
                docker + ["inspect", "--format", "{{.State.Running}}", name], text=True, timeout=10
            )
            assert running.strip() == "true", "Default command exited before authenticated readiness"
            assert time.monotonic() < deadline, "Default command did not become ready"
            time.sleep(0.2)
        subprocess.run(
            docker + ["exec", name, "/app/.venv/bin/python", "/tmp/container_smoke.py", "--supervision-only"],
            check=True,
            timeout=20,
        )
        assert metadata["Entrypoint"] == ["/usr/bin/tini", "--"]
        subprocess.run(docker + ["stop", "--time", "20", name], check=True, capture_output=True, timeout=30)
        state = json.loads(subprocess.check_output(docker + ["inspect", name], text=True))[0]["State"]
        logs = subprocess.check_output(docker + ["logs", name], text=True, stderr=subprocess.STDOUT, timeout=10)
        require_graceful_shutdown(state, logs)
        result = subprocess.run(
            docker
            + [
                "run",
                "--rm",
                "--network",
                "none",
                args.image,
                "/app/.venv/bin/python",
                "-c",
                "raise SystemExit(37)",
            ],
            capture_output=True,
            timeout=20,
        )
        assert result.returncode == 37, "Init must preserve the child command's exit status"
    except BaseException as error:
        original_error = error
    cleanup_owned_resources(docker, args.image, name, schema, environment, original_error)
    print(
        "Default image command passed: authenticated readiness, UID10001, orphan reaping, SIGTERM and child exit status"
    )


if __name__ == "__main__":
    main()
