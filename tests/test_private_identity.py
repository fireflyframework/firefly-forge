# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Private identity locks must never mutate another retained inode."""

import os

import pytest

from firefly_forge.private_identity import private_identity


def test_hardlinked_lock_is_rejected_without_truncating_retained_key(tmp_path):
    retained = tmp_path / "retained.key"
    retained.write_bytes(b"a" * 64)
    retained.chmod(0o600)
    marker = tmp_path / "marker"
    os.link(retained, str(marker) + ".lock")
    try:
        with pytest.raises(ValueError, match="lock"):
            private_identity(marker, lambda: b"b" * 64, size=64)
    finally:
        assert retained.read_bytes() == b"a" * 64
    assert not marker.exists()


@pytest.mark.parametrize("invalid", ["symlink", "public", "directory"])
def test_private_identity_rejects_unsafe_lock_without_changing_it(tmp_path, invalid):
    lock = tmp_path / "marker.lock"
    if invalid == "symlink":
        target = tmp_path / "retained.key"
        target.write_bytes(b"retained")
        target.chmod(0o600)
        lock.symlink_to(target)
    elif invalid == "public":
        lock.write_bytes(b"retained")
        lock.chmod(0o644)
    else:
        lock.mkdir()
    with pytest.raises(ValueError, match="lock"):
        private_identity(tmp_path / "marker", lambda: b"b" * 64, size=64)
    if invalid != "directory":
        assert lock.read_bytes() == b"retained"


def test_existing_private_identity_is_never_replaced(tmp_path):
    marker = tmp_path / "marker"
    marker.write_bytes(b"a" * 64)
    marker.chmod(0o600)
    assert private_identity(marker, lambda: b"b" * 64, size=64) == b"a" * 64
    assert marker.read_bytes() == b"a" * 64
