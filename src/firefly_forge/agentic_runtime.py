# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Native FireflyAgent execution with capability-scoped workspace file tools."""

from __future__ import annotations

import os
import stat
from collections.abc import Awaitable, Callable
from contextlib import suppress
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from firefly_forge.profiles import build_instruction


class RuntimeReport(BaseModel):
    summary: str


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
    def __init__(self, workspace: Path, *, max_file_bytes: int = 1_000_000) -> None:
        if workspace.is_symlink() or not workspace.is_dir():
            raise ValueError("Workspace must be a real directory")
        self.root = workspace.resolve()
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

    async def write_file(self, path: str, content: str) -> str:
        """Write complete UTF-8 source to a confined relative path, creating parent folders."""
        parts = self._parts(path)
        data = content.encode("utf-8")
        if len(data) > self.max_file_bytes:
            raise ValueError("File exceeds the workspace write limit")
        parent = self._parent_fd(parts, create=True)
        try:
            try:
                fd = os.open(parts[-1], os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o644, dir_fd=parent)
            except OSError as exc:
                raise ValueError("Cannot write file; symlinks are unavailable") from exc
            with os.fdopen(fd, "wb") as target:
                if not stat.S_ISREG(os.fstat(target.fileno()).st_mode):
                    raise ValueError("Only regular files are writable")
                if os.fstat(target.fileno()).st_nlink != 1:
                    raise ValueError("Hardlinked files are unavailable")
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


async def execute_agentic(
    workspace: Path,
    brief: str,
    backend: str,
    frontend: str,
    emit: Callable[[str, str], Awaitable[None]],
    *,
    model: Any = None,
    allow_output_token_cap: bool = True,
) -> tuple[str, dict[str, Any]]:
    from fireflyframework_agentic.agents import FireflyAgent
    from fireflyframework_agentic.config import FireflyAgenticConfig
    from fireflyframework_agentic.models import ModelOptions
    from fireflyframework_agentic.tools import firefly_tool
    from pydantic_ai.usage import UsageLimits

    tools = WorkspaceTools(workspace)

    async def read_source(path: str) -> str:
        """Read a confined project source file; unavailable paths return actionable error feedback."""
        try:
            return await tools.read_file(path)
        except (ValueError, FileNotFoundError, UnicodeDecodeError) as exc:
            return f"Unavailable file {path}: {exc}. Use list_files to find existing source paths."

    async def write_source(path: str, content: str) -> str:
        """Write the complete content of a project source file inside the isolated workspace."""
        result = await tools.write_file(path, content)
        await emit("file", result)
        return result

    selected_model = model or os.environ.get("FORGE_AGENTIC_MODEL", "openai-responses:gpt-6-luna")
    agent: FireflyAgent[None, RuntimeReport] = FireflyAgent(
        name="forge-builder",
        model=selected_model,
        instructions=build_instruction(brief, backend, frontend)
        + (
            "\nUse list_files/read_file/write_file to implement the product. "
            "Write actual source and tests before returning the summary; the factory runs ind"
            "ependent verification afterwards."
        ),
        output_type=RuntimeReport,
        auto_register=False,
        config=FireflyAgenticConfig(default_temperature=None),
        model_options=(
            ModelOptions(max_tokens=int(os.environ.get("FORGE_AGENTIC_MAX_TOKENS", "16384")))
            if allow_output_token_cap
            else None
        ),
        tools=[
            firefly_tool("list_files", auto_register=False)(tools.list_files),
            firefly_tool("read_file", auto_register=False)(read_source),
            firefly_tool("write_file", auto_register=False)(write_source),
        ],
    )
    await emit("provider", "Firefly Agentic is generating source with confined workspace tools")
    result = await agent.run(
        brief,
        timeout=float(os.environ.get("FORGE_PROVIDER_TIMEOUT", "1800")),
        usage_limits=UsageLimits(request_limit=100, tool_calls_limit=300),
    )
    if not tools.files_written:
        raise RuntimeError("Firefly Agentic returned without writing any project source")
    usage = result.usage
    return result.output.summary, {
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "requests": usage.requests,
        "files_written": sorted(tools.files_written),
    }
