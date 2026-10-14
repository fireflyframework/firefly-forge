# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Reject private, credential and process content in release archives."""

from __future__ import annotations

import sys
import tarfile
import zipfile
from email.parser import BytesParser
from pathlib import Path

DENIED = {".env", ".codex", ".claude", ".superpowers", ".forge", "AGENTS.md", "CLAUDE.md", "node_modules", "work"}


def inspect(directory: Path) -> None:
    archives = list(directory.glob("*.whl")) + list(directory.glob("*.tar.gz"))
    if len(archives) < 2:
        raise SystemExit("Expected a wheel and source distribution")
    for archive in archives:
        if archive.suffix == ".whl":
            with zipfile.ZipFile(archive) as wheel:
                names = wheel.namelist()
                metadata_path = next(name for name in names if name.endswith(".dist-info/METADATA"))
                requirements = BytesParser().parsebytes(wheel.read(metadata_path)).get_all("Requires-Dist", [])
                if not any(
                    requirement.startswith("flycanon-sdk@ https://github.com/firefly-operationOS/flycanon/releases/")
                    or requirement.startswith(
                        "flycanon-sdk @ https://github.com/firefly-operationOS/flycanon/releases/"
                    )
                    for requirement in requirements
                ):
                    raise SystemExit("Wheel must retain the Flycanon SDK release URL for standalone installation")
        else:
            with tarfile.open(archive) as source:
                names = source.getnames()
        for name in names:
            parts = Path(name).parts
            if (
                DENIED.intersection(parts)
                or "bridge" in parts
                or any(part == "plans" for part in parts)
                or name.endswith(".tsbuildinfo")
            ):
                raise SystemExit(f"Release contains excluded content: {name}")
        if archive.suffix == ".whl" and not any(name.endswith("static/index.html") for name in names):
            raise SystemExit("Wheel does not include the built Studio")
        print(f"Verified release boundary: {archive.name} ({len(names)} entries)")


if __name__ == "__main__":
    inspect(Path(sys.argv[1]))
