# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""The previous measured runtime remains historical, never fresh admission."""

from dataclasses import replace

import pytest

from firefly_forge import source_edit_qualification as q


def test_v23_retains_its_exact_historical_measurement_after_read_recovery_changes():
    old = next(p for p in q._profiles() if p.loader_profile == "codex-exec-restricted-source/v23")
    assert q.REGISTRY_VERSION == 23
    assert q.LOADER_PROFILE == "codex-exec-restricted-source/v24"
    seal = q.historical_profile_seal(old)
    assert seal["profile_digest"] == "3b0d24b37fc122841de3c5a866f6957e5d2ae24131933cdc2b69229da97b950c"
    assert seal["proof_sha256"] == "827154f7be537dbf580ed3e0a8f62b4e6fa5d2ba43e528931479eed0259b63e8"
    with pytest.raises(ValueError, match="qualified"):
        q.require_profile(old)
    with pytest.raises(ValueError, match="registered provenance"):
        q.historical_profile_seal(replace(old, loader_profile=q.LOADER_PROFILE))
