# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Effective safety modes cannot replace the frozen provider, credentials or budgets."""

import copy
from types import SimpleNamespace

import pytest


def case():
    old = {
        "FORGE_BRIDGE_MODEL": "controlled/model",
        "FORGE_BRIDGE_URL": "https://example.test/v1",
        "FORGE_BRIDGE_TOKEN": "synthetic",
        "FORGE_PROVIDER_TIMEOUT": "1800",
    }
    new = old | {"FORGE_AGENTIC_CHECKPOINTS": "required", "FORGE_BRIDGE_COMPLETION_RECOVERY": "required"}
    run = {
        "id": "1" * 32,
        "config_revision": "2" * 32,
        "checkpoint_mode_adoption": {
            "request_id": "3" * 32,
            "record_digest": "a" * 64,
            "original_config_revision": "2" * 32,
            "execution_config_revision": "4" * 32,
            "policy_digest": "b" * 64,
        },
    }
    config = SimpleNamespace(
        load_snapshot=lambda revision: copy.deepcopy(old if revision == "2" * 32 else new),
        resolve=lambda agent: copy.deepcopy(new),
    )
    return run, config, old, new


def test_effective_revision_preserves_original_revision_and_exact_provider_settings():
    from firefly_forge.checkpoint_mode_runtime import effective_revision, execution_settings

    run, config, old, new = case()
    before = copy.deepcopy(run)
    assert effective_revision(run, "config_revision") == "4" * 32
    assert effective_revision(run, "review_config_revision") is None
    assert execution_settings(config, run) == new and config.load_snapshot(run["config_revision"]) == old
    assert run == before


@pytest.mark.parametrize(
    "field", ["FORGE_BRIDGE_MODEL", "FORGE_BRIDGE_URL", "FORGE_BRIDGE_TOKEN", "FORGE_PROVIDER_TIMEOUT", "UNREVIEWED"]
)
def test_effective_snapshot_cannot_change_any_other_setting(field):
    from firefly_forge.checkpoint_mode_runtime import execution_settings

    run, config, old, new = case()
    new[field] = "changed"
    with pytest.raises(ValueError):
        execution_settings(config, run)


def test_current_provider_drift_is_not_covered_by_mode_adoption():
    from firefly_forge.checkpoint_mode_runtime import execution_settings

    run, config, old, new = case()
    config.resolve = lambda agent: old
    with pytest.raises(ValueError):
        execution_settings(config, run)


def test_mode_reference_cannot_rewrite_the_original_revision():
    from firefly_forge.checkpoint_mode_runtime import effective_revision

    run, config, old, new = case()
    run["config_revision"] = "5" * 32
    with pytest.raises(ValueError):
        effective_revision(run, "config_revision")
