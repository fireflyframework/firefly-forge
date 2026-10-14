# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Install the released server separately from forge's framework dependencies."""

import hashlib
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

RELEASE = "https://github.com/fireflyframework/flycanon/releases/download/v26.10.3/"
ASSETS = {
    "flycanon-26.10.3-py3-none-any.whl": "4301cd499629c91c3fe4a20089b12082603219575e809e9faac2e72edd4c1172",
    "flycanon-26.10.3.tar.gz": "4e99b76bc8d019d6f8b7686222ed42120e6b97feeeaeb5c29178e2e3fe76657f",
    "runtime-requirements-26.10.3.txt": "b9a13d386f24a94dff57b1f69533a6378b97a59db6759e9c9bc60c41d5f03790",
}

with tempfile.TemporaryDirectory() as directory:
    target = Path(directory)
    for name, digest in ASSETS.items():
        with urllib.request.urlopen(RELEASE + name, timeout=120) as response:
            data = response.read()
        if hashlib.sha256(data).hexdigest() != digest:
            raise SystemExit("Released flycanon asset checksum mismatch: " + name)
        (target / name).write_bytes(data)
    with tarfile.open(target / "flycanon-26.10.3.tar.gz") as archive:
        members = [
            member
            for member in archive.getmembers()
            if member.name.startswith("flycanon-26.10.3/migrations/") or member.name == "flycanon-26.10.3/alembic.ini"
        ]
        archive.extractall(target / "scaffold", members=members, filter="data")
    import shutil

    scaffold = target / "scaffold/flycanon-26.10.3"
    shutil.copytree(scaffold / "migrations", "/app/migrations")
    shutil.copyfile(scaffold / "alembic.ini", "/app/alembic.ini")
    requirements = (target / "runtime-requirements-26.10.3.txt").read_text().splitlines()
    requirements[0] = "flycanon @ " + (target / "flycanon-26.10.3-py3-none-any.whl").as_uri()
    (target / "install.txt").write_text("\n".join(requirements) + "\n")
    subprocess.run(
        [sys.executable, "-m", "pip", "install", "--no-cache-dir", "-r", str(target / "install.txt")], check=True
    )
    # The release's pyfly configuration is packaged as a separately checksummed asset.
    with urllib.request.urlopen(RELEASE + "pyfly-26.10.3.yaml", timeout=120) as response:
        configuration = response.read()
    if hashlib.sha256(configuration).hexdigest() != "5288abf593807f5bdc6642c361ca033741f35b9fca940792778e28f97b2adc1e":
        raise SystemExit("Released flycanon configuration checksum mismatch")
    Path("/app/pyfly.yaml").write_bytes(configuration)
