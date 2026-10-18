# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Bounded public facts rendered through native Agentic Markdown templates."""

from __future__ import annotations

import hashlib
import html
import json
import re
from collections.abc import Callable
from functools import cache
from typing import Any
from urllib.parse import urlsplit

from firefly_forge.prompts import prompt_text

Data = dict[str, Any]
MAX_FEEDBACK = 32
TERMINAL = frozenset({"succeeded", "failed", "cancelled", "interrupted"})
STAGES = frozenset({"prepare", "implement", "verify", "review", "checkpoint"})


@cache
def _copy() -> Data:
    return dict(json.loads(prompt_text("collaboration/copy")))


def _text(value: object, safe: Callable[[str], str], limit: int = 1200) -> str:
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        return ""
    value = safe(str(value))
    value = re.sub(r"(?:[A-Za-z]:\\|/)[^\s<>]+", _copy()["redactions"]["path"], value)
    value = re.sub(r"\b[A-Z][A-Z0-9_]*=\S+", _copy()["redactions"]["setting"], value)
    result = []
    length = 0
    for character in " ".join(value.split()):
        escaped = html.escape(character, quote=True)
        if character == "@":
            escaped = "＠"
        elif character in "\\`*_{}[]()#+.!|>~-":
            escaped = "\\" + escaped
        if length + len(escaped) > limit:
            break
        result.append(escaped)
        length += len(escaped)
    return "".join(result)


def _link(value: object, host: str, repository: Data, *, reference: bool = False) -> str:
    if not isinstance(value, str) or len(value) > 600:
        return ""
    parsed, allowed = urlsplit(value), urlsplit(host)
    if (
        parsed.scheme != "https"
        or parsed.netloc != allowed.netloc
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or re.search(r"[\s<>\\()\[\]]", value)
    ):
        return ""
    # A review link must name this repository, not merely share a trusted hostname.
    remote = urlsplit(str(repository.get("url", ""))).path.removesuffix(".git").rstrip("/")
    if not remote or not parsed.path.startswith(remote + "/"):
        return ""
    suffix = (
        r"/(?:tree|-/tree|src)/(?:forge-reference|forge(?:/|%2F)references(?:/|%2F)[a-f0-9]{64})"
        if reference
        else r"/(?:pull|merge_requests|-/merge_requests|pull-requests)/[0-9]+"
    )
    if not re.fullmatch(re.escape(remote) + suffix, parsed.path):
        return ""
    return value


def verification_handoff(run: Data) -> Data:
    value = run.get("verification_handoff") or {}
    request = run.get("worker_request") or {}
    if (
        type(value.get("version")) is not int
        or value.get("version") != 1
        or value.get("request_id") != request.get("id")
        or not value.get("id")
        or run.get("status") in TERMINAL
        and value.get("status") in {"pending", "running"}
        or value.get("status") not in {"pending", "running", "completed", "held"}
        or any(
            type(value.get(key, 0)) is not int or not 0 <= value.get(key, 0) <= 20
            for key in ("commands_count", "completed_commands", "failed_commands")
        )
    ):
        return {}
    return {
        "id": value["id"],
        "status": value["status"],
        **{
            key: value.get(key, 0)
            for key in ("commands_count", "completed_commands", "failed_commands")
            if value.get("status") in {"completed", "held"}
        },
    }


def verification_refusal(run: Data) -> Data:
    value = run.get("verification_refusal") or {}
    request = run.get("worker_request") or {}
    if (
        type(value.get("version")) is int
        and value.get("version") == 1
        and value.get("request_id") == request.get("id")
        and request.get("status") == "pending"
        and run.get("status") not in TERMINAL
        and value.get("status") == "held"
        and value.get("reason") == "admission_refused"
    ):
        return {"request_id": value["request_id"], "reason": value["reason"]}
    return {}


def review_key(run: Data) -> str | None:
    value = run.get("pr_url")
    return hashlib.sha256(str(value).encode()).hexdigest() if value else None


def milestone(run: Data) -> Data:
    """Use durable run fields, never provider output messages or wall-clock ticks."""
    request = run.get("worker_request") or {}
    checks = run.get("final_verification") or []
    return {
        "status": run.get("status"),
        "task": run.get("active_task") if run.get("active_task") in STAGES else None,
        "request": request.get("id") if request.get("status") == "pending" else None,
        "pause": (run.get("pause") or {}).get("kind"),
        "checks": [check.get("exit_code") for check in checks if isinstance(check, dict)],
        "review": run.get("pr_url"),
        "publication_error": bool(run.get("publication_error")),
        "handoff": verification_handoff(run),
        "verification_refusal": verification_refusal(run),
    }


def milestone_key(run: Data) -> str:
    return hashlib.sha256(json.dumps(milestone(run), sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def sent_history(data: Data) -> list[Data]:
    history = list(data.get("feedback_history") or [])
    if not history and data.get("feedback_status") == "sent":
        history.append({"legacy": True})
    return history


def feedback_due(data: Data, run: Data) -> bool:
    if data.get("feedback_status") != "sent" or not run:
        return False
    history = sent_history(data)
    if len(history) >= MAX_FEEDBACK:
        return False
    if len(history) >= MAX_FEEDBACK - 2 and run.get("status") not in TERMINAL:
        return False
    if len(history) == MAX_FEEDBACK - 1 and data.get("feedback_stage") in TERMINAL:
        published = bool(run.get("pr_url")) and review_key(run) != data.get("feedback_review_key")
        failed = bool(run.get("publication_error")) and not data.get("feedback_publication_error")
        if not published and not failed:
            return False
    if data.get("feedback_key"):
        return bool(data["feedback_key"] != milestone_key(run))
    # Existing receipts adopt fingerprints only on a real transition, without reposting on upgrade.
    return bool(
        run.get("status") != data.get("feedback_stage")
        or ((run.get("worker_request") or {}).get("id") != data.get("feedback_request_id"))
    )


def feedback_card(data: Data, run: Data, repository: Data, host: str, safe: Callable[[str], str]) -> str:
    copy = _copy()
    status = str(run.get("status") or (data.get("action_result") or {}).get("status") or "recorded")
    action = str((data.get("command") or {}).get("action", "status"))
    request = run.get("worker_request") or {}
    handoff = verification_handoff(run)
    refused = verification_refusal(run)
    state = "question" if request.get("status") == "pending" and status not in TERMINAL else status
    description = copy["states"].get(state, copy["states"]["unknown"])
    stage = run.get("active_task")
    if status == "running" and stage in copy["stages"]:
        description = copy["stages"][stage]
    sections = []
    result = data.get("action_result") or {}
    if result:
        sections.append(
            prompt_text(
                "collaboration/action",
                summary=copy["actions"].get(action, copy["actions"]["other"]),
                status=_text(result.get("status", "recorded"), safe, 80),
            )
        )
    if action == "assistant" and isinstance(result.get("reply"), str):
        sections.append(prompt_text("collaboration/reply", reply=_text(result["reply"], safe)))
    facts = []
    for field, label in copy["facts"].items():
        value = result.get(field)
        if type(value) is int and value >= 0 or isinstance(value, str) and re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", value):
            facts.append(label + ": `" + _text(value, safe, 80) + "`")
    if facts:
        sections.append(prompt_text("collaboration/facts", facts="\n".join(facts)))
    reference = result.get("remote_reference") or {}
    reference_url = _link(reference.get("url"), host, repository, reference=True) if reference.get("published") else ""
    if reference_url:
        safe(reference_url)
        sections.append(prompt_text("collaboration/reference", url=reference_url))
    if handoff:
        sections.append(
            prompt_text(
                "collaboration/handoff",
                explanation=copy["handoff"][handoff["status"]],
                total=str(
                    handoff.get("commands_count", (run.get("verification_handoff") or {}).get("commands_count", 0))
                ),
                completed=str(handoff.get("completed_commands", 0)),
                failed=str(handoff.get("failed_commands", 0)),
            )
        )
        if request.get("status") == "pending" and handoff["status"] in {"pending", "running", "completed"}:
            description = copy["handoff"][handoff["status"]]
    if refused:
        sections.append(prompt_text("collaboration/recovery", explanation=copy["verification_refused"]))
    if (
        status not in TERMINAL
        and request.get("status") == "pending"
        and handoff.get("status") not in {"pending", "running", "completed"}
    ):
        choices = request.get("choices") or []
        sections.append(
            prompt_text(
                "collaboration/question",
                question=_text(request.get("question", ""), safe),
                request_id=_text(request.get("id", ""), safe, 80),
                choices=(
                    copy["choices_heading"]
                    + "\n"
                    + "\n".join("- " + _text(choice, safe, 200) for choice in choices[:12])
                )
                if choices
                else "",
            )
        )
    elif not handoff and status in {"paused", "needs_reconciliation", "failed", "interrupted", "cancelled"}:
        kind = str((run.get("pause") or {}).get("kind", "unknown"))
        if status == "paused" and request.get("status") == "answered" and kind == "worker_request":
            kind = "answered"
        sections.append(
            prompt_text("collaboration/recovery", explanation=copy["recovery"].get(kind, copy["recovery"]["unknown"]))
        )
    checks = run.get("final_verification") or []
    if checks:
        passed = sum(type(check.get("exit_code")) is int and check["exit_code"] == 0 for check in checks)
        sections.append(prompt_text("collaboration/verification", passed=str(passed), total=str(len(checks))))
    url = _link(run.get("pr_url"), host, repository)
    if url:
        safe(url)
        sections.append(prompt_text("collaboration/review", url=url))
    elif run.get("publication_error"):
        sections.append(prompt_text("collaboration/publication-held"))
    if action == "assistant":
        sections.append(prompt_text("collaboration/proposal"))
    return prompt_text(
        "collaboration/card",
        action=action if action in copy["actions"] else "status",
        description=description,
        status=_text(status, safe, 80),
        run_reference=(
            prompt_text("collaboration/run", run_id=_text(run.get("id", data.get("run_id", "")), safe, 80))
            if run.get("id") or data.get("run_id")
            else ""
        ),
        receipt_id=_text(data["id"], safe, 80),
        sections="\n\n".join(sections),
    )
