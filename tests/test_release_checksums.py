# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
import hashlib
import subprocess
import sys
from pathlib import Path


def test_release_checksums_match_upload_glob_and_ignore_packaging_bookkeeping(tmp_path):
    artifacts = {
        "firefly_forge-0.2.1-py3-none-any.whl": b"wheel artifact",
        "firefly_forge-0.2.1.tar.gz": b"source artifact",
        "firefly-forge-studio.tar.gz": b"studio artifact",
    }
    for name, body in artifacts.items():
        (tmp_path / name).write_bytes(body)
    (tmp_path / ".gitignore").write_text("*\n")
    (tmp_path / ".cache").write_text("packaging bookkeeping")
    (tmp_path / "SHA256SUMS").write_text("stale manifest")
    script = Path(__file__).resolve().parents[1] / "scripts" / "release_checksums.py"

    subprocess.run([sys.executable, str(script), str(tmp_path)], check=True)

    expected = "".join(
        f"{hashlib.sha256(body).hexdigest()}  {name}\n" for name, body in sorted(artifacts.items())
    )
    assert (tmp_path / "SHA256SUMS").read_text() == expected
    subprocess.run([sys.executable, str(script), str(tmp_path)], check=True)
    assert (tmp_path / "SHA256SUMS").read_text() == expected
