# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Bounded source-only AS-IS inspection with exact commit and line evidence."""

from __future__ import annotations

import ast
import asyncio
import copy
import hashlib
import json
import re
import uuid
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from pyfly.container import service
from pyfly.data.relational.sqlalchemy import LockMode

from firefly_forge.configuration import redact_secrets
from firefly_forge.engine import private_path
from firefly_forge.process import checked, execute
from firefly_forge.project_persistence import ForgeAnalysisEntity, ForgeAnalysisRepository
from firefly_forge.projects import Projects
from firefly_forge.scm import git_credentials
from firefly_forge.secrets import redact_environment
from firefly_forge.store import Store, identifier, now

Data = dict[str, Any]


class AnalysisInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    component_ids: list[str] = Field(default_factory=list, max_length=50)


class AnalysisReview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    reviewed: Literal[True] = True


@service
class ReverseEngineering:
    def __init__(
        self,
        store: Store,
        projects: Projects,
        directory: Path,
        connections: Any = None,
        *,
        records: ForgeAnalysisRepository | None = None,
    ) -> None:
        self.store, self.projects, self.directory, self.connections = store, projects, directory, connections
        self.records = records or ForgeAnalysisRepository()
        self.active: set[str] = set()

    def safe(self, value: str) -> str:
        secrets = self.connections.redaction_settings() if self.connections else {}
        return redact_secrets(redact_environment(value), secrets)

    async def analysis(self, project_id: str, analysis_id: str) -> Data:
        async with self.store._unit():
            row = await self.records.find_by_id(identifier(analysis_id))
            if row is None or row.project_id != identifier(project_id):
                raise KeyError("AS-IS analysis not found in this product project")
            return copy.deepcopy(row.data)

    async def analyses(self, project_id: str) -> list[Data]:
        await self.projects.project(project_id)
        async with self.store._unit():
            values = [copy.deepcopy(row.data) for row in await self.records.latest(identifier(project_id))]
        for index, value in enumerate(values):
            if value["status"] == "analyzing" and value["id"] not in self.active:
                values[index] = await self._change(
                    value,
                    status="interrupted",
                    detail="Source inspection was interrupted; request a new analysis explicitly",
                )
        return values

    async def review(self, project_id: str, analysis_id: str, request: AnalysisReview) -> Data:
        report = await self.analysis(project_id, analysis_id)
        if report["status"] not in {"ready_for_review", "reviewed"} or request.digest != report.get("digest"):
            raise ValueError("Review requires the exact ready AS-IS evidence digest")
        if report["reviewed"]:
            return report
        return await self._change(report, status="reviewed", reviewed=True, reviewed_at=now())

    async def _change(self, data: Data, **changes: Any) -> Data:
        async with self.store._unit():
            row = await self.records.find_by_id(identifier(data["id"]), lock=LockMode.PESSIMISTIC_WRITE)
            if row is None:
                raise KeyError("AS-IS analysis not found")
            if row.status != data["status"]:
                return copy.deepcopy(row.data)
            result = {**copy.deepcopy(row.data), **changes, "updated_at": now()}
            result["history"].append({"status": result["status"], "at": now()})
            row.data, row.status = result, result["status"]
            await self.records.save(row)
            return copy.deepcopy(result)

    async def analyze(self, project_id: str, request: AnalysisInput) -> Data:
        project = await self.projects.project(project_id)
        components = project["definition"]["components"]
        if set(request.component_ids) - {item["id"] for item in components}:
            raise ValueError("Choose components from this product project")
        selected = [item for item in components if not request.component_ids or item["id"] in request.component_ids]
        data: Data = {
            "id": uuid.uuid4().hex,
            "project_id": project_id,
            "project_snapshot": project,
            "component_ids": [item["id"] for item in selected],
            "status": "analyzing",
            "reviewed": False,
            "method": "source_inspection",
            "created_at": now(),
            "updated_at": now(),
            "history": [{"status": "analyzing", "at": now()}],
            "components": [],
            "documents": {},
            "functional_definition": None,
            "open_questions": [
                "Confirm the customer, business problem and actual end-to-end journeys with product owners.",
                "Review the observed contracts and define expected behavior, acceptance and failure paths.",
                "Confirm runtime configuration and deployments; source inspection does not prove live behavior.",
            ],
        }
        async with self.store._unit():
            await self.records.save(
                ForgeAnalysisEntity(
                    id=identifier(data["id"]), project_id=identifier(project_id), status="analyzing", data=data
                )
            )
        self.active.add(data["id"])
        try:
            async with asyncio.timeout(300):
                root = self.directory / data["id"]
                root.mkdir(parents=True, mode=0o700)
                clones: dict[str, tuple[Path, str]] = {}
                budget = {"files": 0, "bytes": 0}
                reports = []
                for component in selected:
                    repository_id = component["repository_id"]
                    if repository_id not in clones:
                        repository = await self.store.repository(repository_id)
                        destination = root / repository_id
                        credentials = (
                            await self.connections.git_environment(repository)
                            if self.connections and repository.get("connection_provider")
                            else None
                        )
                        async with git_credentials(credentials) as env:
                            await checked(
                                [
                                    "git",
                                    "-c",
                                    "init.templateDir=",
                                    "-c",
                                    "core.hooksPath=/dev/null",
                                    "clone",
                                    "--bare",
                                    "--no-hardlinks",
                                    "--single-branch",
                                    "--branch",
                                    repository["default_branch"],
                                    "--",
                                    repository["url"],
                                    str(destination),
                                ],
                                root,
                                timeout=120,
                                env={**(env or {}), "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"},
                            )
                        clones[repository_id] = (destination, await checked(["git", "rev-parse", "HEAD"], destination))
                    directory, commit = clones[repository_id]
                    reports.append(await self._inspect(component, directory, commit, budget))
                signed = {"project_digest": project["digest"], "components": reports}
                digest = hashlib.sha256(json.dumps(signed, sort_keys=True).encode()).hexdigest()
                lines = [
                    "# Functional AS-IS",
                    "",
                    "Product: " + project["definition"]["name"],
                    "",
                    "Source observations require owner review. Business journeys and live behavior remain unverified.",
                    "",
                ]
                for report in reports:
                    lines.extend(["## " + report["name"], "", "Commit: `" + report["commit"] + "`", ""])
                    for fact in report["facts"]:
                        evidence = fact["source"]
                        lines.append(f"- {fact['kind']}: {fact['value']} ({evidence['path']}:{evidence['line']}).")
                    lines.append("")
                lines.extend(
                    ["## Questions for product owners", "", *["- " + question for question in data["open_questions"]]]
                )
                return await self._change(
                    data,
                    status="ready_for_review",
                    components=reports,
                    digest=digest,
                    documents={
                        "functional-as-is.md": "\n".join(lines) + "\n",
                        "source-evidence.json": json.dumps(signed, indent=2),
                    },
                )
        except asyncio.CancelledError:
            await self._change(
                data,
                status="interrupted",
                detail="Source inspection was interrupted; no source was executed or published",
            )
            raise
        except Exception as exc:
            await self._change(data, status="failed", detail=self.safe(str(exc))[:2000])
            raise ValueError("AS-IS analysis failed; inspect its retained receipt") from None
        finally:
            self.active.discard(data["id"])

    async def _inspect(self, component: Data, directory: Path, commit: str, budget: dict[str, int]) -> Data:
        inventory = await checked(["git", "ls-tree", "-r", "-z", "--full-tree", commit], directory)
        facts: list[Data] = []
        skipped = 0
        for entry in inventory.split("\x00"):
            if not entry:
                continue
            metadata, path = entry.split("\t", 1)
            mode, kind, blob = metadata.split(" ")
            scope = component["path"]
            if scope != "." and not path.startswith(scope + "/"):
                continue
            if mode not in {"100644", "100755"} or kind != "blob" or private_path(path):
                continue
            if Path(path).suffix not in {".py", ".php", ".json", ".toml", ".md", ".yml", ".yaml", ".js", ".ts", ".tsx"}:
                continue
            size = int(await checked(["git", "cat-file", "-s", blob], directory))
            if size > 65536 or budget["files"] >= 200 or budget["bytes"] + size > 2_000_000:
                skipped += 1
                continue
            result = await execute(["git", "cat-file", "blob", blob], directory)
            if result.exit_code:
                raise ValueError("Cannot read the committed source blob")
            content = result.output
            budget["files"] += 1
            budget["bytes"] += size
            if self.safe(content) != content:
                skipped += 1
                continue
            lines = content.splitlines()

            def fact(
                kind: str,
                value: str,
                line: int,
                source_lines: list[str] = lines,
                source_path: str = path,
                source_blob: str = blob,
            ) -> None:
                if len(facts) < 200 and 1 <= line <= len(source_lines):
                    facts.append(
                        {
                            "kind": kind,
                            "value": value[:400],
                            "confidence": "observed",
                            "source": {
                                "path": source_path,
                                "line": line,
                                "excerpt": source_lines[line - 1][:500],
                                "blob": source_blob,
                                "commit": commit,
                            },
                        }
                    )

            if path.endswith(".py"):
                try:
                    tree = ast.parse(content)
                except SyntaxError:
                    continue
                for node in ast.walk(tree):
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        for decorator in node.decorator_list:
                            if (
                                isinstance(decorator, ast.Call)
                                and isinstance(decorator.func, ast.Name)
                                and decorator.func.id
                                in {"get_mapping", "post_mapping", "put_mapping", "delete_mapping", "patch_mapping"}
                                and decorator.args
                                and isinstance(decorator.args[0], ast.Constant)
                                and isinstance(decorator.args[0].value, str)
                            ):
                                fact("http_route", decorator.args[0].value, decorator.lineno)
                        if node.name.startswith("test_"):
                            fact("declared_test", node.name, node.lineno)
            for number, line in enumerate(lines, 1):
                match = (
                    re.search(r"Route::(get|post|put|patch|delete)\(\s*['\"]([^'\"]+)['\"]", line)
                    if path.endswith(".php")
                    else None
                )
                if match:
                    fact("http_route", match[1].upper() + " " + match[2], number)
                if Path(path).name in {"pyproject.toml", "composer.json", "package.json"} and any(
                    name in line.lower() for name in ("pyfly", "larafly", "react", "vue", "angular", "next")
                ):
                    fact("declared_dependency", line.strip(), number)
        return {
            "id": component["id"],
            "name": component["name"],
            "repository_id": component["repository_id"],
            "path": component["path"],
            "commit": commit,
            "facts": facts,
            "skipped_files": skipped,
            "execution": "source_only",
        }
