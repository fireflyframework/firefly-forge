# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Native handoff projections preserve JSON types against their immutable producers."""

import pytest
from test_checkpoint_mode_handoff_custody import completed as completed
from test_checkpoint_mode_handoff_custody import validate
from test_sandbox_command_ordinals_host import command_case as command_case


@pytest.mark.parametrize("projection", ["response_actor_version", "state_version"])
async def test_completed_handoff_rejects_numeric_type_drift_in_raw_projection(completed, projection):
    c = completed
    validate(c)
    if projection == "response_actor_version":
        actor = c.run["worker_response"]["responding_actor"]
        actor["version"] = float(actor["version"])
    else:
        state = c.run["verification_handoff"]
        state["version"] = float(state["version"])
    with pytest.raises(ValueError):
        validate(c)
