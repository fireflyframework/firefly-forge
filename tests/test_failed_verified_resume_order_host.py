# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""An intact hash chain cannot move a dispatched coding grant before its native resume."""

import copy

import pytest
from test_checkpoint_mode_handoff_custody import rehash

from firefly_forge.checkpoint_mode_handoff_custody import validate_completed_handoff

pytest_plugins = ["test_checkpoint_mode_handoff_custody"]


@pytest.mark.parametrize("kind", ["intent", "verification_continuation_intent"])
async def test_predispatch_custody_refuses_reordered_future_coding_records(completed, kind):
    c = completed
    resume_index = next(i for i, e in enumerate(c.entries) if e["kind"] == "handoff")
    prefix = copy.deepcopy(c.entries[: resume_index + 1])
    assert validate_completed_handoff(c.run, prefix, c.rows, c.values, before_dispatch=True)
    future = copy.deepcopy(next(e for e in c.entries[resume_index + 1 :] if e["kind"] == kind))
    prefix.insert(-1, future)
    rehash(prefix)
    with pytest.raises(ValueError, match="Canonical completed verification"):
        validate_completed_handoff(c.run, prefix, c.rows, c.values, before_dispatch=True)
