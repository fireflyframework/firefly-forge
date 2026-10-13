# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Load mounted private credentials before booting the unchanged released pyfly server."""

import json
import os
import stat
import subprocess
import sys

path = "/run/knowledge/service.json"
descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
with os.fdopen(descriptor) as source:
    info = os.fstat(source.fileno())
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_mode & 0o077:
        raise SystemExit("Knowledge credentials must be private regular files")
    settings = json.load(source)
if not settings.get("api_key") or not settings.get("database_url"):
    raise SystemExit("Knowledge authentication and database configuration are required")
os.environ["FLYCANON_API_KEYS"] = settings["api_key"]
os.environ["FLYCANON_DATABASE_URL"] = settings["database_url"]
os.environ["FLYCANON_WEBHOOK_SECRET"] = settings.get("webhook_secret", "")
os.environ["FLYCANON_ADMIN_REQUIRE_AUTH"] = "true"
if sys.argv[1:] == ["migrate"]:
    raise SystemExit(subprocess.call([sys.executable, "-m", "alembic", "upgrade", "head"]))
if sys.argv[1:] == ["worker"]:
    os.execvp("flycanon", ["flycanon", "worker"])
if sys.argv[1:] != ["serve"]:
    raise SystemExit("Choose migrate, serve or worker")
os.environ["_PYFLY_SERVER_TYPE"] = "uvicorn"
os.environ["_PYFLY_SERVER_HOST"] = "0.0.0.0"
os.environ["_PYFLY_SERVER_PORT"] = "8500"
os.execvp(
    "uvicorn",
    [
        "uvicorn",
        "flycanon.main:app",
        "--host",
        "0.0.0.0",
        "--port",
        "8500",
        "--ssl-certfile",
        "/run/knowledge/server.crt",
        "--ssl-keyfile",
        "/run/knowledge/server.key",
    ],
)
