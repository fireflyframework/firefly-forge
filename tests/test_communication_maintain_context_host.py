# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Verified owner wording reaches native outbox generation and final admission."""

import copy

from communication_model_fixture import reviewed_model
from test_communication_writer import configured_store
from test_scm_maintain_question_host import question
from test_scm_maintain_ticket_host import poll_case as poll_case
from test_scm_maintain_ticket_host import prepared_binding


async def test_verified_spanish_owner_and_requested_work_reach_native_maintain_post(poll_case, monkeypatch, tmp_path):
    async with poll_case() as c:
        comment = c.comment
        owner = "@forge, por favor informa sobre los cambios del CRM y los resultados de las comprobaciones."

        def localized(number, body):
            return comment(number, owner if number == 99 else body)

        c.comment = localized
        service, request = await prepared_binding(c, monkeypatch)
        child = c.db["runs"][request.run_id].data
        child.update(
            component_snapshot={"name": "Orbit CRM", "responsibility": "Company and contact workflows"},
            brief="Fix company conflicts and CSRF checks. Do not claim those fixes are complete.",
            attempt=1,
        )
        original_parent = copy.deepcopy(c.db["runs"][c.run_id].data)
        kept = await service.configure(c.project, c.repository, request)
        await question(c, request.run_id)
        c.store.task_router = (await configured_store(tmp_path / "communication")).task_router
        calls = []
        c.collaboration.communication_writer.model = reviewed_model(
            {
                "title": {
                    "text": "Decisión para los cambios solicitados en Orbit CRM",
                    "fact_ids": ["work", "requested_work"],
                },
                "paragraphs": [
                    {
                        "text": (
                            "La tarea solicita revisar los conflictos de empresas y las comprobaciones CSRF. "
                            "El trabajo está pausado por una pregunta pendiente."
                        ),
                        "fact_ids": ["requested_work", "state", "audience"],
                    },
                    {
                        "text": "Este intento todavía no tiene resultados de verificación registrados.",
                        "fact_ids": ["check_status"],
                    },
                ],
                "decision": {
                    "question": "Keep the existing contacts?",
                    "context": "This fresh maintenance unit preserves the original records\\.",
                    "choices": ["Keep", "Review"],
                },
            },
            calls=calls,
        )
        await c.collaboration.feedback(kept["outbox_id"])
        sent = await c.collaboration.receipt(kept["outbox_id"])
        assert sent["feedback_status"] == "sent", sent.get("reason")
        assert "Decisión para los cambios solicitados en Orbit CRM" in sent["feedback_intent"]["body"]
        assert "Keep the existing contacts?" in sent["feedback_intent"]["body"]
        assert [phase for phase, _, _ in calls] == ["draft", "review"]
        assert owner in str(calls)
        assert sent["feedback_intent"]["communication"]["facts"]["check_status"]["recorded"] == 0
        assert c.db["runs"][c.run_id].data == original_parent
        assert c.db["runs"][request.run_id].data["status"] == "paused"
