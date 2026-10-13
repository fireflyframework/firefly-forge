# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Reviewed, scoped source facts for product dialogue and explicit knowledge admission."""

from __future__ import annotations

import base64
import copy
import hashlib
import json
from pathlib import PurePosixPath
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pyfly.container import service

from firefly_forge.engine import private_path
from firefly_forge.knowledge_uploads import KnowledgeUploadInput, KnowledgeUploads
from firefly_forge.projects import Projects
from firefly_forge.reverse_engineering import ReverseEngineering

Data = dict[str, Any]


def _bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()


def _git_digest(value: Any) -> bool:
    return isinstance(value, str) and len(value) in {40, 64} and all(c in "0123456789abcdef" for c in value)


class AnalysisContextInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    component_ids: list[str] = Field(default_factory=list, max_length=50)

    @model_validator(mode="after")
    def unique_components(self) -> AnalysisContextInput:
        if len(set(self.component_ids)) != len(self.component_ids):
            raise ValueError("Select unique analyzed component identifiers")
        return self


@service
class ReviewedAnalysisContext:
    def __init__(
        self, analysis: ReverseEngineering, projects: Projects, uploads: KnowledgeUploads | None = None
    ) -> None:
        self.analysis, self.projects, self.uploads = analysis, projects, uploads

    @staticmethod
    def validate_frozen(snapshot: Data) -> None:
        value = {key: item for key, item in snapshot.items() if key != "context_digest"}
        if (
            hashlib.sha256(_bytes(value)).hexdigest() != snapshot.get("context_digest")
            or snapshot.get("business_journeys_confirmed") is not False
            or snapshot.get("live_behavior_verified") is not False
            or len(json.dumps(snapshot, ensure_ascii=False).encode()) > 40000
        ):
            raise ValueError("Reviewed AS-IS context integrity verification failed")

    async def _reviewed(self, project_id: str, analysis_id: str, request: AnalysisContextInput) -> tuple[Data, Data]:
        report = await self.analysis.analysis(project_id, analysis_id)
        if report.get("status") != "reviewed" or report.get("reviewed") is not True:
            raise ValueError("Only an explicitly reviewed AS-IS analysis can enter product context")
        snapshot = report["project_snapshot"]
        signed = {"project_digest": snapshot["digest"], "components": report["components"]}
        # Source inspection used the default JSON separators and ASCII escaping.
        actual = hashlib.sha256(json.dumps(signed, sort_keys=True).encode()).hexdigest()
        if request.digest != report.get("digest") or actual != request.digest:
            raise ValueError("Reviewed AS-IS evidence digest differs from its exact source facts")
        project = await self.projects.project(project_id)
        if snapshot.get("id") != project_id or project["digest"] != snapshot["digest"]:
            raise ValueError("The logical project changed; review a current source analysis before handoff")
        graph = {component["id"]: component for component in project["definition"]["components"]}
        observed = {component["id"] for component in report["components"]}
        if set(request.component_ids) - observed:
            raise ValueError("Choose component identifiers present in this reviewed analysis")
        if set(report["component_ids"]) != observed:
            raise ValueError("Reviewed analysis component selection differs from its evidence")
        for component in report["components"]:
            configured = graph.get(component["id"])
            if (
                configured is None
                or any(configured[key] != component[key] for key in ("repository_id", "path"))
                or not _git_digest(component["commit"])
            ):
                raise ValueError("Reviewed component repository or path differs from the logical project")
            for fact in component["facts"]:
                self._fact(component, fact)
        return report, project

    @staticmethod
    def _fact(component: Data, fact: Data) -> None:
        source = fact["source"]
        path = PurePosixPath(source["path"])
        scope = component["path"]
        if (
            path.is_absolute()
            or ".." in path.parts
            or path.as_posix() != source["path"]
            or private_path(source["path"])
            or (scope != "." and not source["path"].startswith(scope + "/"))
            or source["commit"] != component["commit"]
            or not _git_digest(source["blob"])
            or type(source["line"]) is not int
            or source["line"] < 1
            or fact.get("confidence") != "observed"
            or fact["kind"] not in {"http_route", "declared_test", "declared_dependency"}
            or not isinstance(fact["value"], str)
            or len(fact["value"]) > 400
            or not isinstance(source["excerpt"], str)
            or len(source["excerpt"]) > 500
        ):
            raise ValueError("AS-IS fact must retain exact bounded commit, blob, path and line evidence")

    @staticmethod
    def _document(project_id: str, report: Data, request: AnalysisContextInput) -> Data:
        selected = [
            component
            for component in report["components"]
            if not request.component_ids or component["id"] in request.component_ids
        ]
        return {
            "analysis_id": report["id"],
            "analysis_digest": report["digest"],
            "project_id": project_id,
            "project_digest": report["project_snapshot"]["digest"],
            "reviewed_at": report["reviewed_at"],
            "method": "source_inspection",
            "source_reviewed": True,
            "business_journeys_confirmed": False,
            "live_behavior_verified": False,
            "components": copy.deepcopy(selected),
            "open_questions": copy.deepcopy(report["open_questions"]),
        }

    async def snapshot(self, project_id: str, analysis_id: str, request: AnalysisContextInput) -> Data:
        report, _ = await self._reviewed(project_id, analysis_id, request)
        document = self._document(project_id, report, request)
        originals = document["components"]
        total = sum(len(component["facts"]) for component in originals)
        document["components"] = [
            {key: value for key, value in component.items() if key != "facts"} | {"facts": []}
            for component in originals
        ]
        document.update(citations=[], included_facts=0, omitted_facts=total, truncated=bool(total))
        if len(json.dumps(document, ensure_ascii=False).encode()) > 39900:
            raise ValueError("Reviewed component metadata exceeds 40 KB; select fewer components")
        for original, component in zip(originals, document["components"], strict=True):
            for fact in original["facts"]:
                identifier = (
                    "as-is-"
                    + hashlib.sha256(
                        _bytes(
                            {
                                "analysis_digest": report["digest"],
                                "component_id": component["id"],
                                "fact": fact,
                            }
                        )
                    ).hexdigest()[:32]
                )
                admitted = copy.deepcopy(fact) | {"citation_id": identifier}
                citation = {
                    "id": identifier,
                    "component_id": component["id"],
                    "repository_id": component["repository_id"],
                    **copy.deepcopy(fact["source"]),
                }
                component["facts"].append(admitted)
                document["citations"].append(citation)
                if len(json.dumps(document, ensure_ascii=False).encode()) > 39900:
                    component["facts"].pop()
                    document["citations"].pop()
                    break
                document["included_facts"] += 1
                document["omitted_facts"] -= 1
        document["truncated"] = document["omitted_facts"] > 0
        document["context_digest"] = hashlib.sha256(_bytes(document)).hexdigest()
        if len(json.dumps(document, ensure_ascii=False).encode()) > 40000:
            raise ValueError("Reviewed AS-IS context exceeds its bounded snapshot")
        return document

    async def admit(self, project_id: str, analysis_id: str, request: AnalysisContextInput) -> Data:
        report, project = await self._reviewed(project_id, analysis_id, request)
        if self.uploads is None:
            raise ValueError("Explicit AS-IS knowledge admission is not configured")
        document = self._document(project_id, report, request)
        content = _bytes(document)
        if len(content) > 1_000_000:
            raise ValueError("Selected AS-IS source facts exceed 1 MB; admit fewer components explicitly")
        repository_id = project["definition"]["product_repository_id"]
        return await self.uploads.upload(
            repository_id,
            KnowledgeUploadInput(
                filename="as-is-" + analysis_id + ".json",
                title="Reviewed AS-IS: " + project["definition"]["name"],
                content_base64=base64.b64encode(content).decode(),
            ),
            provenance={
                "analysis_id": analysis_id,
                "analysis_digest": report["digest"],
                "project_id": project_id,
                "project_digest": project["digest"],
                "component_ids": [item["id"] for item in document["components"]],
            },
        )
