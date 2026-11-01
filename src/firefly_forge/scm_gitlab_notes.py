# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Bind GitLab Notes responses to a grant-checked repository and exact thread."""

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from firefly_forge.scm_authority_models import canonical_provider_id, exact_origin

Data = dict[str, Any]


def _positive_id(value: Any) -> str:
    if type(value) not in (int, str):
        raise ValueError("GitLab note references need canonical positive IDs")
    return canonical_provider_id("gitlab", str(value))


class GitLabNoteReadbackUncertain(ValueError):
    def __init__(self, remote: Data) -> None:
        super().__init__("GitLab accepted the note, but its exact readback could not be confirmed")
        self.remote = remote


@dataclass(frozen=True)
class GitLabNoteThread:
    project_id: str
    iid: str
    noteable_type: str
    endpoint: str
    web_url: str

    @classmethod
    def prepare(cls, repo: Data, issue: Data, api: str) -> "GitLabNoteThread":
        parsed = urlsplit(api)
        origin = exact_origin(parsed.scheme + "://" + parsed.netloc)
        # SCM application configuration currently accepts an origin, not a relative URL root.
        if parsed.path != "/api/v4" or parsed.query or parsed.fragment:
            raise ValueError("GitLab notes require the configured API base")
        name = repo["name"]
        if not isinstance(name, str) or any(
            part in {".", ".."} or not re.fullmatch(r"[A-Za-z0-9_.-]+", part) for part in name.split("/")
        ):
            raise ValueError("Invalid connected GitLab repository path")
        registered = repo["url"]
        if registered not in {origin + "/" + name, origin + "/" + name + ".git"}:
            raise ValueError("The registered GitLab repository does not match its configured origin and path")
        kind = issue["kind"]
        if kind not in {"issue", "merge_request"}:
            raise ValueError("GitLab notes require an issue or merge request thread")
        project_id, iid = _positive_id(repo["remote_repository_id"]), _positive_id(issue["number"])
        path = "issues" if kind == "issue" else "merge_requests"
        return cls(
            project_id,
            iid,
            "Issue" if kind == "issue" else "MergeRequest",
            api + f"/projects/{project_id}/{path}/{iid}/notes",
            origin + "/" + name + f"/-/{path}/{iid}",
        )

    def confirm(self, result: Any) -> tuple[Data, str]:
        if not isinstance(result, dict):
            raise ValueError("GitLab returned an invalid note response")
        note_id = _positive_id(result.get("id"))
        noteable_id = _positive_id(result.get("noteable_id"))
        if (
            _positive_id(result.get("project_id")) != self.project_id
            or _positive_id(result.get("noteable_iid")) != self.iid
            or result.get("noteable_type") != self.noteable_type
        ):
            raise ValueError("GitLab returned a note for a different project or thread")
        return {"id": int(note_id), "number": int(note_id), "url": self.web_url + "#note_" + note_id}, noteable_id
