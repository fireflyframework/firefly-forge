# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""The catalog-fence fix requires distinct measured V16 admission authority."""

import pytest

from firefly_forge import source_edit_qualification as q


def test_v16_preserves_eleven_historical_profiles_without_current_authority():
    assert q.REGISTRY_VERSION > 14
    assert q.LOADER_PROFILE != "codex-exec-restricted-source/v15"
    profiles = q._profiles()
    assert len(profiles) >= 11
    old = profiles[10]
    assert (old.version, old.loader_profile, old.profile_id) == (
        14,
        "codex-exec-restricted-source/v15",
        "codex-0163-darwin-arm64-source-v15",
    )
    seal = q.historical_profile_seal(old)
    assert seal["profile_digest"] == "b2ed1ac273f0382949686741b3c472a03b19e0097c3524f3615a1e7c464d534e"
    assert seal["proof_sha256"] == "8f85fbae62a26890e49265e878693ca5a8b73927aea96761cac137d4196e01cd"
    for profile in profiles[:11]:
        with pytest.raises(ValueError, match="qualified"):
            q.require_profile(profile)
