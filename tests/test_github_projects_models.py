# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Board configuration carries identifiers and consent, never provider credentials."""

import copy

import pytest
from pydantic import ValidationError

from firefly_forge.github_projects_models import STATES, BoardConnectionInputV1, BoardSyncInputV1


def connection() -> dict:
    return {
        "version": 1,
        "request_id": "1" * 32,
        "expected_digest": None,
        "connection_name": "github-board",
        "owner_kind": "user",
        "owner": "octocat",
        "number": 2,
        "board_id": "PVT_123",
        "field_id": "PVTSSF_123",
        "state_options": dict.fromkeys(STATES, "todo"),
        "enabled": True,
        "include_existing": False,
        "acknowledge_managed_field": True,
    }


def test_shared_options_are_supported_without_inventing_statuses() -> None:
    assert BoardConnectionInputV1.model_validate(connection()).state_options == dict.fromkeys(STATES, "todo")


@pytest.mark.parametrize(
    "change",
    [
        {"token": "secret"},
        {"acknowledge_managed_field": False},
        {"acknowledge_managed_field": 1},
        {"owner": "https://github.com/octocat"},
        {"number": -1},
        {"expected_digest": "stale"},
        {"state_options": {"succeeded": "done"}},
        {"state_options": {**dict.fromkeys(STATES, "todo"), "done": "done"}},
    ],
)
def test_invalid_or_secret_bearing_config_is_rejected(change: dict) -> None:
    body = copy.deepcopy(connection())
    body.update(change)
    with pytest.raises(ValidationError):
        BoardConnectionInputV1.model_validate(body)


def test_sync_cannot_supply_remote_content_or_completion_authority() -> None:
    body = {"version": 1, "request_id": "2" * 32, "expected_binding_digest": "a" * 64}
    BoardSyncInputV1.model_validate(body)
    for key in ("content_id", "status", "product_completion_verified", "token"):
        with pytest.raises(ValidationError):
            BoardSyncInputV1.model_validate({**body, key: True})
