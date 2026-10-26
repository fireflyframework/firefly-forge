# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""The catalog-fence fix requires distinct measured V15 admission authority."""

import pytest

from firefly_forge import source_edit_qualification as q


def test_v15_keeps_ten_profiles_as_provenance_before_measured_append():
    assert q.REGISTRY_VERSION > 13
    assert q.LOADER_PROFILE != "codex-exec-restricted-source/v14"
    profiles = q._profiles()
    assert len(profiles) >= 10
    old = profiles[9]
    assert (old.version, old.loader_profile, old.profile_id) == (
        13,
        "codex-exec-restricted-source/v14",
        "codex-0163-darwin-arm64-source-v14",
    )
    seal = q.historical_profile_seal(old)
    assert seal["profile_digest"] == "b4068930fabf4eee4bc22f1bd3f5846cce78318e5b7b3cc51334bdc780492b24"
    assert seal["proof_sha256"] == "8707395cab0da41bea21939a7e524c19cf36917de3b030117af0a5c7ce24eb65"
    for profile in profiles[:10]:
        with pytest.raises(ValueError, match="qualified"):
            q.require_profile(profile)
