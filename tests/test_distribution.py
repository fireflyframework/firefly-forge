# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Release inspection rejects generated compiler state in either native archive."""

import io
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest


def archives(directory: Path, target: str | None = None, cache: str = "") -> None:
    with zipfile.ZipFile(directory / "firefly_forge-0.2.3-py3-none-any.whl", "w") as wheel:
        wheel.writestr(
            "firefly_forge-0.2.3.dist-info/METADATA",
            "Metadata-Version: 2.4\nName: firefly-forge\nVersion: 0.2.3\n"
            "Requires-Dist: flycanon-sdk @ https://github.com/fireflyframework/flycanon/releases/"
            "download/v26.10.0/flycanon_sdk-26.10.0-py3-none-any.whl\n",
        )
        wheel.writestr("firefly_forge/static/index.html", "<html>Studio fixture</html>")
        if target == "wheel":
            wheel.writestr(cache, "generated compiler bookkeeping")
    with tarfile.open(directory / "firefly_forge-0.2.3.tar.gz", "w:gz") as source:
        entries = [
            ("firefly_forge-0.2.3/src/firefly_forge/__init__.py", b""),
            ("firefly_forge-0.2.3/studio/dist/index.html", b"<html>Studio fixture</html>"),
        ]
        if target == "sdist":
            entries.append(("firefly_forge-0.2.3/" + cache, b"compiler bookkeeping"))
        for name, content in entries:
            info = tarfile.TarInfo(name)
            info.size = len(content)
            source.addfile(info, io.BytesIO(content))


def inspect(directory: Path) -> subprocess.CompletedProcess[str]:
    script = Path(__file__).resolve().parents[1] / "scripts" / "check_distribution.py"
    return subprocess.run([sys.executable, str(script), str(directory)], capture_output=True, text=True)


@pytest.mark.parametrize("target", ["wheel", "sdist"])
@pytest.mark.parametrize(
    "cache", ["tsconfig.tsbuildinfo", "studio/tsconfig.tsbuildinfo", "studio/nested/app.tsbuildinfo"]
)
def test_distribution_rejects_generated_typescript_caches(tmp_path, target, cache):
    archives(tmp_path, target, cache)
    result = inspect(tmp_path)
    assert result.returncode != 0
    assert "excluded content" in result.stderr and cache in result.stderr


def test_distribution_preserves_source_and_bundled_studio(tmp_path):
    archives(tmp_path)
    result = inspect(tmp_path)
    assert result.returncode == 0, result.stderr
    assert result.stdout.count("Verified release boundary:") == 2
