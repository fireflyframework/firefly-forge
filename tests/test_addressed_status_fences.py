# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Faults at native status custody boundaries must not become a remote reply."""

import pytest
from test_addressed_status_polling import addressed, configured_status
from test_addressed_status_signed import grant, signed_address
from test_scm_polling import poll_case as poll_case


@pytest.mark.parametrize("transport", ["poll", "signed"])
@pytest.mark.parametrize("change", ["source", "membership"])
async def test_status_rechecks_source_and_membership_after_issue_token_wait(poll_case, monkeypatch, transport, change):
    async with poll_case() as c:
        if transport == "poll":
            await c.ready()
            await c.consent()
            await configured_status(c, monkeypatch)
            delivery = await addressed(c)
        else:
            delivery, comment = await signed_address(c)
            c.state["comments"].append(comment)
            await grant(c, delivery)
        prepare = c.scm.issue_authentication

        async def token_then_change(*args, **kwargs):
            value = await prepare(*args, **kwargs)
            if change == "source":
                c.state["edited"] = True
            else:
                c.db["memberships"]["member"].active = False
            return value

        c.scm.issue_authentication = token_then_change
        result = await c.collaboration.process(delivery["id"])
        assert result["action_status"] == "completed"
        assert result["feedback_status"] == "needs_reconciliation"
        assert not c.state["posts"] and not c.db["journal"]
        await c.collaboration.sync_feedback()
        assert not c.state["posts"]


@pytest.mark.parametrize("change", ["source_conflict", "binding_digest", "principal_missing", "foreign_issue"])
async def test_invalid_historical_receipt_cannot_supply_ticket_context(poll_case, monkeypatch, change):
    async with poll_case() as c:
        await c.ready()
        await c.consent()
        previous = await c.new_command("status")
        await c.collaboration.process(previous["id"])
        row = c.db["inbox"][previous["id"]]
        if change == "source_conflict":
            row.data["source_conflict"] = True
        elif change == "binding_digest":
            row.data["scm_execution_actor"]["scm_delegation"]["binding_digest"] = "0" * 64
        elif change == "principal_missing":
            del row.data["scm_execution_actor"]
        else:
            row.data["issue"]["number"] = 4
        await configured_status(c, monkeypatch)
        delivery = await addressed(c)
        result = await c.collaboration.process(delivery["id"])
        assert result["status"] == "processed" and result["run_id"] is None
        assert result["conversation"]["selection"] == "missing"
        assert len(c.state["posts"]) == 2 and not c.db["journal"]


@pytest.mark.parametrize("transport", ["poll", "signed"])
@pytest.mark.parametrize("change", ["digest", "scope", "selection"])
async def test_status_rejects_retained_context_tampering_during_issue_token_wait(
    poll_case, monkeypatch, transport, change
):
    async with poll_case() as c:
        if transport == "poll":
            await c.ready()
            await c.consent()
            await configured_status(c, monkeypatch)
            delivery = await addressed(c)
        else:
            delivery, comment = await signed_address(c)
            c.state["comments"].append(comment)
            await grant(c, delivery)
        prepare = c.scm.issue_authentication

        async def token_then_tamper(*args, **kwargs):
            value = await prepare(*args, **kwargs)
            retained = c.db["inbox"][delivery["id"]].data
            if transport == "poll":
                turn = retained["natural_turn"]
                if change == "digest":
                    turn["policy_digest"] = "0" * 64
                elif change == "scope":
                    turn["context"]["scope"]["binding_digest"] = "0" * 64
                else:
                    turn["context"]["status_context"]["selection"] = "ambiguous"
            else:
                context = retained["conversation"]
                if change == "digest":
                    context["digest"] = "0" * 64
                elif change == "scope":
                    context["scopes"][0]["component_ids"] = ["other"]
                else:
                    context["selection"] = "clarify"
            return value

        c.scm.issue_authentication = token_then_tamper
        result = await c.collaboration.process(delivery["id"])
        assert result["action_status"] == "completed"
        assert result["feedback_status"] == "needs_reconciliation"
        assert not c.state["posts"] and not c.db["journal"]
        await c.collaboration.sync_feedback()
        assert not c.state["posts"]
