# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Only a current native administrator can inspect or configure finite source allowances."""

# ruff: noqa: F401,F811
import pytest
from test_source_budget_service_host import (
    answered_four,
    controlled_metadata,
    environment_journal,
    legacy_workspace,
    reconciliation_service,
)

from firefly_forge import source_budget_allowances as module
from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied


async def test_policy_service_requires_current_native_admin_and_preserves_default_deny(
    reconciliation_service, tmp_path, monkeypatch
):
    env, parent, path, _ = await answered_four(reconciliation_service, tmp_path, monkeypatch)
    policies = module.SourceBudgetPolicies(env.store)
    assert (await policies.get())["settings"]["enabled"] is True
    written = await policies.configure(module.SourceBudgetPolicy())
    assert written["settings"] == module.SourceBudgetPolicy().model_dump()
    before = module.policy_snapshot(env.store.configuration)
    env.identity.enabled = False
    with pytest.raises(AccessDenied):
        await policies.configure(
            module.SourceBudgetPolicy(
                enabled=True, max_read_file_lifetime_limit=20, max_activation_ceiling=5, max_additional_activations=1
            )
        )
    assert module.policy_snapshot(env.store.configuration) == before
    token = CURRENT_PRINCIPAL.set(None)
    try:
        with pytest.raises(AccessDenied):
            await policies.get()
    finally:
        CURRENT_PRINCIPAL.reset(token)


def test_public_controllers_expose_separate_typed_budget_paths():
    from firefly_forge.source_budget_controllers import SourceBudgetPolicyController
    from firefly_forge.workspace_reconciliation_controllers import SourceEditContinuationController

    assert hasattr(SourceBudgetPolicyController, "configure")
    assert {"budget_candidate", "admit_budget", "budget_receipt"} <= set(vars(SourceEditContinuationController))
