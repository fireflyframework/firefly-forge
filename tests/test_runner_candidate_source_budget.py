# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Optional read checkpoints preserve the default archive contract and close descriptors."""

import os

import pytest

from firefly_forge.sandbox import pack_workspace


def test_optional_checkpoint_preserves_exact_default_archive_bytes(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_bytes(b"print('owned')\n" * 10000)
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "private").write_text("excluded")
    before = pack_workspace(tmp_path, 1_000_000)
    calls = []
    assert pack_workspace(tmp_path, 1_000_000, _checkpoint=lambda: calls.append(True)) == before
    assert len(calls) >= 3


def test_checkpoint_refuses_before_read_and_closes_owned_descriptor(tmp_path, monkeypatch):
    (tmp_path / "main.py").write_text("owned")
    opened, closed = [], []
    original_open, original_close = os.open, os.close

    def opening(*args, **kwargs):
        descriptor = original_open(*args, **kwargs)
        opened.append(descriptor)
        return descriptor

    def closing(descriptor):
        closed.append(descriptor)
        return original_close(descriptor)

    checks = []

    def refuse():
        checks.append(True)
        if len(checks) >= 3:
            raise TimeoutError("owned cooperative deadline")

    monkeypatch.setattr(os, "open", opening)
    monkeypatch.setattr(os, "close", closing)
    with pytest.raises(TimeoutError, match="owned cooperative deadline"):
        pack_workspace(tmp_path, 1024, _checkpoint=refuse)
    assert opened and set(opened) <= set(closed)
