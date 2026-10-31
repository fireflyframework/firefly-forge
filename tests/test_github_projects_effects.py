# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
import pytest

from firefly_forge.github_projects_effects import decide_update, managed_value


def test_unrelated_fields_are_preserved_and_human_drift_refuses():
    item = {
        "id": "item",
        "fieldValues": {
            "nodes": [
                {"field": {"id": "human-field"}, "optionId": "human-value"},
                {"field": {"id": "forge-field"}, "optionId": "running"},
            ]
        },
    }
    assert managed_value(item, "forge-field") == "running"
    assert decide_update("running", "running", "paused") == "update"
    assert decide_update("running", "paused", "paused") == "conflict"
    assert decide_update("running", None, "paused") == "conflict"
    assert decide_update(None, None, "paused") == "update"
    assert decide_update("paused", None, "paused") == "unchanged"


def test_duplicate_selected_field_is_incomplete_evidence():
    item = {
        "fieldValues": {
            "nodes": [
                {"field": {"id": "forge-field"}, "optionId": "running"},
                {"field": {"id": "forge-field"}, "optionId": "paused"},
            ]
        }
    }
    with pytest.raises(ValueError):
        managed_value(item, "forge-field")
