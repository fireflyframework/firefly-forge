# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Immutable private JSON tool results; journal receipts retain exact content digests."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any


def encoded(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()


@contextmanager
def directory(root: Path) -> Iterator[int]:
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise ValueError("Agentic tool artifact directory must remain private and owned")
        yield fd
    finally:
        os.close(fd)


def retain(root: Path, value: Any) -> dict[str, Any]:
    raw = encoded(value)
    sha = hashlib.sha256(raw).hexdigest()
    descriptor = {"version": 1, "sha256": sha, "bytes": len(raw), "media_type": "application/json"}
    with directory(root) as folder:
        try:
            fd = os.open(sha + ".json", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=folder)
        except FileExistsError:
            load(root, descriptor)
        else:
            with os.fdopen(fd, "wb") as target:
                target.write(raw)
                target.flush()
                os.fsync(target.fileno())
            os.fsync(folder)
    return descriptor


def load(root: Path, descriptor: dict[str, Any]) -> Any:
    if (
        set(descriptor) != {"version", "sha256", "bytes", "media_type"}
        or type(descriptor["version"]) is not int
        or descriptor["version"] != 1
        or descriptor["media_type"] != "application/json"
        or not isinstance(descriptor["sha256"], str)
        or not re.fullmatch(r"[a-f0-9]{64}", descriptor["sha256"])
        or type(descriptor["bytes"]) is not int
        or descriptor["bytes"] < 1
    ):
        raise ValueError("Agentic tool artifact descriptor changed")
    try:
        with directory(root) as folder:
            fd = os.open(descriptor["sha256"] + ".json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=folder)
            with os.fdopen(fd, "rb") as source:
                info = os.fstat(source.fileno())
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_nlink != 1
                    or info.st_uid != os.geteuid()
                    or stat.S_IMODE(info.st_mode) & 0o077
                    or info.st_size != descriptor["bytes"]
                ):
                    raise ValueError("Agentic tool artifact custody changed")
                raw = source.read(descriptor["bytes"] + 1)
        if len(raw) != descriptor["bytes"] or hashlib.sha256(raw).hexdigest() != descriptor["sha256"]:
            raise ValueError("Agentic tool artifact content changed")
        value = json.loads(raw)
        if encoded(value) != raw:
            raise ValueError("Agentic tool artifact encoding changed")
        return value
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("Agentic tool artifact is unavailable") from exc
