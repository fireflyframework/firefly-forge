# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Diagnostic-branch pytest observer; the original metadata fence stays authoritative."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
from pathlib import Path

SELECTED_TEST = (
    "tests/test_learning_observation_receipts.py::"
    "test_native_exact_historical_observation_receipt_is_readable_without_remote_io"
)
PROTECTED = (
    "config",
    "config.worktree",
    "packed-refs",
    "hooks",
    "info",
    "objects/info",
    "refs/replace",
    "shallow",
    "commondir",
    "worktrees",
)


def inventory(workspace):
    root = workspace / ".git"
    if workspace.is_symlink() or root.is_symlink() or not root.is_dir():
        raise ValueError("Diagnostic Git metadata must be an owned directory")
    files = {}
    size = 0
    for name in PROTECTED:
        item = root / name
        if any(parent.is_symlink() for parent in item.parents if parent != root and parent.is_relative_to(root)):
            raise ValueError("Diagnostic Git metadata cannot redirect through symlinks")
        paths = [item]
        if item.is_dir() and not item.is_symlink():
            for current, directories, names in os.walk(item, followlinks=False):
                paths.extend(Path(current) / entry for entry in [*directories, *names])
                if len(paths) > 600:
                    raise ValueError("Diagnostic inventory exceeds its inspection bound")
        for path in paths:
            if not path.exists() and not path.is_symlink():
                continue
            info = path.lstat()
            if stat.S_ISDIR(info.st_mode):
                continue
            row = {"mode": stat.S_IMODE(info.st_mode), "links": info.st_nlink, "type": stat.S_IFMT(info.st_mode)}
            if stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as stream:
                    actual = os.fstat(stream.fileno())
                    if not stat.S_ISREG(actual.st_mode) or actual.st_nlink != 1:
                        raise ValueError("Diagnostic inventory requires owned regular files")
                    content = stream.read(1_000_001)
                size += len(content)
                if size > 1_000_000:
                    raise ValueError("Diagnostic inventory exceeds its inspection bound")
                row.update(sha256=hashlib.sha256(content).hexdigest(), bytes=len(content))
            files[path.relative_to(root).as_posix()] = row
            if len(files) > 300:
                raise ValueError("Diagnostic inventory exceeds its inspection bound")
    return {"files": files}


def observed_inventory(workspace):
    try:
        return inventory(workspace)
    except Exception as exc:
        # Diagnostic I/O must never substitute an exception from the original fence.
        return {"files": {}, "error_type": type(exc).__name__}


class MetadataDiagnostic:
    def __init__(self, original, output, worker):
        self.original = original
        self.output = output / worker
        self.nodeid = None
        self.sequence = 0
        self.baselines = {}

    def record(self, phase, row):
        try:
            self.output.mkdir(parents=True, exist_ok=True)
            ordinal = 0 if phase == "before_digest" else 1
            path = self.output / f"metadata-{self.sequence:06d}-{ordinal}-{phase}.json"
            with path.open("x") as stream:
                json.dump({"phase": phase, "sequence": self.sequence, **row}, stream, indent=2)
                stream.write("\n")
        except Exception:
            print("Git metadata diagnostic receipt could not be written", file=sys.stderr)

    def __call__(self, workspace):
        if self.nodeid != SELECTED_TEST:
            return self.original(workspace)
        self.sequence += 1
        key = str(workspace)
        before = observed_inventory(workspace)
        caller = sys._getframe(1)
        common = {
            "nodeid": SELECTED_TEST,
            "workspace_id": hashlib.sha256(key.encode()).hexdigest(),
            "baseline_capture": key not in self.baselines,
            "caller": {
                "file": Path(caller.f_code.co_filename).name,
                "function": caller.f_code.co_name,
                "line": caller.f_lineno,
            },
            "before": before,
            "scope": "Protected relative paths, types, modes, link counts, sizes and content hashes only",
        }
        self.record("before_digest", common)
        try:
            value = self.original(workspace)
        except BaseException as exc:
            self.record("original_refusal", {**common, "original_error_type": type(exc).__name__})
            raise
        after = observed_inventory(workspace)
        baseline = self.baselines.setdefault(key, {"digest": value, "inventory": before})
        initial = baseline["inventory"]["files"]
        current = after["files"]
        changes = [
            {"path": path, "before": initial.get(path), "after": current.get(path)}
            for path in sorted(initial.keys() | current.keys())
            if initial.get(path) != current.get(path)
        ]
        self.record(
            "after_digest",
            {
                **common,
                "after": after,
                "digest": value,
                "baseline_digest": baseline["digest"],
                "digest_differs_from_baseline": value != baseline["digest"],
                "changes_from_baseline": changes,
            },
        )
        return value


_observer = None
_engine = None


def pytest_sessionstart(session):
    global _observer, _engine
    from firefly_forge import engine

    _engine = engine
    worker = os.environ.get("PYTEST_XDIST_WORKER", "master")
    output = Path(os.environ["FORGE_METADATA_DIAGNOSTIC_OUTPUT"])
    _observer = MetadataDiagnostic(engine.metadata_digest, output, worker)
    engine.metadata_digest = _observer


def pytest_runtest_setup(item):
    _observer.nodeid = item.nodeid


def pytest_runtest_teardown(item):
    _observer.nodeid = None


def pytest_sessionfinish(session, exitstatus):
    _engine.metadata_digest = _observer.original
