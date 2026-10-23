# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Operator recovery reads the same public-message receipt without reposting or a model."""

import importlib
import importlib.util

import pytest
from test_scm_natural_admission_host import question
from test_scm_natural_intake_host import prepared
from test_scm_polling import poll_case  # noqa: F401

from firefly_forge.project_access import AccessDenied
from firefly_forge.scm_natural_effects import NaturalReadmission


async def test_exact_retained_answer_recovery_is_scoped_and_repeated_admission_has_no_new_effect(
    poll_case,  # noqa: F811
    monkeypatch,  # noqa: F811
):  # noqa: F811
    assert importlib.util.find_spec("firefly_forge.scm_natural_controllers"), "Natural recovery API is missing"
    module = importlib.import_module("firefly_forge.scm_natural_controllers")
    async with poll_case() as c:
        service, receipt = await prepared(c, monkeypatch)
        await question(c)
        await service.process(receipt)
        controller = module.SCMNaturalController(c.collaboration)
        with pytest.raises(AccessDenied):
            await controller.read("0" * 32, c.repository, receipt)
        with pytest.raises(AccessDenied):
            await controller.read(c.project, "0" * 32, receipt)
        retained = await controller.read(c.project, c.repository, receipt)
        cut = await controller.review(c.project, c.repository, receipt)
        request = NaturalReadmission(
            request_id="6" * 32, expected_intake_digest=cut["intake_digest"], expected_cut_digest=cut["digest"]
        )
        result = await controller.admit(c.project, c.repository, receipt, request)
        assert result["natural_turn"]["human_message"] == retained["natural_turn"]["human_message"]
        assert (await controller.admit(c.project, c.repository, receipt, request))["id"] == receipt
        original = await controller.admission(c.project, c.repository, receipt, request.request_id)
        assert original["request"] == request.model_dump(mode="json")
        assert len([r for r in c.db["journal"].values() if r.kind == "worker_response"]) == 1
        assert c.state["natural_calls"] == 1
