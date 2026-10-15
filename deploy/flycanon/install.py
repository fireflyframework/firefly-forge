# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Install the released server separately from forge's framework dependencies."""

import hashlib
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

RELEASE = "https://github.com/fireflyframework/flycanon/releases/download/v26.10.4/"
ASSETS = {
    "flycanon-26.10.4-py3-none-any.whl": "5e430634e5d67cdaba441284df51c74beecd10175f07348a3015ab4a7a6108d0",
    "flycanon-26.10.4.tar.gz": "15d7bf40b2fe4092877e59a770bee401ad727870deb05063b5083f6e31321028",
    "runtime-requirements-26.10.4.txt": "7b010fc4064b01203352a23af6e0a5bf7cc046ab29df7c638be9fd7f5ccf3ca1",
}

with tempfile.TemporaryDirectory() as directory:
    target = Path(directory)
    for name, digest in ASSETS.items():
        with urllib.request.urlopen(RELEASE + name, timeout=120) as response:
            data = response.read()
        if hashlib.sha256(data).hexdigest() != digest:
            raise SystemExit("Released flycanon asset checksum mismatch: " + name)
        (target / name).write_bytes(data)
    with tarfile.open(target / "flycanon-26.10.4.tar.gz") as archive:
        members = [
            member
            for member in archive.getmembers()
            if member.name.startswith("flycanon-26.10.4/migrations/") or member.name == "flycanon-26.10.4/alembic.ini"
        ]
        archive.extractall(target / "scaffold", members=members, filter="data")
    import shutil

    scaffold = target / "scaffold/flycanon-26.10.4"
    shutil.copytree(scaffold / "migrations", "/app/migrations")
    shutil.copyfile(scaffold / "alembic.ini", "/app/alembic.ini")
    requirements = (target / "runtime-requirements-26.10.4.txt").read_text().splitlines()
    requirements[0] = "flycanon @ " + (target / "flycanon-26.10.4-py3-none-any.whl").as_uri()
    (target / "install.txt").write_text("\n".join(requirements) + "\n")
    subprocess.run(
        [sys.executable, "-m", "pip", "install", "--no-cache-dir", "-r", str(target / "install.txt")], check=True
    )
    # The release's pyfly configuration is packaged as a separately checksummed asset.
    with urllib.request.urlopen(RELEASE + "pyfly-26.10.4.yaml", timeout=120) as response:
        configuration = response.read()
    if hashlib.sha256(configuration).hexdigest() != "5288abf593807f5bdc6642c361ca033741f35b9fca940792778e28f97b2adc1e":
        raise SystemExit("Released flycanon configuration checksum mismatch")
    Path("/app/pyfly.yaml").write_bytes(configuration)
