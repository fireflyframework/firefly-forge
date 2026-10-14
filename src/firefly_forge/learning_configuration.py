# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Private policy revisions captured before learning decisions can affect a run."""

from __future__ import annotations

import copy
import uuid
from pathlib import Path
from typing import Any

from firefly_forge.configuration import ConfigurationStore
from firefly_forge.journal import digest
from firefly_forge.learning_models import LearningPolicy

Data = dict[str, Any]


def learning_snapshot(directory: Path | ConfigurationStore) -> Data:
    private = directory if isinstance(directory, ConfigurationStore) else ConfigurationStore(directory)
    with private.lock:
        record = private._read(private.directory / "learning-policy.json")
    payload = {
        "version": 1,
        "revision": record.get("revision"),
        "settings": LearningPolicy.model_validate(record.get("settings", {})).model_dump(),
    }
    return {**payload, "digest": digest(payload)}


def frozen_learning(snapshot: Data | None) -> Data:
    if snapshot is None:
        payload = {"version": 1, "revision": None, "settings": LearningPolicy(enabled=False).model_dump()}
        return {**payload, "digest": digest(payload)}
    payload = {key: value for key, value in snapshot.items() if key != "digest"}
    if (
        set(payload) != {"version", "revision", "settings"}
        or payload["version"] != 1
        or digest(payload) != snapshot.get("digest")
    ):
        raise ValueError("Learning policy snapshot was changed")
    LearningPolicy.model_validate(payload["settings"])
    return copy.deepcopy(snapshot)


def configure_learning(private: ConfigurationStore, request: LearningPolicy) -> Data:
    with private.lock:
        private._write(
            private.directory / "learning-policy.json", {"revision": uuid.uuid4().hex, "settings": request.model_dump()}
        )
    return learning_snapshot(private)
