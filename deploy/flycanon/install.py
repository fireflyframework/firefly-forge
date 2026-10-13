# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Install the released server separately from forge's framework dependencies."""

import hashlib
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

RELEASE = "https://github.com/firefly-operationOS/flycanon/releases/download/v26.10.0/"
ASSETS = {
    "flycanon-26.10.0-py3-none-any.whl": "1610e4762ab5898283b9a0d6ee3673b794375ee433c1e0535cadd664e81ffe8d",
    "flycanon-26.10.0.tar.gz": "11e36445d3675efdda16fddf82aa3e19640b70a77329549c876b29cde3adeaa7",
    "runtime-requirements-26.10.0.txt": "1212dc97ae3b4215dd669ddd149a4dbc3be0adb6f676eb2b7d05278ed8df5335",
}

with tempfile.TemporaryDirectory() as directory:
    target = Path(directory)
    for name, digest in ASSETS.items():
        with urllib.request.urlopen(RELEASE + name, timeout=120) as response:
            data = response.read()
        if hashlib.sha256(data).hexdigest() != digest:
            raise SystemExit("Released flycanon asset checksum mismatch: " + name)
        (target / name).write_bytes(data)
    with tarfile.open(target / "flycanon-26.10.0.tar.gz") as archive:
        members = [
            member
            for member in archive.getmembers()
            if member.name.startswith("flycanon-26.10.0/migrations/") or member.name == "flycanon-26.10.0/alembic.ini"
        ]
        archive.extractall(target / "scaffold", members=members, filter="data")
    import shutil

    scaffold = target / "scaffold/flycanon-26.10.0"
    shutil.copytree(scaffold / "migrations", "/app/migrations")
    shutil.copyfile(scaffold / "alembic.ini", "/app/alembic.ini")
    requirements = (target / "runtime-requirements-26.10.0.txt").read_text().splitlines()
    requirements[0] = "flycanon @ " + (target / "flycanon-26.10.0-py3-none-any.whl").as_uri()
    (target / "install.txt").write_text("\n".join(requirements) + "\n")
    subprocess.run(
        [sys.executable, "-m", "pip", "install", "--no-cache-dir", "-r", str(target / "install.txt")], check=True
    )
    # The release's pyfly configuration is packaged as a separately checksummed asset.
    with urllib.request.urlopen(RELEASE + "pyfly-26.10.0.yaml", timeout=120) as response:
        configuration = response.read()
    if hashlib.sha256(configuration).hexdigest() != "5288abf593807f5bdc6642c361ca033741f35b9fca940792778e28f97b2adc1e":
        raise SystemExit("Released flycanon configuration checksum mismatch")
    Path("/app/pyfly.yaml").write_bytes(configuration)
