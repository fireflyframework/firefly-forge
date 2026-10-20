# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""The review must reread current grant and execution state after admission waits."""

import copy
from types import SimpleNamespace

import pytest
from test_scm_poll_resume_contract import polling_grant

from firefly_forge.project_access import AccessDenied
from firefly_forge.scm_poll_resume import SCMPollResumeAuthority


async def test_read_review_withholds_a_grant_revoked_during_credential_admission():
    project, repository, run_id = (value * 32 for value in "abc")
    request = polling_grant()
    grant = {"digest": "d" * 64, "request": request, "native_user_id": "e" * 32}
    binding = {"binding_key": "f" * 64}

    async def reader(*args, **kwargs):
        pass

    async def admit(value):
        grant["request"] = {**request, "active": False}
        grant["digest"] = "0" * 64

    class Harness(SCMPollResumeAuthority):
        async def _current_binding(self, *args):
            return copy.deepcopy(binding)

        async def _grant(self, *args, **kwargs):
            return copy.deepcopy(grant)

        async def _binding(self, *args):
            return copy.deepcopy(binding)

        async def _run_data(self, *args):
            return {"id": run_id, "project_id": project, "repository_id": repository, "component_id": "api"}

        async def _original(self, *args):
            pass

        async def _preflight(self, *args):
            pass

        async def _cut(self, *args):
            return {"digest": "1" * 64}

    store = SimpleNamespace(admit_credentials=admit, reject_credentials=lambda value: None)
    service = Harness(store, SimpleNamespace(_reader=reader), None, None, None, None)
    with pytest.raises(AccessDenied, match="changed"):
        await service.review(project, repository, request["expected_binding_request_id"], run_id)
