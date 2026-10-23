# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Actual PG/Git/Journal handoff; the SCM verifier and official CLI are public fixtures."""

from uuid import UUID

import pytest
from test_source_edit_native import activation, prepared
from worker_reply_fixtures import authenticated_operator

from firefly_forge.identity import Principal
from firefly_forge.identity_persistence import ForgeIdentityUserRepository
from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied
from firefly_forge.scm_source_edit_models import SealedSourceEditAdmission


@pytest.mark.parametrize("outcome", ["completed", "revoked", "unknown"])
async def test_native_scm_source_handoff_preserves_sealed_actor_same_task_and_unknown_custody(
    store, tmp_path, monkeypatch, outcome
):
    async with authenticated_operator(store) as operator:
        env = await prepared(store, tmp_path, monkeypatch)
        candidate = await env.service.source_candidate(env.run_id)
        body = activation(candidate)
        caller = Principal(
            operator.id,
            operator.issuer,
            operator.subject,
            authentication_method="scm_mapping",
            scm_delegation={"transport": "github_rest_poll_source_edit_v1"},
        )
        evidence = dict(
            version=1,
            transport="github_rest_poll_source_edit_v1",
            receipt_id="a" * 32,
            grant_digest="b" * 64,
            binding_digest="c" * 64,
        )
        checks = []

        async def verify():
            assert CURRENT_PRINCIPAL.get() == caller
            checks.append(True)

        admission = SealedSourceEditAdmission(
            run_id=env.run_id,
            native_actor=candidate["actor"],
            caller=caller,
            request=body,
            evidence=evidence,
            verify=verify,
        )
        before = await store.get_run(env.run_id)
        old_index = (env.workspace / ".git/index").read_bytes()
        if outcome == "revoked":
            admit = store.admit_credentials

            async def revoke(value):
                await admit(value)
                async with store._unit():
                    user = await ForgeIdentityUserRepository().find_by_id(operator.id)
                    user.enabled = False
                    await ForgeIdentityUserRepository().save(user)

            monkeypatch.setattr(store, "admit_credentials", revoke)
        elif outcome == "unknown":
            import firefly_forge.workspace_reconciliations as module

            def interrupted(*args):
                raise OSError("Controlled source effect result lost")

            monkeypatch.setattr(module, "apply_dependency_plan", interrupted)
        token = CURRENT_PRINCIPAL.set(caller)
        try:
            if outcome == "completed":
                result = await env.service.activate_source_edit_from_scm(admission)
                assert result["actor"] == candidate["actor"]
                assert result["scm_source_activation"] == evidence
                assert CURRENT_PRINCIPAL.get() == caller
                from dataclasses import replace

                different = replace(admission, evidence={**evidence, "receipt_id": "d" * 32})
                with pytest.raises(AccessDenied, match="provenance|trigger|SCM"):
                    await env.service.activate_source_edit_from_scm(different)
            else:
                with pytest.raises(AccessDenied if outcome == "revoked" else OSError):
                    await env.service.activate_source_edit_from_scm(admission)
        finally:
            CURRENT_PRINCIPAL.reset(token)
        after = await store.get_run(env.run_id)
        for name in ("attempt", "implementation_rounds", "sandbox_snapshot", "commands", "execution_actor"):
            assert after.get(name) == before.get(name)
        async with store._unit():
            rows = await env.journal.records.find_for_run(UUID(env.run_id))
        intents = [r for r in rows if r.kind == "workspace_reconciliation_intent"]
        receipts = [r for r in rows if r.kind == "workspace_reconciliation_receipt"]
        if outcome == "completed":
            assert len(intents) == len(receipts) == 1
            assert after["status"] == "queued" and len(checks) >= 4
            assert receipts[0].data["payload"]["scm_source_activation"] == evidence
        elif outcome == "revoked":
            assert not intents and not receipts
            assert after["status"] == "failed"
            assert (env.workspace / ".git/index").read_bytes() == old_index
        else:
            assert len(intents) == 1 and not receipts
            assert after["status"] == "failed"
            with pytest.raises(ValueError, match="unknown"):
                await env.service.activate_source_edit(env.run_id, body)
