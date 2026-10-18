# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import pytest
from pydantic import ValidationError

from firefly_forge.journal import digest
from firefly_forge.tool_broker import FactoryToolPolicyInput as ToolPolicyInput


def test_new_tool_policy_explicitly_grants_bounded_automatic_verification():
    policy = ToolPolicyInput().model_dump()
    assert policy.get("automatic_verification") == {"enabled": True, "max_continuations": 1}


@pytest.mark.parametrize("limit", [True, 0, 4, "2"])
def test_automatic_verification_limit_is_strict_and_bounded(limit):
    with pytest.raises(ValidationError):
        ToolPolicyInput.model_validate({"automatic_verification": {"enabled": True, "max_continuations": limit}})


def test_disabled_automatic_verification_is_explicit():
    policy = ToolPolicyInput.model_validate({"automatic_verification": {"enabled": False, "max_continuations": 3}})
    assert policy.model_dump()["automatic_verification"] == {"enabled": False, "max_continuations": 3}


def test_legacy_frozen_tool_policy_is_not_an_automatic_grant():
    from firefly_forge.verification_models import automatic_verification_grant

    payload = {"version": 1, "revision": None, "settings": {"calls_limit": 30}}
    assert automatic_verification_grant({**payload, "digest": digest(payload)}) is None


def test_changed_policy_digest_cannot_grant_automatic_execution():
    from firefly_forge.verification_models import automatic_verification_grant

    payload = {"version": 1, "revision": None, "settings": ToolPolicyInput().model_dump()}
    snapshot = {**payload, "digest": digest(payload)}
    assert automatic_verification_grant(snapshot).max_continuations == 1
    snapshot["settings"]["automatic_verification"]["max_continuations"] = 3
    with pytest.raises(ValueError, match="changed"):
        automatic_verification_grant(snapshot)


def test_legacy_policy_update_preserves_explicitly_disabled_automatic_grant(tmp_path):
    from types import SimpleNamespace

    from firefly_forge.tool_broker import ToolInvocationBroker
    from firefly_forge.tool_broker import ToolPolicyInput as LegacyToolPolicyInput

    broker = ToolInvocationBroker(SimpleNamespace(), SimpleNamespace(), tmp_path / "private")
    broker.configure(ToolPolicyInput(automatic_verification={"enabled": False, "max_continuations": 2}))
    updated = broker.configure(LegacyToolPolicyInput(calls_limit=40))
    assert updated["settings"]["calls_limit"] == 40
    assert updated["settings"]["automatic_verification"] == {"enabled": False, "max_continuations": 2}
