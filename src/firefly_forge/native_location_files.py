# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Read retained local storage identity without creating or hydrating private data."""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import stat
import sys
from dataclasses import dataclass, field
from pathlib import Path

from firefly_forge.native_location_models import plain_location
from firefly_forge.source_catalog import _stamp


def _identity(path: Path) -> tuple[int, ...]:
    value = path.lstat()
    return (*_stamp(value), getattr(value, "st_flags", 0))


def _root_identity(path: Path) -> tuple[int, ...]:
    value = _identity(path)
    return (*value[:5], value[-1])


@dataclass(frozen=True)
class NativeStorageIdentity:
    path: Path
    _value: bytes = field(repr=False)
    _file_stamp: tuple[int, ...] = field(repr=False)
    _root_stamp: tuple[int, ...] = field(repr=False)

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self._value).hexdigest()

    def matches(self, value: str) -> bool:
        return hmac.compare_digest(self._value, value.encode())

    def sign(self, value: bytes) -> str:
        return hmac.new(self._value, value, hashlib.sha256).hexdigest()

    def fence(self) -> None:
        if _identity(self.path) != self._file_stamp or _root_identity(self.path.parent) != self._root_stamp:
            raise ValueError("Retained native storage identity changed")


def read_storage_identity(root: Path) -> NativeStorageIdentity:
    plain_location(str(root))
    try:
        folder = root.lstat()
        if (
            root.resolve() != root
            or not stat.S_ISDIR(folder.st_mode)
            or folder.st_uid != os.getuid()
            or folder.st_mode & 0o022
            or getattr(folder, "st_flags", 0) & 0x40000000
        ):
            raise ValueError("Retained native storage requires its real owner-controlled directory")
        path = root / "runtime-storage.identity"
        before = path.lstat()
        if getattr(before, "st_flags", 0) & 0x40000000:
            raise ValueError("Retained native storage identity is unavailable locally")
        if (
            not stat.S_ISREG(before.st_mode)
            or stat.S_IMODE(before.st_mode) != 0o600
            or before.st_uid != os.getuid()
            or before.st_nlink != 1
            or before.st_size != 64
        ):
            raise ValueError("Retained native storage identity requires exact private ordinary bytes")
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as stream:
            if _stamp(os.fstat(stream.fileno())) != _stamp(before):
                raise ValueError("Retained native storage identity changed before opening")
            value = stream.read(65)
            if not re.fullmatch(b"[a-f0-9]{64}", value):
                raise ValueError("Retained native storage identity bytes are invalid")
            if _stamp(os.fstat(stream.fileno())) != _stamp(before):
                raise ValueError("Retained native storage identity changed while reading")
        captured = NativeStorageIdentity(
            path,
            value,
            (*_stamp(before), getattr(before, "st_flags", 0)),
            (*_stamp(folder)[:5], getattr(folder, "st_flags", 0)),
        )
        captured.fence()
        return captured
    except FileNotFoundError:
        raise ValueError("The existing retained native storage identity is required") from None


def _owned_bytes(path: Path, limit: int) -> bytes:
    before = path.lstat()
    if (
        path.resolve() != path
        or not stat.S_ISREG(before.st_mode)
        or before.st_uid != os.getuid()
        or before.st_mode & 0o022
        or before.st_nlink != 1
        or before.st_size > limit
        or getattr(before, "st_flags", 0) & 0x40000000
    ):
        raise ValueError("Native location proof requires locally available owned ordinary files")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        if _identity(path) != (*_stamp(before), getattr(before, "st_flags", 0)):
            raise ValueError("Native location proof changed before opening")
        if _stamp(os.fstat(stream.fileno())) != _stamp(before):
            raise ValueError("Native location proof changed before opening")
        value = stream.read(limit + 1)
        if len(value) > limit or _stamp(os.fstat(stream.fileno())) != _stamp(before):
            raise ValueError("Native location proof changed while reading")
    if _identity(path) != (*_stamp(before), getattr(before, "st_flags", 0)):
        raise ValueError("Native location proof changed after reading")
    return value


@dataclass(frozen=True)
class NativePostgresIdentity:
    directory: Path
    system_identifier: str
    postmaster_pid: int
    started_at: int
    port: int


def read_postgres_identity(directory: Path) -> NativePostgresIdentity:
    plain_location(str(directory))
    folder = directory.lstat()
    if (
        directory.resolve() != directory
        or not stat.S_ISDIR(folder.st_mode)
        or folder.st_uid != os.getuid()
        or folder.st_mode & 0o077
        or getattr(folder, "st_flags", 0) & 0x40000000
    ):
        raise ValueError("Offline native maintenance requires its real private PostgreSQL directory")
    if _owned_bytes(directory / "PG_VERSION", 32) != b"17\n":
        raise ValueError("Native location maintenance supports the PostgreSQL 17 control layout")
    control = _owned_bytes(directory / "global" / "pg_control", 8192)
    lines = _owned_bytes(directory / "postmaster.pid", 8192).decode("utf-8").splitlines()
    if len(control) != 8192 or len(lines) < 4 or lines[1] != str(directory):
        raise ValueError("PostgreSQL control and running directory identity are unavailable")
    try:
        pid, started, port = (int(lines[index]) for index in (0, 2, 3))
    except ValueError:
        raise ValueError("PostgreSQL postmaster identity is invalid") from None
    system = int.from_bytes(control[:8], sys.byteorder)
    if min(pid, started, system) <= 0 or not 0 < port < 65536:
        raise ValueError("PostgreSQL postmaster identity is invalid")
    return NativePostgresIdentity(directory, str(system), pid, started, port)
