# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Bounded repair progress and optional fresh-context review before delivery."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import uuid
import zipfile
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pyfly.container import service

from firefly_forge.configuration import redact_secrets
from firefly_forge.journal import Journal
from firefly_forge.process import checked
from firefly_forge.prompts import prompt_text
from firefly_forge.secrets import redact_environment
from firefly_forge.store import Store, now


class ReviewPending(RuntimeError):
    """The reviewer effect lacks a trustworthy completion receipt."""


class ReviewFinding(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    severity: Literal["low", "medium", "high", "critical"]
    path: str | None = Field(default=None, min_length=1, max_length=300)
    line: int | None = Field(default=None, ge=1, le=1_000_000)
    summary: str = Field(min_length=1, max_length=2000)
    evidence: str = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def relative_reference(self) -> ReviewFinding:
        if self.path and (Path(self.path).is_absolute() or ".." in Path(self.path).parts):
            raise ValueError("Review findings must reference relative source paths")
        if self.line and not self.path:
            raise ValueError("A review line requires a source path")
        return self


class ReviewReport(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    verdict: Literal["approved", "changes_required"]
    summary: str = Field(min_length=1, max_length=4000)
    findings: list[ReviewFinding] = Field(default_factory=list, max_length=30)

    @model_validator(mode="after")
    def coherent_verdict(self) -> ReviewReport:
        if self.verdict == "changes_required" and not self.findings:
            raise ValueError("A changes-required verdict needs concrete findings")
        if self.verdict == "approved" and any(f.severity in {"high", "critical"} for f in self.findings):
            raise ValueError("An approved review cannot retain high or critical findings")
        return self


def progress_record(
    history: list[dict[str, Any]], round_number: int, tree: str, failures: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    signature = hashlib.sha256(
        json.dumps(
            sorted((tuple(check["argv"]), int(check["exit_code"])) for check in failures), separators=(",", ":")
        ).encode()
    ).hexdigest()
    record = {"round": round_number, "source_tree": tree, "check_signature": signature}
    previous = next((item for item in reversed(history) if item["round"] < round_number), None)
    if previous and previous["source_tree"] == tree and previous["check_signature"] == signature:
        raise ValueError("No progress: the source tree and failing checks repeated across implementation rounds")
    return history if any(item["round"] == round_number for item in history) else [*history, record]


def _manifest(
    directory: Path,
    *,
    report: bool = False,
    original: bool = False,
    include_git: bool = False,
    extra_ignored: frozenset[str] = frozenset(),
) -> dict[str, str]:
    from firefly_forge.engine import private_path

    manifest: dict[str, str] = {}
    total = 0
    ignored = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", "build", "dist"} | extra_ignored
    for current, directories, files in os.walk(directory):
        directories[:] = [
            name
            for name in directories
            if (include_git or name != ".git")
            and not (original and (private_path(str((Path(current) / name).relative_to(directory))) or name in ignored))
        ]
        for name in [*directories, *files]:
            path = Path(current) / name
            relative = path.relative_to(directory)
            if report and relative.as_posix() == "review.json" or original and private_path(str(relative)):
                continue
            if path.is_symlink():
                raise ValueError("Review workspaces must not contain symbolic links")
            if path.is_dir():
                manifest[relative.as_posix() + "/"] = str(path.stat().st_mode)
            if path.is_file():
                info = path.stat()
                if info.st_nlink != 1:
                    raise ValueError("Review files must not be hard linked")
                total += info.st_size
                if total > 100_000_000 or len(manifest) >= 10000:
                    raise ValueError("Review source exceeds the supported size")
                manifest[relative.as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest() + str(info.st_mode)
    return manifest


@service
class Assurance:
    def __init__(
        self,
        store: Store,
        workspaces: Path,
        journal: Journal | None = None,
        provider_factory: Callable[[str], Any] | None = None,
    ) -> None:
        self.store, self.journal = store, journal
        self.workspaces = workspaces
        self.workspaces.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.workspaces.is_symlink():
            raise ValueError("Review workspaces require a private real directory")
        self.workspaces.chmod(0o700)
        self.provider_factory = provider_factory

    async def review(
        self,
        run: dict[str, Any],
        workspace: Path,
        emit: Callable[[str, str], Awaitable[None]],
        assert_ownership: Callable[[], Awaitable[None]] | None = None,
    ) -> dict[str, Any]:
        from firefly_forge.project_access import ProjectAccess

        await ProjectAccess(self.store).authorize_execution(run, "execute")
        agent = run.get("assurance", {}).get("review_agent")
        if not agent:
            result = {"status": "not_requested", "source_tree": run["source_tree"]}
            await self.store.update(run["id"], review=result)
            return result
        tree = run["source_tree"]
        intent = None
        if self.journal:
            plan = await self.journal.ensure_plan(run["id"])
            task = next(item for item in plan["tasks"] if item["id"] == "review")
            intent = await self.journal.intent(
                run["id"],
                "review",
                attempt=task["attempt"],
                payload={"source_tree": tree, "agent": agent, "config_revision": run.get("review_config_revision")},
            )
            if intent["disposition"] == "completed":
                evidence = task.get("evidence", {})
                if evidence.get("source_tree") != tree:
                    raise ValueError("Approved review evidence belongs to another source snapshot")
                await self.store.update(run["id"], review=evidence)
                return dict(evidence)
            if intent["disposition"] != "execute":
                raise ReviewPending("Independent review outcome is unknown; reconcile its receipt before continuing")
        if assert_ownership:
            await assert_ownership()
        original = _manifest(workspace, original=True)
        directory = self.workspaces / (run["id"] + "-" + uuid.uuid4().hex)
        directory.mkdir(mode=0o700)
        source = directory / "source"
        source.mkdir()
        archive = directory / "source.zip"
        await checked(["git", "archive", "--format=zip", f"--output={archive}", tree], workspace)
        with zipfile.ZipFile(archive) as content:
            total = 0
            for item in content.infolist():
                from firefly_forge.engine import private_path

                path = Path(item.filename)
                if private_path(item.filename):
                    continue
                total += item.file_size
                if (
                    path.is_absolute()
                    or ".." in path.parts
                    or stat.S_ISLNK(item.external_attr >> 16)
                    or total > 100_000_000
                ):
                    raise ValueError("Review source archive contains an unsafe or oversized entry")
                content.extract(item, source)
        archive.unlink()
        await checked(["git", "init", "--quiet"], directory)
        await checked(["git", "add", "--all"], directory)
        await checked(
            [
                "git",
                "-c",
                "user.name=forge review",
                "-c",
                "user.email=review@example.invalid",
                "commit",
                "--quiet",
                "-m",
                "Immutable review source",
            ],
            directory,
        )
        head = await checked(["git", "rev-parse", "HEAD"], directory)
        baseline = _manifest(directory, include_git=True)
        if run.get("review_model_selection"):
            selected = run["review_model_selection"]
            if (
                self.store.task_router is None
                or agent != selected["provider"]
                or run["review_config_revision"] != selected["config_revision"]
            ):
                raise ValueError("The frozen review model profile is unavailable or inconsistent")
            settings = self.store.task_router.profiles.validate_snapshot(selected)
        else:
            settings = (
                self.store.configuration.load_snapshot(run["review_config_revision"])
                if run.get("review_config_revision")
                else {}
            )
        if not self.provider_factory and not run.get("review_config_revision"):
            raise ValueError("Queue a frozen reviewer configuration before requesting independent review")
        if self.provider_factory:
            provider = self.provider_factory(agent)
        else:
            from firefly_forge.providers import get_provider

            provider = get_provider(agent, settings)
        from firefly_forge.configuration import validate_checkpoints

        validate_checkpoints(settings, task="review", provider=agent)
        brief = prompt_text(
            "assurance/review",
            schema=json.dumps(ReviewReport.model_json_schema()),
            brief=run["brief"],
            definition=json.dumps(run.get("product_definition")),
            checks=json.dumps(
                [{"argv": check["argv"], "exit_code": check["exit_code"]} for check in run["final_verification"]]
            ),
        )
        await emit("reviewing", "Independent review started in a fresh source-copy context")
        if assert_ownership:
            await assert_ownership()
        await ProjectAccess(self.store).authorize_execution(await self.store.get_run(run["id"]), "execute")
        from firefly_forge.bridge_completion_scope import bridge_completion_scope
        from firefly_forge.bridge_completions import BridgeCompletions
        from firefly_forge.providers import ProviderSubmissionCancelled

        completion_scope = (
            BridgeCompletions(self.store, self.journal).scope(
                await self.store.get_run(run["id"]), intent, review=True, assert_ownership=assert_ownership
            )
            if self.journal and intent
            else None
        )
        try:
            with bridge_completion_scope(completion_scope):
                await provider.execute(directory, brief, run["backend"], run["frontend"], emit)
        except ProviderSubmissionCancelled as exc:
            if self.journal and intent:
                await self.journal.fail(
                    run["id"],
                    intent["idempotency_key"],
                    "provider_outcome_unknown",
                    "Bridge review submission cancelled before its outcome was known",
                )
                exc.native_retained = True
            raise
        except Exception as exc:
            from firefly_forge.providers import (
                ProviderAdmissionRefused,
                ProviderLimitError,
                ProviderOutcomeUnknown,
                classify_provider_failure,
            )

            kind = classify_provider_failure(exc)
            if (
                self.journal
                and intent
                and kind
                and kind[0]
                in {
                    "quota",
                    "rate_limit",
                    "authentication",
                    "provider_paused",
                    "provider_outcome_unknown",
                    "bridge_admission_refused",
                }
            ):
                await self.journal.fail(
                    run["id"],
                    intent["idempotency_key"],
                    kind[0],
                    redact_secrets(redact_environment(str(exc)), settings),
                    retry_after=kind[1],
                    **(
                        {
                            "observation": json.loads(
                                redact_secrets(redact_environment(json.dumps(exc.observation)), settings)
                            )
                        }
                        if isinstance(exc, (ProviderLimitError, ProviderOutcomeUnknown, ProviderAdmissionRefused))
                        and exc.observation
                        else {}
                    ),
                )
            if isinstance(exc, ProviderOutcomeUnknown):
                raise
            raise ReviewPending(
                "Independent review did not return a durable result; inspect its paused or unknown outcome"
            ) from None
        try:
            if (
                _manifest(directory, report=True, include_git=True) != baseline
                or await checked(["git", "rev-parse", "HEAD"], directory) != head
            ):
                raise ValueError("Reviewer modified the immutable review source or Git baseline")
            if _manifest(workspace, original=True) != original:
                raise ValueError("Original product source changed during independent review")
            descriptor = os.open(directory / "review.json", os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(descriptor, "rb") as report_file:
                info = os.fstat(report_file.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 262144:
                    raise ValueError("Independent review must be a bounded regular JSON file")
                report_bytes = report_file.read(262145)
                if len(report_bytes) > 262144:
                    raise ValueError("Independent review exceeds the JSON size limit")
                raw = report_bytes.decode("utf-8")
            if redact_secrets(redact_environment(raw), settings) != raw:
                raise ValueError("Independent review contains configured credentials")
            try:
                report = ReviewReport.model_validate_json(raw)
            except ValueError:
                raise ValueError("Independent review does not conform to the strict JSON contract") from None
            for finding in report.findings:
                if finding.path:
                    path = source / finding.path
                    if not path.is_file() or path.is_symlink():
                        raise ValueError("Review finding references an unavailable source file")
                    text = path.read_text()
                    if finding.evidence not in text or finding.line and finding.line > len(text.splitlines()):
                        raise ValueError("Review evidence does not match its source reference")
            result = {
                "status": report.verdict,
                "summary": report.summary,
                "findings": [finding.model_dump() for finding in report.findings],
                "source_tree": tree,
                "agent": agent,
                "config_revision": run.get("review_config_revision"),
                "created_at": now(),
            }
            if assert_ownership:
                await assert_ownership()
            async with self.store._unit():
                await self.store.update(run["id"], review=result)
                if self.journal and intent:
                    await self.journal.receipt(run["id"], intent["idempotency_key"], result)
            await emit("reviewing", "Independent review verdict: " + report.verdict)
            return result
        except (ValueError, OSError) as exc:
            if self.journal and intent:
                await self.journal.fail(run["id"], intent["idempotency_key"], "source_invalid", str(exc))
            raise ValueError("Independent review was rejected: " + str(exc)) from None
