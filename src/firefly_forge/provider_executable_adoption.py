# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Bounded entrypoint identity for an explicitly reviewed CLI recovery."""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path
from typing import Any


def executable_identity(configured: str, workspaces: Path) -> dict[str, Any]:
    path = Path(configured)
    try:
        if not configured or not path.is_absolute() or path.is_symlink():
            raise ValueError("CLI adoption requires an absolute regular executable, without a symlink")
        canonical = path.resolve(strict=True)
        if canonical.is_relative_to(workspaces.resolve()):
            raise ValueError("A project workspace cannot supply the adopted CLI executable")
        with os.fdopen(os.open(canonical, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as stream:
            before = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(before.st_mode)
                or not before.st_mode & 0o111
                or before.st_mode & 0o022
                or not 0 < before.st_size <= 512 * 1024 * 1024
                or not os.access(canonical, os.X_OK)
            ):
                raise ValueError(
                    "The adopted CLI must be an executable regular file within 512 MiB, not writable by others"
                )
            checksum, size = hashlib.sha256(), 0
            while chunk := stream.read(1024 * 1024):
                size += len(chunk)
                if size > 512 * 1024 * 1024:
                    raise ValueError("The CLI executable changed beyond the adoption size bound")
                checksum.update(chunk)
            after = os.fstat(stream.fileno())
        fields = ("st_dev", "st_ino", "st_size", "st_mode", "st_uid", "st_gid", "st_mtime_ns", "st_ctime_ns")
        identity = {name[3:]: getattr(before, name) for name in fields}
        if (
            size != before.st_size
            or identity != {name[3:]: getattr(after, name) for name in fields}
            or identity != {name[3:]: getattr(canonical.lstat(), name) for name in fields}
            or path.is_symlink()
            or path.resolve(strict=True) != canonical
        ):
            raise ValueError("The CLI executable changed during adoption review")
    except OSError as exc:
        raise ValueError("The configured CLI executable is unavailable for adoption") from exc
    return {"path": str(canonical), "sha256": checksum.hexdigest(), **identity}
