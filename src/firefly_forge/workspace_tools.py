# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Confined source tools and the private callbacks used by native agents."""

from __future__ import annotations

import hashlib
import os
import stat
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from firefly_forge.process import ProcessResult, execute

if TYPE_CHECKING:
    from firefly_forge.qa import BrowserJourney, EvidenceImage, JourneyEvidence, QAReference


@dataclass(frozen=True)
class ToolRuntimeContext:
    scope_root: Path
    repository_root: Path
    allowed_tools: frozenset[str] | None = None
    command_executor: Callable[[list[str], int], Awaitable[ProcessResult]] | None = None
    browser_runner: Callable[[str, int, int, str], Awaitable[dict[str, Any]]] | None = None
    internet_search: Callable[[str, int], Awaitable[dict[str, Any]]] | None = None
    internet_search_refresh: Callable[[str, int, bool, str | None], Awaitable[dict[str, Any]]] | None = None
    knowledge_lookup: Callable[[str, bool, str | None], Awaitable[dict[str, Any]]] | None = None
    external_mcp: Callable[[str, str, dict[str, Any]], Awaitable[dict[str, Any]]] | None = None
    credential_registry: Callable[[], dict[str, str]] = dict
    credential_admission: Callable[[Any], Awaitable[None]] | None = None
    browser_journey: Callable[[BrowserJourney], Awaitable[JourneyEvidence]] | None = None
    image_capability: dict[str, Any] = field(default_factory=dict)
    qa_references: tuple[dict[str, Any], ...] = ()
    reference_loader: Callable[[str], Awaitable[EvidenceImage]] | None = None
    reference_resolver: Callable[[BrowserJourney], Awaitable[tuple[QAReference, EvidenceImage] | None]] | None = None


def _private_name(name: str) -> bool:
    return (
        name
        in {
            ".git",
            ".npmrc",
            ".pypirc",
            ".ssh",
            ".forge",
            ".codex",
            ".claude",
            "credentials.json",
            "id_rsa",
            "id_ed25519",
        }
        or (name.startswith(".env") and not name.endswith((".example", ".sample", ".template")))
        or name.endswith((".pem", ".key"))
    )


class WorkspaceTools:
    def __init__(
        self, workspace: Path, *, repository_root: Path | None = None, max_file_bytes: int = 1_000_000
    ) -> None:
        if workspace.is_symlink() or not workspace.is_dir():
            raise ValueError("Workspace must be a real directory")
        self.root = workspace.resolve()
        repository = repository_root if repository_root is not None else workspace
        if repository.is_symlink() or not repository.is_dir():
            raise ValueError("Repository must be a real directory")
        self.repository_root = repository.resolve()
        if not self.root.is_relative_to(self.repository_root):
            raise ValueError("Workspace must be confined to the repository")
        self.max_file_bytes = max_file_bytes
        self.files_written: set[str] = set()

    def _parts(self, path: str) -> tuple[str, ...]:
        value = Path(path)
        if value.is_absolute() or not value.parts or ".." in value.parts:
            raise ValueError("Path must be relative and confined to the workspace")
        if any(_private_name(part) for part in value.parts):
            raise ValueError("Credential files and git metadata are unavailable")
        return value.parts

    def _parent_fd(self, parts: tuple[str, ...], *, create: bool) -> int:
        fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for part in parts[:-1]:
                if create:
                    with suppress(FileExistsError):
                        os.mkdir(part, dir_fd=fd)
                try:
                    child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                except OSError as exc:
                    raise ValueError("Path contains a symlink or invalid directory") from exc
                os.close(fd)
                fd = child
            return fd
        except BaseException:
            os.close(fd)
            raise

    async def read_file(self, path: str) -> str:
        """Read a UTF-8 source file relative to the workspace; no credentials or symlinks."""
        parts = self._parts(path)
        parent = self._parent_fd(parts, create=False)
        try:
            try:
                fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            except OSError as exc:
                raise ValueError("Cannot read file; symlinks are unavailable") from exc
            with os.fdopen(fd, "rb") as source:
                if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                    raise ValueError("Only regular files are readable")
                if os.fstat(source.fileno()).st_nlink != 1:
                    raise ValueError("Hardlinked files are unavailable")
                data = source.read(self.max_file_bytes + 1)
                if len(data) > self.max_file_bytes:
                    raise ValueError("File exceeds the workspace read limit")
                return data.decode("utf-8")
        finally:
            os.close(parent)

    async def write_file(self, path: str, content: str, *, expected_sha256: str | None = None) -> str:
        """Write complete UTF-8 source to a confined relative path, creating parent folders."""
        parts = self._parts(path)
        data = content.encode("utf-8")
        if len(data) > self.max_file_bytes:
            raise ValueError("File exceeds the workspace write limit")
        parent = self._parent_fd(parts, create=True)
        try:
            try:
                fd = os.open(parts[-1], os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o644, dir_fd=parent)
            except OSError as exc:
                raise ValueError("Cannot write file; symlinks are unavailable") from exc
            with os.fdopen(fd, "r+b") as target:
                if not stat.S_ISREG(os.fstat(target.fileno()).st_mode):
                    raise ValueError("Only regular files are writable")
                if os.fstat(target.fileno()).st_nlink != 1:
                    raise ValueError("Hardlinked files are unavailable")
                if expected_sha256 is not None:
                    previous = target.read(self.max_file_bytes + 1)
                    if hashlib.sha256(previous).hexdigest() != expected_sha256:
                        raise ValueError("Source hash changed before the edit could be written")
                    target.seek(0)
                target.truncate(0)
                target.write(data)
            self.files_written.add(path)
            return f"Wrote {path} ({len(data)} bytes)"
        finally:
            os.close(parent)

    async def list_files(self) -> list[str]:
        """List at most 2000 project source paths; exclude dependencies, credentials and symlinks."""
        output: list[str] = []
        skipped = {".git", ".env", ".npmrc", ".ssh", ".venv", "node_modules", "vendor", "__pycache__", "dist", "build"}
        for directory, folders, files in os.walk(self.root, followlinks=False):
            base = Path(directory)
            folders[:] = sorted(
                p for p in folders if p not in skipped and not _private_name(p) and not (base / p).is_symlink()
            )
            for name in sorted(files):
                path = base / name
                if name not in skipped and not _private_name(name) and not path.is_symlink():
                    output.append(str(path.relative_to(self.root)))
                    if len(output) >= 2000:
                        return output
        return output

    async def search_text(self, text: str, path_prefix: str = "", max_results: int = 50) -> dict[str, Any]:
        """Search literal text, returning bounded source lines and content hashes."""
        if not text or len(text) > 500 or "\x00" in text or "\n" in text:
            raise ValueError("Search text must contain 1..500 characters on one line")
        if type(max_results) is not int or not 1 <= max_results <= 100:
            raise ValueError("Search results must be bounded to 1..100")
        prefix = "/".join(self._parts(path_prefix)) if path_prefix else ""
        matches: list[dict[str, Any]] = []
        total = 0
        truncated = False
        for path in await self.list_files():
            if prefix and path != prefix and not path.startswith(prefix + "/"):
                continue
            try:
                content = await self.read_file(path)
            except (ValueError, OSError, UnicodeDecodeError):
                continue
            total += len(content.encode("utf-8"))
            if total > 16_000_000:
                truncated = True
                break
            digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
            for number, line in enumerate(content.splitlines(), 1):
                if text not in line:
                    continue
                if len(matches) == max_results:
                    truncated = True
                    break
                matches.append({"path": path, "line": number, "snippet": line[:300], "sha256": digest})
            if truncated:
                break
        return {"matches": matches, "truncated": truncated}

    async def edit_lines(
        self, path: str, start_line: int, end_line: int, replacement: str, expected_sha256: str
    ) -> dict[str, str]:
        """Replace an inclusive line range only when the source hash still matches."""
        if len(expected_sha256) != 64 or any(c not in "0123456789abcdef" for c in expected_sha256):
            raise ValueError("Expected source hash must be lowercase SHA256")
        content = await self.read_file(path)
        if hashlib.sha256(content.encode("utf-8")).hexdigest() != expected_sha256:
            raise ValueError("Source hash changed; read the file before editing again")
        lines = content.splitlines(keepends=True)
        if type(start_line) is not int or type(end_line) is not int or not 1 <= start_line <= end_line <= len(lines):
            raise ValueError("Line range must refer to existing source lines")
        updated = "".join(lines[: start_line - 1]) + replacement + "".join(lines[end_line:])
        await self.write_file(path, updated, expected_sha256=expected_sha256)
        return {"path": path, "sha256": hashlib.sha256(updated.encode("utf-8")).hexdigest()}

    async def git_diff(self) -> dict[str, Any]:
        """Read the component's source diff without hooks, external diff drivers or index writes."""
        component = self.root.relative_to(self.repository_root).as_posix()
        spec = f":(literal){component}" if component != "." else ":(top)*"
        base = [
            "git",
            "--no-optional-locks",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "diff.external=",
            "-c",
            "core.pager=cat",
            "--no-pager",
            "diff",
            "--no-ext-diff",
            "--no-textconv",
            "--no-color",
        ]
        env = {"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_OPTIONAL_LOCKS": "0"}
        listed = await execute(base + ["--name-only", "-z", "HEAD", "--", spec], self.repository_root, 30, env=env)
        if listed.exit_code:
            raise ValueError("Source diff requires a repository with an existing HEAD")
        paths: list[str] = []
        for name in listed.output.split("\x00"):
            if not name:
                continue
            candidate = Path(name)
            if candidate.is_absolute() or ".." in candidate.parts or any(_private_name(p) for p in candidate.parts):
                continue
            full = self.repository_root / candidate
            if not full.is_relative_to(self.root) or full.is_symlink():
                continue
            if full.exists():
                try:
                    await self.read_file(full.relative_to(self.root).as_posix())
                except (ValueError, OSError, UnicodeDecodeError):
                    continue
            paths.append(name)
        if not paths:
            return {"diff": "", "truncated": False}
        result = await execute(
            base + ["HEAD", "--"] + [f":(literal){path}" for path in paths[:100]],
            self.repository_root,
            30,
            env=env,
        )
        if result.exit_code:
            raise ValueError("Cannot read component source diff")
        return {"diff": result.output[:128_000], "truncated": len(paths) > 100 or len(result.output) > 128_000}


_TOOL_DESCRIPTIONS = {
    "list_files": "List confined project source paths",
    "read_file": "Read bounded UTF-8 project source",
    "write_file": "Write confined project source",
    "search_text": "Search literal source text with file hashes",
    "edit_lines": "Edit lines with an expected source hash",
    "git_diff": "Read the component source diff without Git effects",
    "execute_command": "Execute bounded commands through the selected execution profile",
    "browser_check": "Capture declared viewports and inspect the isolated project browser",
    "browser_journey": "Exercise bounded user journeys and return DOM and screenshot evidence",
    "internet_search": "Search the configured server-mediated search provider",
    "external_mcp": "Read an exact reviewed external tool within a project and repository grant",
    "knowledge_lookup": "Inspect scoped references and exact verified local outcomes without a web-search key",
}


def tool_catalog(runtime_snapshot: dict[str, Any] | None = None, provider: str = "agentic") -> list[dict[str, Any]]:
    snapshot = runtime_snapshot or {}
    result = []
    for identifier, description in _TOOL_DESCRIPTIONS.items():
        optional = identifier in {
            "execute_command",
            "browser_check",
            "browser_journey",
            "internet_search",
            "external_mcp",
            "knowledge_lookup",
        }
        available = not optional or bool(snapshot.get(identifier, False))
        supported = provider in {"agentic", "bridge", "api"}
        result.append(
            {
                "id": identifier,
                "name": identifier.replace("_", " "),
                "description": description,
                "availability": "unsupported" if not supported else "available" if available else "not_configured",
                "reason": (
                    "Official CLI tools use their own policies; Python tool grants are advisory"
                    if not supported
                    else "Configured native workspace capability"
                    if available
                    else "An explicit execution, browser or search configuration is required"
                ),
                "providers": ["agentic", "bridge"],
                "requires": [identifier] if optional else [],
            }
        )
    return result
