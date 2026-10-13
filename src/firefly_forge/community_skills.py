# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Pinned, licensed community instruction adaptations; no downloaded tool execution."""

from __future__ import annotations

import hashlib
import json
from importlib.resources import files
from typing import Any


def community_catalog() -> list[dict[str, Any]]:
    catalog = json.loads(files("firefly_forge").joinpath("data/community-skills.json").read_text())
    result: list[dict[str, Any]] = catalog["skills"]
    for skill in result:
        provenance = skill["provenance"]
        if hashlib.sha256(skill["instructions"].encode()).hexdigest() != skill["adaptation_sha256"]:
            raise ValueError("Community instruction adaptation hash mismatch")
        if hashlib.sha256(provenance["license_text"].encode()).hexdigest() != provenance["license_sha256"]:
            raise ValueError("Community skill license hash mismatch")
    return result
