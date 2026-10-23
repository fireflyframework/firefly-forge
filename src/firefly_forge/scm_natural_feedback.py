# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Quote the retained model reply alongside the exact recorded task outcome."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from firefly_forge.collaboration_feedback import _text
from firefly_forge.scm_natural_render import reply_quote
from firefly_forge.scm_natural_runtime import catalog


def natural_card(data: dict[str, Any], safe: Callable[[str], str]) -> str:
    turn = data["natural_turn"]
    language = (turn.get("decision") or {}).get("language") or data["command"].get("language", "en")
    copy = json.loads(catalog().text("scm-natural/copy"))[language]
    state = turn["state"]
    if state == "needs_reconciliation":
        raise ValueError("An unknown natural outcome cannot produce a successful reply")
    outcome = copy.get(state, copy["waiting"])
    message = str(turn.get("reply") or "")
    questions = (turn.get("decision") or {}).get("questions", [])
    quote = reply_quote(message, questions, safe) if message else ""
    resolution = turn.get("resolution") or {}
    if resolution.get("state") == "waiting" and len(resolution.get("candidates", [])) > 1:
        options = [
            f"{index}. {_text(row.get('question') or copy['task'], safe, 150)}"
            for index, row in enumerate(resolution["candidates"][:10], 1)
        ]
        outcome += "\n\n" + "\n".join(options)
    return catalog().text("scm-natural/feedback", message=quote, outcome=outcome).strip()
