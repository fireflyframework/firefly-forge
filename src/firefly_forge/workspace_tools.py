# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Confined source tools and the private callbacks used by native agents."""

from __future__ import annotations

import asyncio
import base64
import codecs
import hashlib
import json
import os
import stat
import sys
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, BinaryIO

from firefly_forge.process import ProcessResult, execute
from firefly_forge.prompts import prompt_text

if TYPE_CHECKING:
    from firefly_forge.diagnostic_recipes import PythonProfileRequest, PythonTraceRequest
    from firefly_forge.php_diagnostics import PHPProfileRequest
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
    read_run_evidence: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]] | None = None
    knowledge_lookup: Callable[[str, bool, str | None], Awaitable[dict[str, Any]]] | None = None
    external_mcp: Callable[[str, str, dict[str, Any]], Awaitable[dict[str, Any]]] | None = None
    credential_registry: Callable[[], dict[str, str]] = dict
    credential_admission: Callable[[Any], Awaitable[None]] | None = None
    browser_journey: Callable[[BrowserJourney], Awaitable[JourneyEvidence]] | None = None
    image_capability: dict[str, Any] = field(default_factory=dict)
    qa_references: tuple[dict[str, Any], ...] = ()
    reference_loader: Callable[[str], Awaitable[EvidenceImage]] | None = None
    reference_resolver: Callable[[BrowserJourney], Awaitable[tuple[QAReference, EvidenceImage] | None]] | None = None
    python_profile: Callable[[PythonProfileRequest], Awaitable[dict[str, Any]]] | None = None
    python_trace: Callable[[PythonTraceRequest], Awaitable[dict[str, Any]]] | None = None
    php_profile: Callable[[PHPProfileRequest], Awaitable[dict[str, Any]]] | None = None


PRIVATE_BASENAMES = frozenset(
    {
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
)
PUBLIC_ENV_SUFFIXES = (".example", ".sample", ".template")
PRIVATE_KEY_SUFFIXES = (".pem", ".key")


def _private_name(name: str) -> bool:
    folded = name.casefold()
    return (
        folded in PRIVATE_BASENAMES
        or (folded.startswith(".env") and not name.endswith(PUBLIC_ENV_SUFFIXES))
        or folded.endswith(PRIVATE_KEY_SUFFIXES)
    )


def _added_source_diff(path: str, content: str, mode: str) -> str:
    def quoted(prefix: str) -> str:
        return (
            '"'
            + "".join(
                chr(value) if 32 <= value < 127 and value not in {34, 92} else f"\\{value:03o}"
                for value in (prefix + path).encode("utf-8")
            )
            + '"'
        )

    old, new = quoted("a/"), quoted("b/")
    header = f"diff --git {old} {new}\nnew file mode {mode}\n"
    if not content:
        return header
    lines = content.split("\n")
    terminated = content.endswith("\n")
    if terminated:
        lines.pop()
    extent = "1" if len(lines) == 1 else f"1,{len(lines)}"
    result = header + f"--- /dev/null\n+++ {new}\n@@ -0,0 +{extent} @@\n"
    result += "".join("+" + line + "\n" for line in lines)
    return result if terminated else result + "\\ No newline at end of file\n"


def _descriptor_path(fd: int) -> Path:
    """Use the opened object's spelling; resolve() preserves APFS case aliases."""
    if sys.platform == "darwin":
        import fcntl

        # Darwin F_GETPATH returns the kernel's path without reading file contents.
        value = fcntl.fcntl(fd, 50, bytes(1024)).split(b"\x00", 1)[0]
        return Path(os.fsdecode(value))
    if sys.platform.startswith("linux"):
        return Path(os.readlink(f"/proc/self/fd/{fd}"))
    raise ValueError("Verified workspace descriptor paths are unavailable on this platform")


def _directory_path(path: Path) -> Path:
    fd = os.open(path.resolve(), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        return _descriptor_path(fd)
    finally:
        os.close(fd)


def _source_stamp(info: os.stat_result) -> tuple[int, ...]:
    return tuple(
        getattr(info, "st_" + key) for key in ("dev", "ino", "uid", "mode", "nlink", "size", "mtime_ns", "ctime_ns")
    )


def _source_root_identity(root: Path) -> tuple[int, int, int]:
    info = root.lstat()
    if not stat.S_ISDIR(info.st_mode) or root.is_symlink() or info.st_uid != os.geteuid():
        raise ValueError("Workspace must retain its owned directory identity")
    return info.st_dev, info.st_ino, info.st_uid


def _page_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


class WorkspaceTools:
    def __init__(
        self,
        workspace: Path,
        *,
        repository_root: Path | None = None,
        max_file_bytes: int = 1_000_000,
        max_page_bytes: int = 64000,
    ) -> None:
        if workspace.is_symlink() or not workspace.is_dir():
            raise ValueError("Workspace must be a real directory")
        self.root = _directory_path(workspace)
        repository = repository_root if repository_root is not None else workspace
        if repository.is_symlink() or not repository.is_dir():
            raise ValueError("Repository must be a real directory")
        self.repository_root = _directory_path(repository)
        if not self.root.is_relative_to(self.repository_root):
            raise ValueError("Workspace must be confined to the repository")
        self._source_root = _source_root_identity(self.root)
        if type(max_page_bytes) is not int or max_page_bytes < 1:
            raise ValueError("Source response window must be a positive byte budget")
        self.max_page_bytes = max_page_bytes
        self.max_file_bytes = max_file_bytes
        self.files_written: set[str] = set()

    def _parts(self, path: str) -> tuple[str, ...]:
        value = Path(path)
        if value.is_absolute() or not value.parts or ".." in value.parts:
            raise ValueError("Path must be relative and confined to the workspace")
        if any(_private_name(part) for part in value.parts):
            raise ValueError("Credential files and git metadata are unavailable")
        return value.parts

    def _check_descriptor(self, fd: int) -> None:
        actual = _descriptor_path(fd)
        if not actual.is_relative_to(self.root):
            raise ValueError("Opened path is unavailable outside the workspace")
        if any(_private_name(part) for part in actual.relative_to(self.root).parts):
            raise ValueError("Credential files and git metadata are unavailable")

    def _parent_fd(self, parts: tuple[str, ...], *, create: bool) -> int:
        fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            self._check_descriptor(fd)
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
                self._check_descriptor(fd)
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
                self._check_descriptor(source.fileno())
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
                self._check_descriptor(target.fileno())
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

    @contextmanager
    def _source_file(self, path: str, expected: tuple[int, ...] | None = None) -> Iterator[BinaryIO]:
        parts = self._parts(path)
        parent = self._parent_fd(parts, create=False)
        try:
            try:
                fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            except OSError:
                raise ValueError("Selected source is unreadable or linked") from None
            with os.fdopen(fd, "rb") as source:
                info = os.fstat(source.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise ValueError("Selected source requires regular files without links or aliases")
                if info.st_uid != os.geteuid():
                    raise ValueError("Selected source ownership changed")
                self._check_descriptor(source.fileno())
                if _descriptor_path(source.fileno()).as_posix() != (self.root / path).as_posix():
                    raise ValueError("Selected source has a case or path alias")
                before = _source_stamp(info)
                if expected is not None and before != expected:
                    raise ValueError("Selected source changed before reading")
                yield source
                if _source_stamp(os.fstat(source.fileno())) != before:
                    raise ValueError("Selected source changed while reading")
        finally:
            os.close(parent)

    def _source_absence(self, path: str) -> tuple[str, tuple[int, ...]] | None:
        parts = self._parts(path)
        parent = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            self._check_descriptor(parent)
            for ordinal, name in enumerate(parts):
                try:
                    os.stat(name, dir_fd=parent, follow_symlinks=False)
                except FileNotFoundError:
                    return "/".join(parts[: ordinal + 1]), _source_stamp(os.fstat(parent))
                if ordinal == len(parts) - 1:
                    return None
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                os.close(parent)
                parent = child
                if os.fstat(parent).st_uid != os.geteuid():
                    raise ValueError("Selected source directory ownership changed")
                self._check_descriptor(parent)
                if _descriptor_path(parent) != self.root.joinpath(*parts[: ordinal + 1]):
                    raise ValueError("Selected source directory has a case or path alias")
        except OSError:
            raise ValueError("Selected source is unreadable or linked") from None
        finally:
            os.close(parent)
        return None

    async def _inventory(self, prefix: str = "") -> tuple[list[tuple[str, tuple[int, ...]]], str]:
        prefix = "/".join(self._parts(prefix)) if prefix else ""
        if _source_root_identity(self.root) != self._source_root:
            raise ValueError("Workspace identity changed")
        selected: dict[str, tuple[int, ...]] = {}
        absent: dict[str, tuple[str, str | None, tuple[str, tuple[int, ...]]]] = {}
        rule_hash = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()

        async def accept(path: str) -> None:
            if prefix and path != prefix and not path.startswith(prefix + "/"):
                return
            if path in selected:
                raise ValueError("Selected source contains duplicate paths")
            with self._source_file(path) as source:
                selected[path] = _source_stamp(os.fstat(source.fileno()))
            await asyncio.sleep(0)

        metadata = self.repository_root / ".git"
        if metadata.exists() or metadata.is_symlink():
            if metadata.is_symlink() or not (metadata.is_dir() or metadata.is_file()):
                raise ValueError("Git metadata is unavailable; filesystem fallback is forbidden")
            from firefly_forge import checkpoint_source_identity

            rule_hash += hashlib.sha256(Path(checkpoint_source_identity.__file__).read_bytes()).hexdigest()

            async def receive(entry: checkpoint_source_identity.SourceEntry) -> None:
                if prefix and entry.path != prefix and not entry.path.startswith(prefix + "/"):
                    return
                if entry.path in selected or entry.path in absent:
                    raise ValueError("Selected source contains duplicate paths")
                if entry.mode is not None:
                    witness = self._source_absence(entry.path)
                    if witness is not None:
                        absent[entry.path] = (entry.mode, entry.object_id, witness)
                        await asyncio.sleep(0)
                        return
                await accept(entry.path)

            await checkpoint_source_identity.stream_source_entries(self.root, receive)
            for absent_path, (_, _, missing_witness) in absent.items():
                if self._source_absence(absent_path) != missing_witness:
                    raise ValueError("Tracked source absence changed during inventory")
        else:
            skipped = {".git", ".venv", "node_modules", "vendor", "__pycache__", "dist", "build"}

            def unreadable_directory(error: OSError) -> None:
                raise ValueError("Selected source directory is unreadable") from None

            for directory, folders, files in os.walk(self.root, followlinks=False, onerror=unreadable_directory):
                base = Path(directory)
                folders[:] = sorted(
                    name
                    for name in folders
                    if name.casefold() not in skipped and not _private_name(name) and not (base / name).is_symlink()
                )
                for name in sorted(files):
                    path = base / name
                    if name.casefold() not in skipped and not _private_name(name) and not path.is_symlink():
                        await accept(path.relative_to(self.root).as_posix())
        rows = sorted(selected.items())
        view = hashlib.sha256()
        view.update(json.dumps([self._scope_id(), prefix, rule_hash], separators=(",", ":")).encode())
        for row in rows:
            view.update(json.dumps(row, ensure_ascii=True, separators=(",", ":")).encode() + b"\n")
        for absent_path, absence_record in sorted(absent.items()):
            view.update(
                json.dumps(["deleted", absent_path, absence_record], ensure_ascii=True, separators=(",", ":")).encode()
                + b"\n"
            )
        if _source_root_identity(self.root) != self._source_root:
            raise ValueError("Workspace identity changed")
        return rows, view.hexdigest()

    def _scope_id(self) -> str:
        value = [
            _source_root_identity(self.repository_root),
            self._source_root,
            self.root.relative_to(self.repository_root).as_posix(),
        ]
        return hashlib.sha256(json.dumps(value, separators=(",", ":")).encode()).hexdigest()

    def _page_position(self, cursor: str | None, kind: str, query: Any, view: str) -> int:
        if cursor is None:
            return 0
        if type(cursor) is not str or len(cursor) > 4096:
            raise ValueError("Invalid source page cursor")
        try:
            raw = base64.b64decode(cursor.encode("ascii"), altchars=b"-_", validate=True)
            value = json.loads(raw)
            if base64.urlsafe_b64encode(_page_json(value)).decode() != cursor:
                raise ValueError
            if set(value) != {"version", "kind", "scope", "query", "view", "position"}:
                raise ValueError
            if (
                type(value["version"]) is not int
                or value["version"] != 1
                or value["kind"] != kind
                or value["scope"] != self._scope_id()
            ):
                raise ValueError
            if value["query"] != hashlib.sha256(_page_json(query)).hexdigest():
                raise ValueError
            if type(value["position"]) is not int or not 0 <= value["position"] <= 2**53 - 1:
                raise ValueError
        except (ValueError, TypeError, KeyError, UnicodeError):
            raise ValueError("Source page cursor or scope differs") from None
        if value["view"] != view:
            raise ValueError("Source page view changed; start a fresh cursor")
        return int(value["position"])

    def _page_cursor(self, kind: str, query: Any, view: str, position: int) -> str:
        value = {
            "version": 1,
            "kind": kind,
            "scope": self._scope_id(),
            "query": hashlib.sha256(_page_json(query)).hexdigest(),
            "view": view,
            "position": position,
        }
        return base64.urlsafe_b64encode(_page_json(value)).decode()

    async def list_files(self) -> list[str]:
        """Return the exhaustive internal source inventory; model callers use explicit pages."""
        rows, _ = await self._inventory()
        return [name for name, _ in rows]

    async def list_files_page(
        self, path_prefix: str = "", cursor: str | None = None, page_size: int = 100
    ) -> dict[str, Any]:
        """Read a bounded source page; continue its cursor until complete is true."""
        if type(page_size) is not int or not 1 <= page_size <= 1000:
            raise ValueError("Source page size must be an integer from 1 to 1000")
        rows, view = await self._inventory(path_prefix)
        query = [path_prefix, page_size, self.max_page_bytes]
        position = self._page_position(cursor, "list", query, view)
        if position > len(rows):
            raise ValueError("Source page cursor exceeds its view")

        def result(files: list[str]) -> dict[str, Any]:
            end = position + len(files)
            return {
                "version": 1,
                "files": files,
                "view_sha256": view,
                "complete": end == len(rows),
                "next_cursor": self._page_cursor("list", query, view, end) if end < len(rows) else None,
            }

        files: list[str] = []
        for name, _ in rows[position : position + page_size]:
            candidate = [*files, name]
            if len(json.dumps(result(candidate)).encode()) > self.max_page_bytes:
                if not files:
                    raise ValueError("Selected filename and cursor exceed one source response window")
                break
            files = candidate
        value = result(files)
        if (await self._inventory(path_prefix))[1] != view:
            raise ValueError("Source page selection changed while listing")
        if len(json.dumps(value).encode()) > self.max_page_bytes:
            raise ValueError("Source page metadata exceeds the response window")
        return value

    async def source_manifest(self) -> dict[str, str]:
        """Hash all eligible bytes, including binary and large files, without a page or text cutoff."""
        rows, view = await self._inventory()
        manifest: dict[str, str] = {}
        for name, expected in rows:
            digest = hashlib.sha256()
            with self._source_file(name, expected) as source:
                while chunk := source.read(65536):
                    digest.update(chunk)
                    await asyncio.sleep(0)
            manifest[name] = digest.hexdigest()
        if (await self._inventory())[1] != view:
            raise ValueError("Source manifest selection changed while hashing")
        return manifest

    async def _search_file(
        self, path: str, expected: tuple[int, ...], text: str, skip: int, window: int
    ) -> tuple[list[dict[str, Any]], int, str]:
        digest, decoder = hashlib.sha256(), codecs.getincrementaldecoder("utf-8")("strict")
        classification = "text"
        matches: list[dict[str, Any]] = []
        count, number, length = 0, 1, 0
        found, tail, snippet = False, "", ""

        def line() -> None:
            nonlocal count, number, length, found, tail, snippet
            if found:
                if count >= skip and len(matches) < window:
                    matches.append(
                        {"path": path, "line": number, "snippet": snippet, "snippet_truncated": length > 300}
                    )
                count += 1
            number += 1
            length, found, tail, snippet = 0, False, "", ""

        def consume(value: str) -> None:
            nonlocal length, found, tail, snippet
            pieces = value.split("\n")
            for i, piece in enumerate(pieces):
                if i:
                    line()
                candidate = tail + piece
                found = found or text in candidate
                tail = candidate[-(len(text) - 1) :] if len(text) > 1 else ""
                snippet += piece[: max(0, 300 - len(snippet))]
                length += len(piece)

        with self._source_file(path, expected) as source:
            while chunk := source.read(65536):
                digest.update(chunk)
                if b"\x00" in chunk:
                    classification = "binary"
                if classification == "text":
                    try:
                        consume(decoder.decode(chunk))
                    except UnicodeDecodeError:
                        classification = "invalid_utf8"
                await asyncio.sleep(0)
            if classification == "text":
                try:
                    consume(decoder.decode(b"", final=True))
                    line()
                except UnicodeDecodeError:
                    classification = "invalid_utf8"
        if classification != "text":
            return [], 0, classification
        for match in matches:
            match["sha256"] = digest.hexdigest()
        return matches, count, classification

    async def search_text(
        self, text: str, path_prefix: str = "", max_results: int = 50, cursor: str | None = None
    ) -> dict[str, Any]:
        """Search complete selected UTF-8 source, with finite pages and explicit binary classification."""
        if not text or len(text) > 500 or "\x00" in text or "\n" in text:
            raise ValueError("Search text must contain 1..500 characters on one line")
        if type(max_results) is not int or not 1 <= max_results <= 100:
            raise ValueError("Search results must be bounded to 1..100")
        rows, view = await self._inventory(path_prefix)
        query = [path_prefix, text, max_results, self.max_page_bytes]
        position = self._page_position(cursor, "search", query, view)
        matches: list[dict[str, Any]] = []
        count = 0
        classifications = {"text": 0, "binary": 0, "invalid_utf8": 0}
        for name, expected in rows:
            found, total, kind = await self._search_file(
                name, expected, text, max(0, position - count), max_results - len(matches)
            )
            classifications[kind] += 1
            count += total
            matches.extend(found)
        if position > count:
            raise ValueError("Search cursor exceeds its source view")
        if (await self._inventory(path_prefix))[1] != view:
            raise ValueError("Search source view changed while reading")

        def result(items: list[dict[str, Any]]) -> dict[str, Any]:
            end = position + len(items)
            more = end < count
            return {
                "version": 1,
                "matches": items,
                "truncated": more,
                "complete": not more,
                "classifications": classifications,
                "view_sha256": view,
                "next_cursor": self._page_cursor("search", query, view, end) if more else None,
            }

        bounded: list[dict[str, Any]] = []
        for match in matches:
            candidate = [*bounded, match]
            if len(json.dumps(result(candidate)).encode()) > self.max_page_bytes:
                if not bounded:
                    raise ValueError("Selected match and cursor exceed one source response window")
                break
            bounded = candidate
        value = result(bounded)
        if len(json.dumps(value).encode()) > self.max_page_bytes:
            raise ValueError("Search page metadata exceeds the response window")
        return value

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
        git = [
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
        ]
        base = git + [
            "diff",
            "--no-ext-diff",
            "--no-textconv",
            "--no-color",
        ]
        env = {"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_OPTIONAL_LOCKS": "0"}
        listed = await execute(base + ["--name-only", "-z", "HEAD", "--", spec], self.repository_root, 30, env=env)
        if listed.exit_code:
            raise ValueError("Source diff requires a repository with an existing HEAD")
        untracked = await execute(
            git + ["ls-files", "--others", "--exclude-standard", "-z", "--", spec], self.repository_root, 30, env=env
        )
        if untracked.exit_code:
            raise ValueError("Cannot read component source diff")
        added = set(untracked.output.split("\x00")) - {""}
        paths: list[str] = []
        for name in sorted(set(listed.output.split("\x00")) | added):
            if not name:
                continue
            candidate = Path(name)
            if candidate.is_absolute() or ".." in candidate.parts or any(_private_name(p) for p in candidate.parts):
                continue
            full = self.repository_root / candidate
            if not full.is_relative_to(self.root) or full.is_symlink():
                continue
            if name in added and not full.exists():
                continue
            if full.exists():
                try:
                    await self.read_file(full.relative_to(self.root).as_posix())
                except (ValueError, OSError, UnicodeDecodeError):
                    continue
            paths.append(name)
        if not paths:
            return {"diff": "", "truncated": False}
        tracked = [path for path in paths[:100] if path not in added]
        output = ""
        if tracked:
            result = await execute(
                base + ["HEAD", "--"] + [f":(literal){path}" for path in tracked], self.repository_root, 30, env=env
            )
            if result.exit_code:
                raise ValueError("Cannot read component source diff")
            output = result.output
        for name in paths[:100]:
            if name not in added or len(output) > 128_000:
                continue
            relative = (self.repository_root / name).relative_to(self.root).as_posix()
            with self._source_file(relative) as source:
                data = source.read(self.max_file_bytes + 1)
                if len(data) > self.max_file_bytes:
                    raise ValueError("File exceeds the workspace read limit")
                mode = "100755" if os.fstat(source.fileno()).st_mode & stat.S_IXUSR else "100644"
                content = data.decode("utf-8")
            output += _added_source_diff(name, content, mode)
        return {"diff": output[:128_000], "truncated": len(paths) > 100 or len(output) > 128_000}


_TOOL_DESCRIPTIONS = {
    "list_files": prompt_text("tools/list_files", version="1.0.1"),
    "read_file": "Read bounded UTF-8 project source",
    "write_file": "Write confined project source",
    "search_text": prompt_text("tools/search_text", version="1.0.1"),
    "edit_lines": "Edit lines with an expected source hash",
    "git_diff": "Read the component source diff without Git effects",
    "execute_command": "Execute bounded commands through the selected execution profile",
    "browser_check": "Capture declared viewports and inspect the isolated project browser",
    "browser_journey": "Exercise bounded user journeys and return DOM and screenshot evidence",
    "internet_search": "Search the configured server-mediated search provider",
    "external_mcp": "Read an exact reviewed external tool within a project and repository grant",
    "read_run_evidence": prompt_text("tools/read_run_evidence"),
    "knowledge_lookup": "Inspect scoped references and exact verified local outcomes without a web-search key",
    "profile_python": "Observe bounded Python CPU profiling through the frozen isolated command runner",
    "trace_python": "Observe best-effort target Python line counts through the frozen isolated command runner",
    "profile_php": "Observe bounded PHP runtime metrics or an explicitly selected preinstalled Xdebug profile",
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
            "read_run_evidence",
            "profile_python",
            "trace_python",
            "profile_php",
        }
        available = not optional or bool(snapshot.get(identifier, False))
        if identifier in {"profile_python", "trace_python", "profile_php"}:
            available = available and bool(snapshot.get("execute_command", False))
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
                "requires": ["execute_command", identifier]
                if identifier in {"profile_python", "trace_python", "profile_php"}
                else [identifier]
                if optional
                else [],
            }
        )
    return result
