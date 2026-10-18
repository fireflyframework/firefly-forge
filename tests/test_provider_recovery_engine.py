# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Exercise the real Engine terminal-error boundary without Git, processes or a database."""

import pytest

from firefly_forge.engine import Engine
from firefly_forge.provider_recovery_models import ProviderAttemptClosed


@pytest.mark.parametrize("closed", [False, True])
async def test_late_closed_provider_attempt_never_finishes_its_successor(tmp_path, closed):
    class Store:
        path = tmp_path / "native-store"
        runtime_roles = None

        def __init__(self):
            self.reads = 0
            self.finishes = []

        async def claim(self, identifier):
            return True

        async def get_run(self, identifier):
            self.reads += 1
            if self.reads == 1:
                if closed:
                    raise ProviderAttemptClosed("A completed old attempt cannot replace current evidence")
                raise ValueError("Current attempt failed")
            return {"status": "failed"}

        async def finish(self, identifier, status, **kwargs):
            self.finishes.append(status)

    store = Store()
    engine = Engine(store, tmp_path / "workspaces", provider_factory=lambda _: None)
    await engine.execute_run("a" * 32)
    assert store.finishes == ([] if closed else ["failed"])
