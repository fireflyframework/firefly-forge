# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Credential-free official release discovery and immutable Git-object reads."""

from __future__ import annotations

import hashlib
import json
import re
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import PurePosixPath
from typing import Any, Self
from urllib.parse import quote, urlsplit

import httpx

from firefly_forge.framework_knowledge_models import (
    CURRENT_COLLECTOR_REVISION,
    OFFICIAL_REPOSITORIES,
    FrameworkName,
    FrameworkReleaseV1,
    framework_version,
    validate_collector_revision,
)
from firefly_forge.product_provenance import content_digest

Data = dict[str, Any]
_OID = re.compile(r"^[a-f0-9]{40}$")
Spend = Callable[[int, int], Awaitable[None]]


def git_object_digest(kind: str, value: bytes) -> str:
    return hashlib.sha1(kind.encode() + b" " + str(len(value)).encode() + b"\0" + value).hexdigest()


def source_kind(path: str, *, collector_revision: object = CURRENT_COLLECTOR_REVISION) -> str | None:
    revision = validate_collector_revision(collector_revision)
    value = PurePosixPath(path)
    if value.name.lower() in {"license", "license.md", "license.txt", "copying", "notice"}:
        return "license"
    text = {".md", ".rst", ".txt", ".adoc", ".mmd"}
    binary = {
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".ico",
        ".webp",
        ".avif",
        ".woff",
        ".woff2",
        ".ttf",
        ".otf",
        ".zip",
        ".gz",
        ".tgz",
        ".bz2",
        ".xz",
        ".7z",
        ".tar",
        ".pdf",
        ".mp4",
        ".mov",
        ".mp3",
        ".wav",
        ".sqlite",
        ".db",
        ".so",
        ".dll",
        ".dylib",
        ".wasm",
        ".exe",
        ".bin",
        ".jar",
        ".phar",
        ".npz",
        ".npy",
        ".parquet",
    }
    if value.suffix.lower() in binary or (revision >= 2 and value.suffix.lower() == ".svg"):
        return None
    if {part.lower() for part in value.parts} & {
        "example",
        "examples",
        "samples",
        "snippets",
        "tests",
        "stubs",
        "notebooks",
    }:
        return "example"
    if value.suffix.lower() in text:
        return "document"
    return "api" if value.suffix.lower() in {".py", ".pyi", ".php"} else "configuration"


@dataclass(frozen=True)
class OfficialSourceEntry:
    path: str
    oid: str
    size: int
    mode: str
    kind: str | None
    exclusion_reason: str | None = None


class OfficialRateLimited(ValueError):
    def __init__(self, retry_at: str) -> None:
        super().__init__("Official upstream rate limit reached; observe after the retained cooldown")
        self.retry_at = retry_at


class OfficialFrameworkClient:
    def __init__(
        self,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        spend: Spend | None = None,
        assert_current: Callable[[], Awaitable[None]] | None = None,
        load_tree: Callable[[FrameworkName, str], Awaitable[list[Data] | None]] | None = None,
        save_tree: Callable[[FrameworkName, str, list[Data]], Awaitable[None]] | None = None,
        collector_revision: object = CURRENT_COLLECTOR_REVISION,
        chunk_bytes: int = 65536,
        metadata_bytes: int = 64 * 1024**2,
    ) -> None:
        if chunk_bytes <= 0 or metadata_bytes <= 0:
            raise ValueError("Official source response windows must be positive")
        self.collector_revision = validate_collector_revision(collector_revision)
        self.spend, self.assert_current = spend, assert_current
        self.load_tree, self.save_tree = load_tree, save_tree
        self.chunk_bytes, self.metadata_bytes = chunk_bytes, metadata_bytes
        self.trees: dict[tuple[FrameworkName, str], list[Data]] = {}
        self.http = httpx.AsyncClient(
            transport=transport,
            timeout=30,
            follow_redirects=False,
            trust_env=False,
            headers={"User-Agent": "firefly-forge-framework-knowledge"},
        )

    async def __aenter__(self) -> Self:
        await self.http.__aenter__()
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        await self.http.__aexit__(exc_type, exc, traceback)

    async def _tick(self, requests: int = 0, byte_count: int = 0) -> None:
        if self.assert_current is not None:
            await self.assert_current()
        if self.spend is not None:
            await self.spend(requests, byte_count)

    @staticmethod
    def _repository(framework: FrameworkName) -> str:
        if framework not in OFFICIAL_REPOSITORIES:
            raise ValueError("Choose an official supported framework")
        return OFFICIAL_REPOSITORIES[framework]

    def _api(self, framework: FrameworkName, path: str) -> str:
        return "https://api.github.com/repos/" + self._repository(framework) + "/" + path

    async def _stream(self, url: str) -> AsyncIterator[bytes]:
        target = urlsplit(url)
        if (
            target.scheme != "https"
            or target.hostname not in {"api.github.com", "raw.githubusercontent.com"}
            or target.username
            or target.password
            or target.port not in {None, 443}
        ):
            raise ValueError("Official upstream requires an exact public HTTPS origin")
        await self._tick(requests=1)
        self.http.cookies.clear()
        async with self.http.stream("GET", url) as response:
            self.http.cookies.clear()
            if response.status_code == 429 or (
                response.status_code == 403 and response.headers.get("x-ratelimit-remaining") == "0"
            ):
                try:
                    retry = datetime.fromtimestamp(int(response.headers["x-ratelimit-reset"]), UTC)
                except (KeyError, ValueError, OverflowError):
                    try:
                        seconds = max(1, int(response.headers.get("retry-after", "60")))
                    except ValueError:
                        seconds = 60
                    retry = datetime.now(UTC) + timedelta(seconds=seconds)
                raise OfficialRateLimited(retry.isoformat())
            if response.status_code != 200:
                raise ValueError("Official upstream request failed or attempted a redirect")
            async for chunk in response.aiter_bytes(self.chunk_bytes):
                await self._tick(byte_count=len(chunk))
                yield chunk
        await self._tick()

    async def _json(self, url: str) -> object:
        data = bytearray()
        async for chunk in self._stream(url):
            if len(data) + len(chunk) > self.metadata_bytes:
                raise ValueError("Official metadata exceeds its configured response window")
            data.extend(chunk)
        try:
            value: object = json.loads(data)
            return value
        except (ValueError, UnicodeError) as exc:
            raise ValueError("Official upstream returned invalid metadata") from exc

    @staticmethod
    def _verified_tree(oid: str, entries: object) -> list[Data]:
        if not isinstance(entries, list) or any(not isinstance(entry, dict) for entry in entries):
            raise ValueError("Official tree entries are unavailable")
        raw = bytearray()
        names: set[str] = set()
        modes = {"040000": "tree", "100644": "blob", "100755": "blob", "120000": "blob", "160000": "commit"}
        for entry in sorted(
            entries, key=lambda item: (str(item.get("path", "")) + ("/" if item.get("type") == "tree" else "")).encode()
        ):
            name, mode, target = entry.get("path"), entry.get("mode"), entry.get("sha")
            if (
                not isinstance(name, str)
                or name in {"", ".", ".."}
                or "/" in name
                or "\0" in name
                or name in names
                or mode not in modes
                or entry.get("type") != modes[mode]
                or not isinstance(target, str)
                or not _OID.fullmatch(target)
            ):
                raise ValueError("Official tree path or object identity is invalid")
            names.add(name)
            raw.extend(mode.lstrip("0").encode() + b" " + name.encode() + b"\0" + bytes.fromhex(target))
        if git_object_digest("tree", bytes(raw)) != oid:
            raise ValueError("Official tree identity does not match its complete entries")
        return entries

    async def _cached_tree(self, framework: FrameworkName, oid: str) -> list[Data] | None:
        value = self.trees.get((framework, oid))
        if value is None and self.load_tree is not None:
            value = await self.load_tree(framework, oid)
        if value is not None:
            value = self._verified_tree(oid, value)
            self.trees[framework, oid] = value
        return value

    async def _cached_inventory(self, framework: FrameworkName, oid: str) -> bool:
        pending, seen = [oid], set()
        while pending:
            current = pending.pop()
            if current in seen:
                continue
            seen.add(current)
            entries = await self._cached_tree(framework, current)
            if entries is None:
                return False
            pending.extend(entry["sha"] for entry in entries if entry["type"] == "tree")
        return True

    async def _inventory(self, framework: FrameworkName, oid: str) -> None:
        if await self._cached_inventory(framework, oid):
            return
        value = await self._json(self._api(framework, "git/trees/" + oid + "?recursive=1"))
        if not isinstance(value, dict) or value.get("sha") != oid or type(value.get("truncated")) is not bool:
            raise ValueError("Official recursive inventory identity is missing")
        if value["truncated"]:
            return
        entries = value.get("tree")
        if not isinstance(entries, list):
            raise ValueError("Official recursive tree entries are unavailable")
        directories = {"": oid}
        children: dict[str, list[Data]] = {"": []}
        seen: set[str] = set()
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
                raise ValueError("Official recursive tree path is invalid")
            path = entry["path"]
            if any(part in {"", ".", ".."} or "\0" in part for part in path.split("/")) or path in seen:
                raise ValueError("Official recursive tree path is invalid or duplicated")
            seen.add(path)
            parent, _, name = path.rpartition("/")
            children.setdefault(parent, []).append({**entry, "path": name})
            if entry.get("mode") == "040000":
                tree_oid = entry.get("sha")
                if not isinstance(tree_oid, str) or not _OID.fullmatch(tree_oid):
                    raise ValueError("Official recursive tree identity is invalid")
                directories[path] = tree_oid
                children.setdefault(path, [])
        if set(children) != set(directories):
            raise ValueError("Official recursive tree is incomplete")
        verified = {(framework, tree): self._verified_tree(tree, children[path]) for path, tree in directories.items()}
        self.trees.update(verified)
        if self.save_tree is not None:
            for (_, tree), verified_children in verified.items():
                await self.save_tree(framework, tree, verified_children)

    async def _tree(self, framework: FrameworkName, oid: str) -> list[Data]:
        cached = await self._cached_tree(framework, oid)
        if cached is not None:
            return cached
        value = await self._json(self._api(framework, "git/trees/" + oid))
        if not isinstance(value, dict) or value.get("sha") != oid or value.get("truncated") is not False:
            raise ValueError("Official tree identity is missing, truncated or incomplete")
        entries = self._verified_tree(oid, value.get("tree"))
        self.trees[framework, oid] = entries
        if self.save_tree is not None:
            await self.save_tree(framework, oid, entries)
        return entries

    async def files(self, release: FrameworkReleaseV1) -> AsyncIterator[OfficialSourceEntry]:
        await self._inventory(release.framework, release.tree)
        pending: deque[tuple[str, str, tuple[str, ...]]] = deque([("", release.tree, ())])
        while pending:
            prefix, oid, ancestors = pending.popleft()
            if oid in ancestors:
                raise ValueError("Official tree contains a cyclic identity")
            for entry in await self._tree(release.framework, oid):
                path = prefix + entry["path"]
                if entry["mode"] == "040000":
                    pending.append((path + "/", entry["sha"], (*ancestors, oid)))
                    continue
                regular = entry["mode"] in {"100644", "100755"}
                size = entry.get("size", 0)
                if type(size) is not int or size < 0:
                    raise ValueError("Official source size is invalid")
                kind = source_kind(path, collector_revision=self.collector_revision) if regular else None
                reason = (
                    None if kind is not None else "non_regular_git_entry" if not regular else "non_text_reference_asset"
                )
                yield OfficialSourceEntry(path, entry["sha"], size, entry["mode"], kind, reason)

    async def stream_blob(self, release: FrameworkReleaseV1, entry: OfficialSourceEntry) -> AsyncIterator[bytes]:
        url = (
            "https://raw.githubusercontent.com/"
            + release.repository
            + "/"
            + release.commit
            + "/"
            + quote(entry.path, safe="/")
        )
        hashed = hashlib.sha1(b"blob " + str(entry.size).encode() + b"\0")
        size = 0
        async for chunk in self._stream(url):
            size += len(chunk)
            if size > entry.size:
                raise ValueError("Official source grew beyond its immutable Git descriptor")
            hashed.update(chunk)
            yield chunk
        if size != entry.size or hashed.hexdigest() != entry.oid:
            raise ValueError("Official source content differs from its immutable Git descriptor")

    async def _release(self, framework: FrameworkName, selected: str) -> object:
        if selected == "latest":
            return await self._json(self._api(framework, "releases/latest"))
        selected = framework_version(selected)
        page, matches = 1, []
        while True:
            values = await self._json(self._api(framework, f"releases?per_page=100&page={page}"))
            if not isinstance(values, list):
                raise ValueError("Official stable release page is invalid")
            for value in values:
                if (
                    not isinstance(value, dict)
                    or value.get("draft") is not False
                    or value.get("prerelease") is not False
                ):
                    continue
                try:
                    version = framework_version(value.get("tag_name"))
                except ValueError:
                    continue
                if version == selected:
                    matches.append(value)
            if len(values) < 100:
                break
            page += 1
        if len(matches) != 1:
            raise ValueError("Choose one uniquely published official stable release")
        return matches[0]

    async def discover(self, framework: FrameworkName, selected: str = "latest") -> FrameworkReleaseV1:
        release = await self._release(framework, selected)
        if not isinstance(release, dict) or release.get("draft") is not False or release.get("prerelease") is not False:
            raise ValueError("Official discovery requires a published stable release")
        tag = release.get("tag_name")
        if not isinstance(tag, str):
            raise ValueError("Official release tag is missing")
        version = framework_version(tag)
        reference = await self._json(self._api(framework, "git/ref/tags/" + quote(tag, safe="")))
        if not isinstance(reference, dict):
            raise ValueError("Official release tag reference is missing")
        target = reference.get("object")
        visited = set()
        while isinstance(target, dict) and target.get("type") == "tag":
            oid = target.get("sha")
            if not isinstance(oid, str) or not _OID.fullmatch(oid) or oid in visited:
                raise ValueError("Official annotated tag has an invalid or cyclic identity")
            visited.add(oid)
            annotated = await self._json(self._api(framework, "git/tags/" + oid))
            if not isinstance(annotated, dict) or annotated.get("sha") != oid:
                raise ValueError("Official annotated tag identity changed")
            target = annotated.get("object")
        if (
            not isinstance(target, dict)
            or target.get("type") != "commit"
            or not isinstance(target.get("sha"), str)
            or not _OID.fullmatch(target["sha"])
        ):
            raise ValueError("Official release must resolve to an immutable commit")
        commit = target["sha"]
        value = await self._json(self._api(framework, "git/commits/" + commit))
        if (
            not isinstance(value, dict)
            or value.get("sha") != commit
            or not isinstance(value.get("tree"), dict)
            or not isinstance(value["tree"].get("sha"), str)
            or not _OID.fullmatch(value["tree"]["sha"])
        ):
            raise ValueError("Official commit tree identity is missing")
        tree = value["tree"]["sha"]
        await self._inventory(framework, tree)
        entries = await self._tree(framework, tree)
        licenses = [
            e
            for e in entries
            if e["path"] in {"LICENSE", "LICENSE.md", "LICENSE.txt", "COPYING"} and e["mode"] in {"100644", "100755"}
        ]
        if len(licenses) != 1:
            raise ValueError("Official release requires one explicit root license reference")
        license = licenses[0]
        repository = self._repository(framework)
        metadata = {
            "framework": framework,
            "framework_version": version,
            "release_tag": tag,
            "release_id": release.get("id"),
            "repository": repository,
            "commit": commit,
            "tree": tree,
            "license_path": license["path"],
            "license_sha256": "0" * 64,
            "release_url": "https://github.com/" + repository + "/releases/tag/" + quote(tag, safe=""),
            "published_at": release.get("published_at"),
            "checked_at": datetime.now(UTC).isoformat(),
        }
        provisional = FrameworkReleaseV1.model_validate({**metadata, "digest": content_digest(metadata)})
        hashed = hashlib.sha256()
        async for chunk in self.stream_blob(
            provisional,
            OfficialSourceEntry(license["path"], license["sha"], license["size"], license["mode"], "license"),
        ):
            hashed.update(chunk)
        metadata["license_sha256"] = hashed.hexdigest()
        return FrameworkReleaseV1.model_validate({**metadata, "digest": content_digest(metadata)})
