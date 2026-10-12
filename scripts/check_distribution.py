# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Reject private, credential and process content in release archives."""

from __future__ import annotations

import sys
import tarfile
import zipfile
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
        else:
            with tarfile.open(archive) as source:
                names = source.getnames()
        for name in names:
            parts = Path(name).parts
            if DENIED.intersection(parts) or "bridge" in parts or any("plans" == part for part in parts):
                raise SystemExit(f"Release contains excluded content: {name}")
        if archive.suffix == ".whl" and not any(name.endswith("static/index.html") for name in names):
            raise SystemExit("Wheel does not include the built Studio")
        print(f"Verified release boundary: {archive.name} ({len(names)} entries)")


if __name__ == "__main__":
    inspect(Path(sys.argv[1]))
