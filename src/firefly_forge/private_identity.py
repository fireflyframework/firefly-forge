# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Publish complete private identities once, without replacing retained bytes."""

from __future__ import annotations

import os
import stat
import tempfile
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path

from filelock import FileLock


def private_identity(path: Path, create: Callable[[], bytes], *, size: int) -> bytes:
    def read() -> bytes:
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            raise
        except OSError:
            raise ValueError("Private identity must be an owner-only regular file") from None
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_uid != os.getuid()
                or info.st_nlink != 1
                or info.st_size != size
            ):
                raise ValueError("Private identity must be a complete owner-only regular file with one link")
            value = stream.read(size + 1)
            if len(value) != size:
                raise ValueError("Private identity size changed while reading")
            return value

    # Serializing readers also hides the temporary second hardlink during publication.
    with FileLock(str(path) + ".lock", timeout=5, mode=0o600):
        try:
            return read()
        except FileNotFoundError:
            pass
        value = create()
        if len(value) != size:
            raise ValueError("Private identity generator returned an invalid size")
        fd, temporary = tempfile.mkstemp(prefix=".identity-", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(value)
                stream.flush()
                os.fsync(stream.fileno())
            with suppress(FileExistsError):
                os.link(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return read()
