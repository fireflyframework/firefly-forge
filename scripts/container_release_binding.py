# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Bind a container-only publication to an existing public stable release and exact tag commit."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import stat
import subprocess
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import Any

SHA = re.compile(r"[0-9a-f]{40}")


def _project_version(body: bytes, tag: str) -> str:
    try:
        project = tomllib.loads(body.decode())["project"]
    except (KeyError, UnicodeError, tomllib.TOMLDecodeError):
        raise ValueError("Tagged project metadata is invalid") from None
    if not isinstance(project, dict) or project.get("name") != "firefly-forge" or project.get("version") != tag[1:]:
        raise ValueError("Published tag differs from the Forge project version")
    return tag[1:]


def verify_release(
    repository: str,
    tag: str,
    expected_commit: str,
    read_api: Callable[[str], dict[str, Any]],
    *,
    previous: dict[str, str] | None = None,
) -> dict[str, str]:
    if (
        not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository)
        or any(part in {".", ".."} for part in repository.split("/"))
        or not re.fullmatch(r"v(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)", tag)
        or not SHA.fullmatch(expected_commit)
    ):
        raise ValueError("Expected a repository, stable version tag and full lowercase commit SHA")
    prefix = "repos/" + repository
    public = read_api(prefix)
    release = read_api(prefix + "/releases/tags/" + tag)
    if (
        public.get("private") is not False
        or str(public.get("full_name", "")).casefold() != repository.casefold()
        or release.get("tag_name") != tag
        or release.get("draft") is not False
        or release.get("prerelease") is not False
        or not isinstance(release.get("published_at"), str)
        or not release["published_at"]
        or type(release.get("id")) is not int
        or release["id"] < 1
    ):
        raise ValueError("An existing public published stable release is required")
    target = read_api(prefix + "/git/ref/tags/" + tag).get("object", {})
    original_object = target.get("sha")
    seen: set[str] = set()
    while target.get("type") == "tag":
        identity = target.get("sha", "")
        if not isinstance(identity, str) or not SHA.fullmatch(identity) or identity in seen or len(seen) >= 8:
            raise ValueError("Invalid or ambiguous published tag chain")
        seen.add(identity)
        annotation = read_api(prefix + "/git/tags/" + identity)
        if annotation.get("sha") != identity:
            raise ValueError("Published tag identity changed")
        target = annotation.get("object", {})
    if target.get("type") != "commit" or target.get("sha") != expected_commit:
        raise ValueError("Published tag does not resolve to the reviewed commit")
    content = read_api(prefix + "/contents/pyproject.toml?ref=" + expected_commit)
    if content.get("type") != "file" or content.get("path") != "pyproject.toml" or content.get("encoding") != "base64":
        raise ValueError("Tagged project metadata must be an ordinary file")
    try:
        body = base64.b64decode("".join(content["content"].split()), validate=True)
    except (KeyError, TypeError, ValueError, AttributeError):
        raise ValueError("Tagged project metadata is invalid") from None
    blob = hashlib.sha1(b"blob " + str(len(body)).encode() + b"\0" + body).hexdigest()
    if content.get("sha") != blob or type(content.get("size")) is not int or content["size"] != len(body):
        raise ValueError("Tagged project metadata failed its Git blob binding")
    version = _project_version(body, tag)
    binding = {
        "repository": repository,
        "tag": tag,
        "commit": expected_commit,
        "tag_object": str(original_object),
        "release_id": str(release["id"]),
        "version": version,
    }
    if previous is not None and any(binding[name] != previous.get(name) for name in ("tag_object", "release_id")):
        raise ValueError("Published release or tag identity changed after initial validation")
    return binding


def verify_checkout(checkout: Path, binding: dict[str, str]) -> None:
    project = checkout / "pyproject.toml"
    if checkout.is_symlink() or not stat.S_ISREG(project.lstat().st_mode):
        raise ValueError("The publishing checkout metadata is not an ordinary file")
    head = subprocess.check_output(["git", "-C", str(checkout), "rev-parse", "HEAD"]).decode().strip()
    if head != binding["commit"]:
        raise ValueError("The publishing checkout differs from the reviewed commit")
    if subprocess.check_output(["git", "-C", str(checkout), "status", "--porcelain", "--untracked-files=all"]):
        raise ValueError("The publishing checkout must remain clean")
    _project_version(project.read_bytes(), binding["tag"])


def read_github_api(path: str) -> dict[str, Any]:
    try:
        value = json.loads(
            subprocess.check_output(["gh", "api", "--hostname", "github.com", path], stderr=subprocess.PIPE)
        )
    except (subprocess.SubprocessError, json.JSONDecodeError):
        raise ValueError("Published release metadata could not be read") from None
    if not isinstance(value, dict):
        raise ValueError("Invalid GitHub release metadata")
    return value


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--expected-commit", required=True)
    parser.add_argument("--previous-tag-object")
    parser.add_argument("--previous-release-id")
    parser.add_argument("--checkout", type=Path)
    args = parser.parse_args()
    if bool(args.previous_tag_object) != bool(args.previous_release_id):
        raise SystemExit("Both previous publication identities are required")
    previous = (
        {"tag_object": args.previous_tag_object, "release_id": args.previous_release_id}
        if args.previous_tag_object
        else None
    )
    result = verify_release(args.repository, args.tag, args.expected_commit, read_github_api, previous=previous)
    if args.checkout is not None:
        verify_checkout(args.checkout, result)
    if output := os.environ.get("GITHUB_OUTPUT"):
        with Path(output).open("a") as handle:
            handle.writelines(f"{name}={value}\n" for name, value in result.items())
    print(json.dumps(result, sort_keys=True))
