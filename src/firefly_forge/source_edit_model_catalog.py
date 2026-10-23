# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Package-owned client presentation of one pinned public native model catalog."""

from __future__ import annotations

import hashlib
import json
import stat
from pathlib import Path

CATALOG_MODEL = "gpt-6-astra"
CATALOG_SHA256 = "0321266b8a9e1812c9fdc11d64cd36201a4d070d93ca3808186fada2cd149094"
CATALOG_PATH = Path(__file__).parent / "resources" / "native" / "codex-0.163.0" / "source-only-model.json"


def model_catalog_path(model: str) -> Path:
    """Validate the same immutable catalog before metadata and source execution."""
    if model != CATALOG_MODEL:
        raise ValueError("The native model has no qualified package-owned catalog")
    try:
        metadata = CATALOG_PATH.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or any(parent.is_symlink() for parent in CATALOG_PATH.parents)
        ):
            raise ValueError("The native model catalog is aliased or unavailable")
        with CATALOG_PATH.open("rb") as stream:
            content = stream.read(512 * 1024 + 1)
        if len(content) > 512 * 1024 or hashlib.sha256(content).hexdigest() != CATALOG_SHA256:
            raise ValueError("The native model catalog content changed")
        parsed = json.loads(content)
        models = parsed.get("models") if isinstance(parsed, dict) else None
        if (
            not isinstance(models, list)
            or len(models) != 1
            or not isinstance(models[0], dict)
            or models[0].get("slug") != model
            or models[0].get("tool_mode") != "direct"
        ):
            raise ValueError("The native model catalog identity changed")
        return CATALOG_PATH.resolve(strict=True)
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ValueError("The native model catalog is unavailable") from None


def model_catalog_sha256(model: str) -> str:
    model_catalog_path(model)
    return CATALOG_SHA256


def model_catalog_override(model: str) -> str:
    return "model_catalog_json=" + json.dumps(str(model_catalog_path(model)))
