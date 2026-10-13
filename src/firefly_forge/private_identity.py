# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Publish complete private identities once, without replacing retained bytes."""

from __future__ import annotations

import errno
import fcntl
import os
import stat
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from pathlib import Path


@contextmanager
def identity_lock(path: Path) -> Iterator[None]:
    try:
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    except OSError:
        raise ValueError("Private identity lock must be an owner-only regular file") from None
    locked = False
    try:

        def validate() -> None:
            info = os.fstat(descriptor)
            named = path.stat(follow_symlinks=False)
            if (
                not stat.S_ISREG(info.st_mode)
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_uid != os.getuid()
                or info.st_nlink != 1
                or (info.st_dev, info.st_ino) != (named.st_dev, named.st_ino)
            ):
                raise ValueError("Private identity lock must retain its private regular inode with one link")

        validate()
        deadline = time.monotonic() + 5
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
                break
            except OSError as exc:
                if exc.errno not in {errno.EAGAIN, errno.EACCES}:
                    raise
                if time.monotonic() >= deadline:
                    raise TimeoutError("Private identity lock remained busy") from None
                time.sleep(0.02)
        validate()
        yield
    finally:
        if locked:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


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
    with identity_lock(Path(str(path) + ".lock")):
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
