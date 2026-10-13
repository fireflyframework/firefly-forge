# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
import asyncio

import pytest

from firefly_forge.models import RepositoryInput
from firefly_forge.product_assistant import AssistantSessionInput, AssistantTurnInput, ProductAssistant
from firefly_forge.product_assistant_runtime import AssistantReply
from firefly_forge.providers import ProviderLimitError
from firefly_forge.store import identifier


async def test_status_recovery_preserves_a_concurrently_completed_model_receipt(store, tmp_path, monkeypatch):
    repo = await store.add_repository(RepositoryInput(name="Product", url=str(tmp_path)))
    entered, release = asyncio.Event(), asyncio.Event()

    async def designer(prompt, settings):
        entered.set()
        await release.wait()
        return AssistantReply(message="Completed customer proposal")

    manager = ProductAssistant(store, tmp_path / "assistant", designer=designer)
    session = await manager.create(repo["id"], AssistantSessionInput())
    task = asyncio.create_task(
        manager.turn(
            repo["id"],
            session["id"],
            AssistantTurnInput(request_id="3" * 32, message="Define customer"),
        )
    )
    await entered.wait()
    read = manager.events.events
    triggered = False

    async def delayed_events(session_id):
        nonlocal triggered
        records = await read(session_id)
        if not triggered:
            triggered = True
            release.set()
            assert (await task)["status"] == "completed"
        return records

    monkeypatch.setattr(manager.events, "events", delayed_events)
    observed = await manager.session(repo["id"], session["id"])
    assert observed["turns"][0]["status"] == "completed"
    assert observed["turns"][0]["reply"]["message"] == "Completed customer proposal"
    assert observed["journal"]["verified"]


async def test_assistant_persists_intent_and_scoped_history_without_applying_proposals(store, tmp_path):
    first = await store.add_repository(RepositoryInput(name="Inventory", url=str(tmp_path / "inventory")))
    other = await store.add_repository(RepositoryInput(name="Other", url=str(tmp_path / "other")))
    calls = []

    async def designer(prompt, settings):
        calls.append(prompt)
        sessions = await manager.sessions(first["id"])
        recorded = await manager.session(first["id"], sessions[0]["id"])
        assert recorded["turns"][0]["status"] == "processing"
        return AssistantReply(message="Who can reserve stock?", questions=["Who owns approval for a reservation?"])

    manager = ProductAssistant(store, tmp_path / "assistant", designer=designer)
    session = await manager.create(first["id"], AssistantSessionInput(agent="bridge"))
    request = AssistantTurnInput(request_id="a" * 32, message="Help define the inventory product")
    result = await manager.turn(first["id"], session["id"], request)
    assert result["status"] == "completed" and result["reply"]["questions"]
    assert await manager.turn(first["id"], session["id"], request) == result
    assert len(calls) == 1 and "PRODUCT DESIGN" in calls[0]
    assert "Write actual source" not in calls[0]
    async with store._unit():
        assert await store.product_records.find_current(identifier(first["id"])) is None
    with pytest.raises(KeyError):
        await manager.session(other["id"], session["id"])
    with pytest.raises(ValueError, match="different"):
        await manager.turn(first["id"], session["id"], request.model_copy(update={"message": "Different message"}))


async def test_assistant_rate_limit_is_retained_without_blind_resubmission(store, tmp_path):
    repo = await store.add_repository(RepositoryInput(name="Inventory", url=str(tmp_path)))
    calls = 0

    async def limited(prompt, settings):
        nonlocal calls
        calls += 1
        raise ProviderLimitError("No remaining credits", kind="quota")

    manager = ProductAssistant(store, tmp_path / "assistant", designer=limited)
    session = await manager.create(repo["id"], AssistantSessionInput())
    request = AssistantTurnInput(request_id="b" * 32, message="Design inventory journeys")
    result = await manager.turn(repo["id"], session["id"], request)
    assert result["status"] == "needs_attention" and result["failure_kind"] == "quota"
    assert await manager.turn(repo["id"], session["id"], request) == result
    assert calls == 1


async def test_assistant_rejects_known_secrets_before_wal_or_model_admission(store, tmp_path, monkeypatch):
    monkeypatch.setenv("FORGE_TOKEN", "dummy-private-token-never-admit")
    repo = await store.add_repository(RepositoryInput(name="Inventory", url=str(tmp_path)))
    calls = []

    async def designer(prompt, settings):
        calls.append(prompt)
        return AssistantReply(message="Not called")

    manager = ProductAssistant(store, tmp_path / "assistant", designer=designer)
    session = await manager.create(repo["id"], AssistantSessionInput())
    with pytest.raises(ValueError, match="credential"):
        await manager.turn(
            repo["id"],
            session["id"],
            AssistantTurnInput(request_id="e" * 32, message="Use dummy-private-token-never-admit"),
        )
    saved = await manager.session(repo["id"], session["id"])
    assert not calls and saved["turns"] == []
    assert "dummy-private-token" not in str(saved)


async def test_json_escaped_credentials_cannot_cross_the_dialogue_boundary(store, tmp_path, monkeypatch):
    import json

    secret = 'dummy-quoted-"credential"\nprivate-line'
    monkeypatch.setenv("FORGE_TOKEN", secret)
    repo = await store.add_repository(RepositoryInput(name="Inventory", url=str(tmp_path)))
    manager = ProductAssistant(store, tmp_path / "assistant")
    session = await manager.create(repo["id"], AssistantSessionInput())
    with pytest.raises(ValueError, match="credential"):
        await manager.turn(
            repo["id"],
            session["id"],
            AssistantTurnInput(request_id="f" * 32, message="My configuration: " + json.dumps({"key": secret})),
        )
    assert (await manager.session(repo["id"], session["id"]))["turns"] == []


async def test_assistant_native_agent_has_structured_output_and_no_execution_tools():
    from pydantic_ai.models.test import TestModel

    from firefly_forge.product_assistant_runtime import design_reply

    reply = await design_reply(
        "Ask who owns stock reservations",
        {},
        model=TestModel(
            custom_output_args={
                "message": "Who manages stock?",
                "questions": ["Who approves a reservation?"],
                "assumptions": [],
                "proposal": None,
                "citation_ids": [],
            }
        ),
    )
    assert reply.questions == ["Who approves a reservation?"]
    assert reply.proposal is None


async def test_bridge_dialogue_uses_prompted_output_with_full_local_validation():
    import json

    from pydantic_ai.messages import ModelResponse, TextPart
    from pydantic_ai.models.function import FunctionModel

    from firefly_forge.product_assistant_runtime import design_reply

    observed = []

    def model(messages, info):
        observed.append(info)
        assert info.output_tools == []
        return ModelResponse(
            [
                TextPart(
                    json.dumps(
                        {
                            "message": "" if len(observed) == 1 else "Define the stock approval journey",
                            "questions": [],
                            "assumptions": [],
                            "proposal": None,
                            "citation_ids": [],
                        }
                    )
                )
            ]
        )

    reply = await design_reply("Help define stock", {"ASSISTANT_PROVIDER": "bridge"}, model=FunctionModel(model))
    assert reply.message == "Define the stock approval journey" and len(observed) == 2


async def test_concurrent_design_turns_cannot_overlap_in_one_conversation(store, tmp_path):
    import asyncio

    repo = await store.add_repository(RepositoryInput(name="Inventory", url=str(tmp_path)))
    entered, release = asyncio.Event(), asyncio.Event()
    both_read = asyncio.Barrier(2)
    calls = 0

    class ConcurrentAssistant(ProductAssistant):
        async def session(self, repository_id, session_id):
            value = await super().session(repository_id, session_id)
            await both_read.wait()
            return value

    async def designer(prompt, settings):
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        return AssistantReply(message="Define stock ownership first")

    manager = ConcurrentAssistant(store, tmp_path / "assistant", designer=designer)
    session = await manager.create(repo["id"], AssistantSessionInput())
    running = asyncio.create_task(
        manager.turn(repo["id"], session["id"], AssistantTurnInput(request_id="c" * 32, message="First turn"))
    )
    second = asyncio.create_task(
        manager.turn(repo["id"], session["id"], AssistantTurnInput(request_id="d" * 32, message="Second turn"))
    )
    await entered.wait()
    release.set()
    outcomes = await asyncio.gather(running, second, return_exceptions=True)
    assert calls == 1
    assert sum(isinstance(outcome, ValueError) for outcome in outcomes) == 1


async def test_model_cannot_assign_its_own_provenance_receipt(store, tmp_path):
    from firefly_forge.collaboration import ProductDefinition

    repo = await store.add_repository(RepositoryInput(name="Product", url=str(tmp_path)))

    async def designer(prompt, settings):
        return AssistantReply(
            message="Proposed product",
            proposal=ProductDefinition(
                customer="Warehouse team",
                problem="Prevent overselling",
                journey=["Reserve stock"],
                user_stories=["Reserve an available item"],
                acceptance=["No negative stock"],
                owners=["Product team"],
                ai_origin={"conversation_id": "a" * 32, "turn_id": "b" * 32, "proposal_digest": "c" * 64},
            ),
        )

    assistant = ProductAssistant(store, tmp_path / "assistant-origin", designer=designer)
    session = await assistant.create(repo["id"], AssistantSessionInput())
    turn = await assistant.turn(
        repo["id"], session["id"], AssistantTurnInput(request_id="f" * 32, message="Define reservations")
    )
    assert turn["status"] == "failed"
    assert turn["reply"] is None
    assert "provenance" in turn["detail"].lower()
