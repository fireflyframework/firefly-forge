# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""A new disabled continuation flag cannot reinterpret stored policy digests."""

from firefly_forge.configuration import ConfigurationStore
from firefly_forge.invalid_read_lease import ContinuityLeasePolicyV1, policy_snapshot, write_policy
from firefly_forge.journal import digest


def test_absent_disabled_final_flag_preserves_exact_legacy_policy_projection(tmp_path):
    private = ConfigurationStore(tmp_path)
    settings = dict(
        version=1,
        enabled=True,
        allow_unavailable_source_observations=False,
        allow_pre_admission_recovery=False,
        allow_readonly_observation_recovery=False,
        allow_checkpoint_continuation=True,
        max_window_seconds=60,
        max_tickets_per_run=50,
        max_pending_tickets=500,
    )
    path = private.directory / "invalid-read-continuity-policy.json"
    private._write(path, {"revision": "a" * 32, "settings": settings})
    raw = path.read_bytes()
    expected = {"version": 1, "revision": "a" * 32, "settings": settings}
    assert policy_snapshot(private) == {**expected, "digest": digest(expected)}
    assert path.read_bytes() == raw


def test_final_flag_requires_explicit_new_policy_and_disabling_records_new_revision(tmp_path):
    private = ConfigurationStore(tmp_path)
    old = write_policy(private, ContinuityLeasePolicyV1(version=1, enabled=True))
    assert "allow_final_outcome_continuation" not in old["settings"]
    enabled = write_policy(
        private, ContinuityLeasePolicyV1(version=1, enabled=True, allow_final_outcome_continuation=True)
    )
    assert enabled["revision"] != old["revision"] and enabled["settings"]["allow_final_outcome_continuation"] is True
    disabled = write_policy(
        private, ContinuityLeasePolicyV1(version=1, enabled=True, allow_final_outcome_continuation=False)
    )
    assert (
        disabled["revision"] != enabled["revision"] and "allow_final_outcome_continuation" not in disabled["settings"]
    )
