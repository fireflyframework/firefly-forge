# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Install the released server separately from forge's framework dependencies."""

import hashlib
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

RELEASE = "https://github.com/fireflyframework/flycanon/releases/download/v26.10.5/"
ASSETS = {
    "flycanon-26.10.5-py3-none-any.whl": "1f0099aeea2d3c3d775ae7cae81b343dad8ca635ab0a1ffda6236e3481230ea6",
    "flycanon-26.10.5.tar.gz": "1db735770081a845b125f23ba9fdaaae81f7213cea61f6a1e272dcfe85c1957f",
    "runtime-requirements-26.10.5.txt": "f2452338a89e24962d9348c42ec75cca6d3437ddbaa322649384125fe6022d6a",
}

with tempfile.TemporaryDirectory() as directory:
    target = Path(directory)
    for name, digest in ASSETS.items():
        with urllib.request.urlopen(RELEASE + name, timeout=120) as response:
            data = response.read()
        if hashlib.sha256(data).hexdigest() != digest:
            raise SystemExit("Released flycanon asset checksum mismatch: " + name)
        (target / name).write_bytes(data)
    with tarfile.open(target / "flycanon-26.10.5.tar.gz") as archive:
        members = [
            member
            for member in archive.getmembers()
            if member.name.startswith("flycanon-26.10.5/migrations/") or member.name == "flycanon-26.10.5/alembic.ini"
        ]
        archive.extractall(target / "scaffold", members=members, filter="data")
    import shutil

    scaffold = target / "scaffold/flycanon-26.10.5"
    shutil.copytree(scaffold / "migrations", "/app/migrations")
    shutil.copyfile(scaffold / "alembic.ini", "/app/alembic.ini")
    requirements = (target / "runtime-requirements-26.10.5.txt").read_text().splitlines()
    requirements[0] = "flycanon @ " + (target / "flycanon-26.10.5-py3-none-any.whl").as_uri()
    (target / "install.txt").write_text("\n".join(requirements) + "\n")
    subprocess.run(
        [sys.executable, "-m", "pip", "install", "--no-cache-dir", "-r", str(target / "install.txt")], check=True
    )
    # The release's pyfly configuration is packaged as a separately checksummed asset.
    with urllib.request.urlopen(RELEASE + "pyfly-26.10.5.yaml", timeout=120) as response:
        configuration = response.read()
    if hashlib.sha256(configuration).hexdigest() != "5288abf593807f5bdc6642c361ca033741f35b9fca940792778e28f97b2adc1e":
        raise SystemExit("Released flycanon configuration checksum mismatch")
    Path("/app/pyfly.yaml").write_bytes(configuration)
