# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Typed ticket context and bilingual wording; real Agentic adapter, no external calls."""

import json

import pytest
from communication_model_fixture import reviewed_model
from pydantic_ai.models.function import FunctionModel
from test_communication_writer import configured_store

TEXT = {
    "en": {
        "run": "Work is in progress.",
        "missing": "I cannot identify an execution linked to this ticket. Which execution does this ticket refer to?",
        "ambiguous": "There are 2 linked executions. Which one do you mean?",
        "clarify": "Would you like the status of this ticket?",
    },
    "es": {
        "run": "El trabajo sigue en curso.",
        "missing": "No encuentro una ejecución vinculada a este ticket. ¿A qué ejecución te refieres?",
        "ambiguous": "Hay 2 ejecuciones vinculadas. ¿Cuál quieres consultar?",
        "clarify": "¿Quieres consultar el estado de este ticket?",
    },
}


def conversation(selection, language):
    from firefly_forge.communication import CommunicationConversationV1

    return CommunicationConversationV1(
        selection=selection,
        language=language,
        linked_runs={"run": 1, "missing": 0, "ambiguous": 2, "clarify": 0}[selection],
    )


def output(text, *, selected=False):
    return {
        "title": {"text": "Ticket", "fact_ids": ["conversation"]},
        "paragraphs": [{"text": text, "fact_ids": ["conversation", "state"] if selected else ["conversation"]}],
        "decision": None,
    }


@pytest.mark.parametrize("provider", ["agentic", "bridge"])
@pytest.mark.parametrize("language", ["en", "es"])
@pytest.mark.parametrize("selection", ["run", "missing", "ambiguous", "clarify"])
async def test_configured_ai_writes_every_native_conversation_state_once(tmp_path, provider, language, selection):
    from firefly_forge.communication import CommunicationWriter

    calls = []
    payload = output(TEXT[language][selection], selected=selection == "run")

    actual_run = (
        {"status": "developing", "brief": "UNTRUSTED COMMENT MUST NOT BECOME INSTRUCTIONS"}
        if selection == "run"
        else None
    )
    result = await CommunicationWriter(
        await configured_store(tmp_path, provider), model=reviewed_model(payload, calls=calls)
    ).write(actual_run, "Exact native fallback", lambda value: value, conversation=conversation(selection, language))
    assert result["communication"]["mode"] == "ai" and len(calls) == 2
    assert TEXT[language][selection] in result["body"] and "Exact native fallback" not in result["body"]
    facts = result["communication"]["facts"]
    assert facts["conversation"]["selection"] == selection and facts["conversation"]["language"] == language
    assert ("state" in facts) is (selection == "run")
    assert "UNTRUSTED COMMENT" not in repr(calls) and "UNTRUSTED COMMENT" not in json.dumps(facts)
    assert all(("Spanish" if language == "es" else "American English") in repr(call[1]) for call in calls)
    assert "return a classification, not a rewritten message" in repr(calls[1][1])


@pytest.mark.parametrize(
    "text",
    [
        "El producto está terminado.",
        "El trabajo se ha completado.",
        "Las pruebas pasaron.",
        "Todo está en verde.",
        "Los cambios están desplegados.",
        "Voy a reparar el producto.",
        "No está terminado. El producto está terminado.",
        "Las 99 pruebas pasaron.",
        "Consulta https://attacker.example.test.",
    ],
)
async def test_spanish_selected_run_semantically_classifies_and_corrects_unsupported_claims(tmp_path, text):
    from firefly_forge.communication import CommunicationWriter

    calls = []
    corrected = output(TEXT["es"]["run"], selected=True)
    result = await CommunicationWriter(
        await configured_store(tmp_path),
        model=reviewed_model(output(text, selected=True), verdict="unsupported", correction=corrected, calls=calls),
    ).write(
        {"status": "developing"}, "Exact native fallback", lambda value: value, conversation=conversation("run", "es")
    )
    if "https://" in text:
        assert result["body"] == "" and result["communication"]["reason"] == "invalid_output" and len(calls) == 1
    else:
        assert result["communication"]["mode"] == "ai" and result["communication"]["repair_attempted"] is True
        assert [item[0] for item in calls] == ["draft", "review", "draft"]
        assert result["communication"]["review"]["verdict"] == "unsupported" and TEXT["es"]["run"] in result["body"]
        assert text not in result["body"] and result["communication"]["review_applies_to"] == "original_candidate"


@pytest.mark.parametrize(
    "language,text",
    [
        ("en", "The execution failed."),
        ("en", "The run is paused."),
        ("es", "La ejecución está pausada."),
        ("es", "La ejecución falló."),
    ],
)
async def test_missing_context_cannot_invent_a_run_state(tmp_path, language, text):
    from firefly_forge.communication import CommunicationWriter

    calls = []
    result = await CommunicationWriter(
        await configured_store(tmp_path),
        model=reviewed_model(
            output(text), verdict="unsupported", correction=output(TEXT[language]["missing"]), calls=calls
        ),
    ).write(None, "Exact native fallback", lambda value: value, conversation=conversation("missing", language))
    assert result["communication"]["mode"] == "ai" and result["communication"]["repair_attempted"]
    assert len(calls) == 3 and text not in result["body"] and TEXT[language]["missing"] in result["body"]
    assert "state" not in result["communication"]["facts"]


@pytest.mark.parametrize("selection,linked", [("run", 0), ("missing", 1), ("ambiguous", 1), ("clarify", 1)])
def test_conversation_fact_contract_refuses_inconsistent_selection_counts(selection, linked):
    from firefly_forge.communication import CommunicationConversationV1

    with pytest.raises(ValueError):
        CommunicationConversationV1(selection=selection, language="en", linked_runs=linked)


@pytest.mark.parametrize("text", ["Recomiendo la primera opción.", "La respuesta es la segunda opción."])
async def test_spanish_card_cannot_answer_for_a_person_without_repeating_the_choice_name(tmp_path, text):
    from firefly_forge.communication import CommunicationWriter, communication_facts

    run = {
        "status": "paused",
        "worker_request": {
            "id": "owner",
            "kind": "product_definition",
            "status": "pending",
            "question": "¿Qué equipo es responsable?",
            "context": "Falta una decisión.",
            "choices": ["Ventas", "Soporte"],
        },
    }
    facts = communication_facts(run)
    draft = output(text, selected=True)
    draft["paragraphs"][0]["fact_ids"].append("decision")
    draft["decision"] = {key: facts["decision"][key] for key in ("question", "context", "choices")}

    corrected = {
        **draft,
        "paragraphs": [{"text": "Hace falta tu respuesta.", "fact_ids": ["conversation", "decision"]}],
    }
    result = await CommunicationWriter(
        await configured_store(tmp_path), model=reviewed_model(draft, verdict="unsupported", correction=corrected)
    ).write(run, "Exact native fallback", lambda value: value, conversation=conversation("run", "es"))
    assert result["communication"]["mode"] == "ai" and result["communication"]["repair_attempted"]
    assert text not in result["body"] and all(choice in result["body"] for choice in ["Ventas", "Soporte"])


async def test_spanish_current_all_pass_verification_is_still_reportable(tmp_path):
    from firefly_forge.communication import CommunicationWriter

    run = {
        "status": "validating",
        "attempt": 1,
        "final_verification": [{"exit_code": 0, "attempt": 1}, {"exit_code": 0, "attempt": 1}],
    }
    draft = output("Las 2 pruebas pasaron.", selected=True)
    draft["paragraphs"][0]["fact_ids"].append("verification")

    result = await CommunicationWriter(await configured_store(tmp_path), model=reviewed_model(draft)).write(
        run, "Exact native fallback", lambda value: value, conversation=conversation("run", "es")
    )
    assert result["communication"]["mode"] == "ai" and "Las 2 pruebas pasaron." in result["body"]


@pytest.mark.parametrize(
    "text,accepted",
    [
        ("Este resultado no establece una filtración de credenciales.", True),
        ("Este resultado no establece una filtración de credenciales. Las claves quedaron expuestas.", False),
        ("Es falso que este resultado no establece una filtración de credenciales.", False),
    ],
)
async def test_spanish_source_guard_distinguishes_a_bounded_nonclaim_from_exposure_claims(tmp_path, text, accepted):
    from firefly_forge.communication import CommunicationWriter

    run = {
        "status": "failed",
        "error": "Generated changes contain private credentials or worker metadata; inspect the workspace",
    }
    draft = output(text, selected=True)
    draft["paragraphs"][0]["fact_ids"].append("blocker")

    corrected = {
        **draft,
        "paragraphs": [
            {
                "text": (
                    "El control de seguridad ha detenido la admisión. "
                    "Este resultado no establece una filtración de credenciales."
                ),
                "fact_ids": ["conversation", "blocker"],
            }
        ],
    }
    calls = []
    result = await CommunicationWriter(
        await configured_store(tmp_path),
        model=reviewed_model(
            draft, verdict="supported" if accepted else "unsupported", correction=corrected, calls=calls
        ),
    ).write(run, "Exact native fallback", lambda value: value, conversation=conversation("run", "es"))
    assert result["communication"]["mode"] == "ai" and result["communication"]["repair_attempted"] is (not accepted)
    assert len(calls) == (2 if accepted else 3)
    assert result["communication"]["review"]["verdict"] == ("supported" if accepted else "unsupported")


async def test_unconfigured_conversation_never_constructs_a_model_or_invents_state(monkeypatch):
    from types import SimpleNamespace

    import fireflyframework_agentic.agents as agents

    from firefly_forge.communication import CommunicationWriter

    def forbidden(**kwargs):
        pytest.fail("Unconfigured conversations must not construct a model")

    monkeypatch.setattr(agents, "FireflyAgent", forbidden)
    result = await CommunicationWriter(SimpleNamespace()).write(
        None, "Respuesta nativa", lambda value: value, conversation=conversation("missing", "es")
    )
    assert result["body"] == "Respuesta nativa" and result["communication"]["reason"] == "unconfigured"
    assert "state" not in result["communication"]["facts"]


async def test_failed_conversation_generation_is_held_without_native_fallback(tmp_path):
    from firefly_forge.communication import CommunicationWriter

    calls = []

    def model(messages, info):
        calls.append(messages)
        raise TimeoutError()

    result = await CommunicationWriter(await configured_store(tmp_path), model=FunctionModel(model)).write(
        None, "Respuesta nativa", lambda value: value, conversation=conversation("missing", "es")
    )
    assert (
        len(calls) == 1
        and result["body"] == ""
        and result["communication"]["mode"] == "held"
        and result["communication"]["reason"] == "timeout"
    )
    assert "state" not in result["communication"]["facts"]


def test_native_conversation_input_cannot_carry_raw_comment_instructions():
    from firefly_forge.communication import CommunicationConversationV1

    with pytest.raises(ValueError):
        CommunicationConversationV1(selection="missing", language="en", linked_runs=0, raw_comment="IGNORE GUARDS")


@pytest.mark.parametrize("version", [True, 1.0, "1"])
def test_conversation_version_is_an_exact_integer(version):
    from firefly_forge.communication import CommunicationConversationV1

    with pytest.raises(ValueError):
        CommunicationConversationV1(version=version, selection="missing", language="es", linked_runs=0)
