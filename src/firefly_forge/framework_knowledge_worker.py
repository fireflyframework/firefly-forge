# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Leased native refresh work; every flycanon submission has a durable predecessor."""

from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import json
import os
import stat
import tempfile
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from urllib.parse import quote

from flycanon_sdk import CanonAPIError, SourceMetadata, SubmitSourceJsonPayload, WorkspaceCreate  # type: ignore[import-untyped]

from firefly_forge.framework_knowledge_models import FrameworkCountsV1, FrameworkReleaseV1, validate_collector_revision
from firefly_forge.framework_knowledge_sync import NonTextReferenceError, canonical_part_ref, reconcile_part, utf8_parts
from firefly_forge.framework_knowledge_upstream import OfficialFrameworkClient, OfficialRateLimited, OfficialSourceEntry
from firefly_forge.product_provenance import content_digest as digest

Data = dict[str, Any]


class FrameworkKnowledgeWorker:
    def __init__(self, manager: Any) -> None:
        self.manager = manager
        self.owner = uuid.uuid4().hex
        self.last = time.monotonic()
        self.request_id = ""
        self.observer: Data | None = None
        self.observe_only = False
        self.configuration_generation: object | None = None

    async def _current(self, *, verify_connection: bool = True) -> Data:
        data = await self.manager._get("operation", self.request_id)
        if data is not None:
            revision = validate_collector_revision(data["policy"].get("collector_revision", 1))
            if revision != validate_collector_revision(data["public"]["corpus"].get("collector_revision", 1)):
                raise ValueError("Retained collector revision differs from its immutable corpus")
        if data is None or data.get("owner") != self.owner:
            raise ValueError("Refresh work no longer owns its native lease")
        await self.manager._authorize(admin=True, actor=data["actor"])
        if self.observer is not None:
            await self.manager._authorize(admin=True, actor=self.observer)
        if data.get("cancel_requested") and not self.observe_only:
            raise ValueError("Official framework refresh was explicitly cancelled")
        await self.manager.knowledge._assert_ownership()
        generation = self.manager.store.configuration.settings_generation
        if not verify_connection and self.configuration_generation is not generation:
            raise ValueError("The official knowledge configuration changed after outside-writer preparation")
        if verify_connection and data["actor"].get("authentication_method") == "managed_framework_knowledge":
            from firefly_forge.framework_knowledge_bootstrap import managed_grant_body

            body = managed_grant_body(self.manager)
            if body is None or digest(body) != data["actor"]["grant_digest"]:
                raise ValueError("Managed framework refresh grant was changed or revoked")
        if verify_connection and digest(self.manager.knowledge.settings()) != data["connection_digest"]:
            raise ValueError("The official knowledge connection changed; retain and review this operation")
        if verify_connection:
            if generation is not self.manager.store.configuration.settings_generation:
                raise ValueError("The official knowledge configuration changed during preparation")
            self.configuration_generation = generation
        return cast(Data, data)

    async def _guard(self) -> None:
        await self._current()

    async def _write_operation(self, data: Data) -> None:
        public = data["public"]
        public["allowed_actions"] = self.manager.operation_actions(data)
        public["revision"] = digest({k: v for k, v in public.items() if k != "revision"})
        await self.manager._put("operation", self.request_id, public["framework"], data, public["framework"])

    async def _spend(self, requests: int = 0, byte_count: int = 0) -> None:
        data = await self._current()
        delta = time.monotonic() - self.last
        async with self.manager.store._unit() as session:
            await self.manager._lock(session, data["public"]["framework"])
            data = await self._current(verify_connection=False)
            spent = data["public"]["spent_budget"]
            total = data["policy"]["budget"]
            proposed = {
                "requests": spent["requests"] + requests,
                "bytes": spent["bytes"] + byte_count,
                "seconds": spent["seconds"] + delta,
            }
            exhausted = any(proposed[key] > total[key] for key in total)
            if exhausted:
                proposed["requests"] = spent["requests"]
                proposed["bytes"] = spent["bytes"]
            data["public"]["spent_budget"] = proposed
            data["public"]["remaining_budget"] = {key: max(0, total[key] - proposed[key]) for key in total}
            data["lease_until"] = (datetime.now(UTC) + timedelta(minutes=5)).isoformat()
            await self._write_operation(data)
        self.last = time.monotonic()
        if exhausted:
            raise ValueError("Retained framework refresh work budget is exhausted; explicit renewal is required")

    async def _each(self, kind: str, parent: str) -> AsyncIterator[Data]:
        cursor = None
        while True:
            async with self.manager.store._unit():
                rows = await self.manager.records.page(kind, parent, cursor, 100)
                values = [(row.key, copy.deepcopy(row.data)) for row in rows]
            if not values:
                return
            for key, data in values:
                yield data
                cursor = key

    def _cache(self) -> Path:
        path = self.manager.store.configuration.directory / "framework-knowledge-cache"
        path.mkdir(mode=0o700, exist_ok=True)
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.getuid():
            raise ValueError("Official reference cache must be a private owned directory")
        return cast(Path, path)

    async def _fetch(
        self, upstream: OfficialFrameworkClient, release: FrameworkReleaseV1, entry: OfficialSourceEntry
    ) -> tuple[str, str]:
        cache = self._cache()
        descriptor, name = tempfile.mkstemp(dir=cache)
        hashed = hashlib.sha256()
        try:
            with os.fdopen(descriptor, "wb") as target:
                async for chunk in upstream.stream_blob(release, entry):
                    target.write(chunk)
                    hashed.update(chunk)
                target.flush()
                os.fsync(target.fileno())
            sha = hashed.hexdigest()
            os.replace(name, cache / sha)
            return sha, sha
        finally:
            Path(name).unlink(missing_ok=True)

    def _chunks(self, name: str) -> Iterator[bytes]:
        if len(name) != 64 or any(c not in "0123456789abcdef" for c in name):
            raise ValueError("Official cache content requires a canonical digest")
        path = self._cache() / name
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as source:
            before = os.fstat(source.fileno())
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_nlink != 1
                or before.st_uid != os.getuid()
                or before.st_mode & 0o077
            ):
                raise ValueError("Official cache content must be a private regular file")
            hashed = hashlib.sha256()
            while chunk := source.read(65536):
                hashed.update(chunk)
                yield chunk
            if (
                hashed.hexdigest() != name
                or self._stamp(os.fstat(source.fileno())) != self._stamp(before)
                or self._stamp(path.stat(follow_symlinks=False)) != self._stamp(before)
            ):
                raise ValueError("Official cache content changed during its verified read")

    @staticmethod
    def _stamp(value: os.stat_result) -> tuple[int, ...]:
        return (
            value.st_dev,
            value.st_ino,
            value.st_size,
            value.st_mtime_ns,
            value.st_ctime_ns,
            value.st_mode,
            value.st_nlink,
            value.st_uid,
            value.st_gid,
        )

    async def _inventory(self, data: Data, upstream: OfficialFrameworkClient) -> None:
        release = FrameworkReleaseV1.model_validate(data["release"])
        corpus = data["public"]["corpus"]
        ref = corpus["corpus_ref"]
        async for entry in upstream.files(release):
            source_ref = digest({"corpus": ref, "path": entry.path})[:32]
            if await self.manager._get("source", source_ref) is not None:
                continue
            public = dict(
                source_ref=source_ref,
                corpus_ref=ref,
                path=entry.path,
                kind=entry.kind or "configuration",
                blob_oid=entry.oid,
                sha256=None,
                bytes=entry.size,
                parts=0,
                ready_parts=0,
                state="pending" if entry.kind else "excluded",
                source_uri="https://github.com/"
                + release.repository
                + "/blob/"
                + release.commit
                + "/"
                + quote(entry.path, safe="/"),
                license_sha256=release.license_sha256,
                exclusion_reason=entry.exclusion_reason,
            )
            if validate_collector_revision(data["policy"].get("collector_revision", 1)) >= 3:
                public.update(excluded_parts=0, exclusions_sha256=None)
            async with self.manager.store._unit():
                await self._current(verify_connection=False)
                await self.manager._put(
                    "source", source_ref, release.framework, {"public": public, "mode": entry.mode}, ref
                )
        async with self.manager.store._unit():
            fresh = await self._current(verify_connection=False)
            fresh["inventory_complete"] = True
            await self._write_operation(fresh)
        await self._stage("fetching")

    async def _stage(self, stage: str) -> None:
        async with self.manager.store._unit():
            data = await self._current(verify_connection=False)
            data["public"]["stage"] = stage
            phase = {"inventory": "queued", "fetching": "fetching", "submitting": "indexing", "verifying": "verifying"}[
                stage
            ]
            data["public"]["corpus"]["state"] = phase
            ref = data["public"]["corpus"]["corpus_ref"]
            corpus = await self.manager._get("corpus", ref)
            corpus["public"]["state"] = phase
            await self.manager._put("corpus", ref, data["public"]["framework"], corpus, data["public"]["framework"])
            await self._write_operation(data)

    async def _files(self, data: Data, upstream: OfficialFrameworkClient) -> None:
        release = FrameworkReleaseV1.model_validate(data["release"])
        ref = data["public"]["corpus"]["corpus_ref"]
        async for source in self._each("source", ref):
            public = source["public"]
            if public["state"] != "pending":
                continue
            entry = OfficialSourceEntry(
                public["path"], public["blob_oid"], public["bytes"], source["mode"], public["kind"]
            )
            sha, cache_name = await self._fetch(upstream, release, entry)
            revision = validate_collector_revision(data["policy"].get("collector_revision", 1))
            chunks = self._chunks(cache_name)
            nonblank = False
            try:
                for chunk in utf8_parts(chunks, window=data["policy"]["part_bytes"]):
                    nonblank = nonblank or bool(chunk.decode("utf-8").strip())
            except NonTextReferenceError:
                for _ in chunks:
                    pass
                if revision >= 3 and public["kind"] == "license":
                    raise ValueError("Official license requires completely verified extractable UTF-8 text") from None
                public.update(state="excluded", sha256=sha, exclusion_reason="non_utf8_reference_asset")
            else:
                if revision >= 3 and public["kind"] == "license" and not nonblank:
                    raise ValueError("Official license cannot be excluded as a blank reference")
                count = 0
                exclusions: list[Data] = []
                for ordinal, chunk in enumerate(
                    utf8_parts(self._chunks(cache_name), window=data["policy"]["part_bytes"]), 1
                ):
                    excluded = revision >= 3 and not chunk.decode("utf-8").strip()
                    count += not excluded
                    part_ref = digest(
                        {
                            "source": public["source_ref"],
                            "ordinal": ordinal,
                            "sha256": hashlib.sha256(chunk).hexdigest(),
                        }
                    )[:32]
                    part_cache = hashlib.sha256(chunk).hexdigest()
                    fd, temporary = tempfile.mkstemp(dir=self._cache())
                    try:
                        with os.fdopen(fd, "wb") as target:
                            target.write(chunk)
                            target.flush()
                            os.fsync(target.fileno())
                        os.replace(temporary, self._cache() / part_cache)
                    finally:
                        Path(temporary).unlink(missing_ok=True)
                    part = {
                        "part_ref": part_ref,
                        "source_ref": public["source_ref"],
                        "corpus_ref": ref,
                        "ordinal": ordinal,
                        "content_sha256": hashlib.sha256(chunk).hexdigest(),
                        "bytes": len(chunk),
                        "cache": part_cache,
                        "filename": f"ffw-{ref}-{public['source_ref']}-{ordinal}.txt",
                        "state": "excluded" if excluded else "pending",
                        "source_id": None,
                    }
                    if excluded:
                        part["exclusion_reason"] = "semantic_whitespace_only_reference"
                        exclusions.append(self._exclusion_provenance(part))
                    async with self.manager.store._unit():
                        await self._current(verify_connection=False)
                        await self.manager._put("part", part_ref, release.framework, part, ref)
                public.update(state="fetched", sha256=sha, parts=count)
                if revision >= 3:
                    public.update(
                        excluded_parts=len(exclusions), exclusions_sha256=digest(exclusions) if exclusions else None
                    )
                    if not nonblank:
                        public.update(state="excluded", exclusion_reason="semantic_whitespace_only_reference")
            source["cache"] = cache_name
            async with self.manager.store._unit():
                await self._current(verify_connection=False)
                await self.manager._put("source", public["source_ref"], release.framework, source, ref)
        # Bind the nearest retained license; nested official examples can carry distinct terms.
        licenses: dict[str, list[Data]] = {}
        async for source in self._each("source", ref):
            item = source["public"]
            if item["kind"] == "license" and item["path"].rsplit("/", 1)[-1].lower() != "notice" and item["sha256"]:
                directory = item["path"].rpartition("/")[0]
                licenses.setdefault(directory, []).append(item)
        async for source in self._each("source", ref):
            item = source["public"]
            directory = item["path"].rpartition("/")[0]
            while directory not in licenses and directory:
                directory = directory.rpartition("/")[0]
            candidates = licenses.get(directory, [])
            if len({v["sha256"] for v in candidates}) != 1:
                raise ValueError("Official source requires unambiguous nearest license provenance")
            item["license_sha256"] = candidates[0]["sha256"]
            source["license_path"] = candidates[0]["path"]
            async with self.manager.store._unit():
                await self._current(verify_connection=False)
                await self.manager._put("source", item["source_ref"], release.framework, source, ref)
        await self._stage("submitting")

    def _part_content(self, part: Data, window: int) -> bytes:
        selected = b"".join(self._chunks(part["cache"]))
        if (
            len(selected) > window
            or len(selected) != part["bytes"]
            or hashlib.sha256(selected).hexdigest() != part["content_sha256"]
        ):
            raise ValueError("Retained reference window differs from its immutable source")
        return selected

    async def _workspace(self, client: Any, data: Data) -> None:
        workspace = data["public"]["corpus"]["workspace_id"]
        await self._spend(requests=1)
        try:
            await client.get_workspace(workspace)
            return
        except CanonAPIError as exc:
            if exc.status_code != 404:
                raise
        async with self.manager.store._unit():
            await self._current(verify_connection=False)
            await self.manager._event(
                data["public"]["framework"], "workspace_intent", {"workspace_id": workspace}, self.request_id
            )
        await self._spend(requests=1)
        await client.create_workspace(
            WorkspaceCreate(
                id=workspace,
                name="Official " + data["public"]["framework"] + " " + data["release"]["framework_version"],
            )
        )
        await self._spend()

    async def _submit(self, data: Data, *, observe_only: bool) -> None:
        ref = data["public"]["corpus"]["corpus_ref"]
        framework = data["public"]["framework"]
        settings = self.manager.knowledge.settings({"knowledge_snapshot": data["knowledge_snapshot"]})
        async with self.manager.knowledge.client(settings, data["public"]["corpus"]["workspace_id"]) as client:
            if not observe_only:
                await self._workspace(client, data)
            async for part in self._each("part", ref):
                if part["state"] in {"ready", "excluded"}:
                    continue
                if part["state"] in {"submitting", "needs_reconciliation"}:

                    async def before_page() -> None:
                        await self._spend(requests=1)

                    async def lookup_part(reference: str) -> Data | None:
                        await self._current()
                        return cast(Data | None, await self.manager._get("part", reference))

                    remote = await reconcile_part(client, part, before_page=before_page, lookup_part=lookup_part)
                    await self._spend()
                    if remote is None:
                        raise ValueError("An uncertain submission has no unique scoped remote outcome")
                    part.update(state="indexing", source_id=remote)
                    async with self.manager.store._unit():
                        await self._current(verify_connection=False)
                        await self.manager._put("part", part["part_ref"], framework, part, ref)
                if part["state"] == "pending":
                    if observe_only:
                        continue
                    content = self._part_content(part, data["policy"]["part_bytes"])
                    source = await self.manager._get("source", part["source_ref"])
                    await self._spend(requests=1, byte_count=len(content))
                    async with self.manager.store._unit():
                        await self._current(verify_connection=False)
                        part["state"] = "submitting"
                        await self.manager._put("part", part["part_ref"], framework, part, ref)
                        await self.manager._event(
                            framework,
                            "source_intent",
                            {
                                "part_ref": part["part_ref"],
                                "source_sha256": source["public"]["sha256"],
                                "content_sha256": part["content_sha256"],
                            },
                            self.request_id,
                        )
                    remote = await client.submit_source(
                        SubmitSourceJsonPayload(
                            kind="text",
                            filename=part["filename"],
                            content_type="text/plain",
                            content_base64=base64.b64encode(content).decode(),
                            metadata=SourceMetadata(
                                title=source["public"]["path"],
                                domain="engineering",
                                tags=["forge", "official-framework"],
                                extra={
                                    "forge_framework_part": part["part_ref"],
                                    "corpus_ref": ref,
                                    "framework": framework,
                                    "framework_version": data["release"]["framework_version"],
                                    "commit": data["release"]["commit"],
                                    "source_path": source["public"]["path"],
                                    "source_uri": source["public"]["source_uri"],
                                    "source_sha256": source["public"]["sha256"],
                                    "license_sha256": source["public"]["license_sha256"],
                                    "license_path": source["license_path"],
                                },
                            ),
                        )
                    )
                    async with self.manager.store._unit():
                        part.update(state="indexing", source_id=remote.id)
                        await self.manager._put("part", part["part_ref"], framework, part, ref)
                        await self.manager._event(
                            framework,
                            "source_outcome",
                            {"part_ref": part["part_ref"], "source_id": remote.id},
                            self.request_id,
                        )
                    await self._spend()
                if part["source_id"]:
                    await self._verify_part(client, data, part)
        await self._stage("verifying")

    async def _verify_part(self, client: Any, data: Data, part: Data) -> None:
        await self._spend(requests=1)
        remote = await client.get_source(part["source_id"])
        await self._spend()
        reference = remote.metadata.extra.get("forge_framework_part")
        canonical = (
            part
            if reference == part["part_ref"]
            else (await self.manager._get("part", reference) if isinstance(reference, str) else None)
        )
        reference = canonical_part_ref(remote, part, canonical)
        if part.get("canonical_part_ref") is None:
            async with self.manager.store._unit():
                await self._current(verify_connection=False)
                part["canonical_part_ref"] = reference
                await self.manager._put("part", part["part_ref"], data["public"]["framework"], part, part["corpus_ref"])
                if reference != part["part_ref"]:
                    await self.manager._event(
                        data["public"]["framework"],
                        "source_alias",
                        {
                            "part_ref": part["part_ref"],
                            "canonical_part_ref": reference,
                            "source_id": remote.id,
                            "content_sha256": part["content_sha256"],
                        },
                        self.request_id,
                    )
        if remote.status not in {"ingested", "ready", "completed"}:
            return
        await self._spend(requests=1)
        source = await self.manager._get("source", part["source_ref"])
        result = await client.search(source["public"]["path"], top_k=1, source_ids=[part["source_id"]])
        await self._spend()
        if not any(hit.source_id == part["source_id"] and hit.content.strip() for hit in result.hits):
            return
        async with self.manager.store._unit():
            await self._current(verify_connection=False)
            part["state"] = "ready"
            await self.manager._put("part", part["part_ref"], data["public"]["framework"], part, part["corpus_ref"])

    @staticmethod
    def _exclusion_provenance(part: Data) -> Data:
        return {key: part[key] for key in ("part_ref", "ordinal", "content_sha256", "bytes", "exclusion_reason")}

    def _verified_exclusions(self, source: Data, window: int) -> list[Data]:
        public = source["public"]
        content_hash = hashlib.sha256()
        blob_hash = hashlib.sha1(b"blob " + str(public["bytes"]).encode() + b"\0")
        size = 0

        def verified_chunks() -> Iterator[bytes]:
            nonlocal size
            for chunk in self._chunks(source["cache"]):
                size += len(chunk)
                content_hash.update(chunk)
                blob_hash.update(chunk)
                yield chunk

        result = []
        for ordinal, chunk in enumerate(utf8_parts(verified_chunks(), window=window), 1):
            if not chunk.decode("utf-8").strip():
                content_sha = hashlib.sha256(chunk).hexdigest()
                result.append(
                    dict(
                        part_ref=digest({"source": public["source_ref"], "ordinal": ordinal, "sha256": content_sha})[
                            :32
                        ],
                        ordinal=ordinal,
                        content_sha256=content_sha,
                        bytes=len(chunk),
                        exclusion_reason="semantic_whitespace_only_reference",
                    )
                )
        if (
            size != public["bytes"]
            or content_hash.hexdigest() != public["sha256"]
            or blob_hash.hexdigest() != public["blob_oid"]
        ):
            raise ValueError("Excluded reference windows differ from their immutable Git blob")
        return result

    async def _finish(self, data: Data) -> None:
        ref = data["public"]["corpus"]["corpus_ref"]
        framework = data["public"]["framework"]
        current = await self._current()
        if not current.get("inventory_complete"):
            raise ValueError("A complete immutable inventory is required before readiness")
        counts = FrameworkCountsV1().model_dump()
        manifest = hashlib.sha256()
        revision = validate_collector_revision(data["policy"].get("collector_revision", 1))
        manifest_provenance = {"collector_revision": revision, "policy_digest": data["policy_digest"]}
        if revision != 1:
            manifest.update(json.dumps(manifest_provenance, sort_keys=True, separators=(",", ":")).encode() + b"\n")
        excluded_by_source: dict[str, list[Data]] = {}
        if revision >= 3:
            async for part in self._each("part", ref):
                if part["state"] == "excluded":
                    if (
                        part["source_id"] is not None
                        or part.get("exclusion_reason") != "semantic_whitespace_only_reference"
                    ):
                        raise ValueError("Excluded reference part has no reviewed disposition")
                    excluded_by_source.setdefault(part["source_ref"], []).append(self._exclusion_provenance(part))
            for receipts in excluded_by_source.values():
                receipts.sort(key=lambda item: item["ordinal"])
        complete = True
        unknown = 0
        async for source in self._each("source", ref):
            item = source["public"]
            counts["total_files"] += 1
            if revision >= 3:
                excluded = excluded_by_source.pop(item["source_ref"], [])
                if excluded or item.get("exclusion_reason") == "semantic_whitespace_only_reference":
                    if excluded != self._verified_exclusions(source, data["policy"]["part_bytes"]):
                        raise ValueError("Excluded reference ordinal or content provenance changed")
                    if item["kind"] == "license" and item["state"] == "excluded":
                        raise ValueError("Required official license cannot be excluded")
                if item.get("excluded_parts", 0) != len(excluded) or item.get("exclusions_sha256") != (
                    digest(excluded) if excluded else None
                ):
                    raise ValueError("Excluded reference manifest differs from its retained part inventory")
                counts["excluded_parts"] += len(excluded)
            if counts["total_files"] % 100 == 0:
                await self._spend()
            if item["state"] == "excluded":
                counts["excluded_files"] += 1
            else:
                counts["files"] += 1
                counts["bytes"] += item["bytes"]
                if item["kind"] in {"document", "example", "api"}:
                    counts[{"document": "documents", "example": "examples", "api": "api"}[item["kind"]]] += 1
                async with self.manager.store._unit():
                    states = await self.manager.records.part_counts(item["source_ref"])
                ready = states.get("ready", 0)
                if revision >= 3 and sum(states.values()) != item["parts"] + item.get("excluded_parts", 0):
                    raise ValueError("Admitted and excluded reference counts differ from their durable parts")
                unknown += states.get("submitting", 0) + states.get("needs_reconciliation", 0)
                item["ready_parts"] = ready
                if ready == item["parts"] and item["state"] != "pending":
                    item["state"] = "ready"
                else:
                    complete = False
                counts["parts"] += item["parts"]
                counts["ready_parts"] += ready
                async with self.manager.store._unit():
                    await self._current(verify_connection=False)
                    await self.manager._put("source", item["source_ref"], framework, source, ref)
            manifest.update(json.dumps(item, sort_keys=True, separators=(",", ":")).encode() + b"\n")
        if excluded_by_source:
            raise ValueError("Excluded reference parts have no immutable source inventory")
        corpus = await self.manager._get("corpus", ref)
        if complete:
            from firefly_forge.framework_knowledge_requirements import manifest_requirements

            path = "pyproject.toml" if framework == "pyfly" else "composer.json"
            source = await self.manager._get("source", digest({"corpus": ref, "path": path})[:32])
            if source is None or source["public"]["state"] != "ready":
                raise ValueError("Official corpus requires its complete immutable runtime manifest")
            budget = int(os.environ.get("FORGE_FRAMEWORK_MANIFEST_BYTE_BUDGET", str(32 * 1024**2)))
            if budget <= 0 or source["public"]["bytes"] > budget:
                raise ValueError("Official runtime manifest requires a larger explicit metadata work budget")
            body = b"".join(self._chunks(source["cache"]))
            corpus["runtime_requirements"] = manifest_requirements(
                framework, data["release"]["framework_version"], data["release"]["commit"], source["public"], body
            )
        await self._spend()
        corpus["counts"] = counts
        if revision != 1:
            corpus["manifest_provenance"] = manifest_provenance
        corpus["public"]["manifest_sha256"] = manifest.hexdigest() if complete else None
        corpus["public"]["state"] = "ready" if complete else "indexing"
        async with self.manager.store._unit() as session:
            await self.manager._lock(session, framework)
            data = await self._current(verify_connection=False)
            await self.manager._put("corpus", ref, framework, corpus, framework)
            data["public"].update(
                corpus=corpus["public"],
                counts=counts,
                uncertain_sources=unknown,
                state="ready" if complete else "running",
                stage="complete" if complete else "verifying",
                sanitized_error=None,
                allowed_actions=[] if complete else ["observe"],
            )
            if data.get("cancel_requested"):
                data["public"]["state"] = "cancelled"
            if complete and data["make_current"] and not data.get("cancel_requested"):
                saved = await self.manager._get("status", framework) or {}
                saved["current_corpus"] = ref
                await self.manager._put("status", framework, framework, saved)
            await self._write_operation(data)
            if complete:
                await self.manager._event(
                    framework,
                    "refresh_outcome",
                    {
                        "request_id": self.request_id,
                        "corpus_ref": ref,
                        "manifest_sha256": manifest.hexdigest(),
                        "counts": counts,
                    },
                    self.request_id,
                )

    async def advance(self, request_id: str, *, observe_only: bool = False, observer: Data | None = None) -> None:
        self.request_id = request_id
        self.observe_only = observe_only
        self.observer = observer
        self.last = time.monotonic()
        data = await self.manager._get("operation", request_id)
        if data is not None:
            validate_collector_revision(data["policy"].get("collector_revision", 1))
        if data is None or data["public"]["state"] in {"ready", "failed", "cancelled"}:
            return
        if observe_only and data["public"]["stage"] in {"inventory", "fetching"}:
            return
        async with self.manager.store._unit() as session:
            await self.manager._lock(session, data["public"]["framework"])
            data = await self.manager._get("operation", request_id)
            if data.get("owner") and datetime.fromisoformat(data["lease_until"]) > datetime.now(UTC):
                return
            data["owner"] = self.owner
            data["lease_until"] = (datetime.now(UTC) + timedelta(minutes=5)).isoformat()
            data["public"].update(state="running", sanitized_error=None)
            await self._write_operation(data)
        try:
            await self._spend()
            if not observe_only:
                async with OfficialFrameworkClient(
                    collector_revision=data["policy"].get("collector_revision", 1),
                    transport=self.manager.transport,
                    spend=self._spend,
                    assert_current=self._guard,
                    load_tree=self.manager._load_tree,
                    save_tree=self.manager._save_tree,
                ) as upstream:
                    if data["public"]["stage"] == "inventory":
                        await self._inventory(data, upstream)
                    if (await self.manager._get("operation", request_id))["public"]["stage"] == "fetching":
                        await self._files(data, upstream)
            await self._submit(data, observe_only=observe_only)
            await self._finish(data)
        except (Exception, asyncio.CancelledError) as exc:
            async with self.manager.store._unit():
                fresh = await self.manager._get("operation", request_id)
                if fresh and fresh.get("owner") == self.owner:
                    unknown = 0
                    async for part in self._each("part", fresh["public"]["corpus"]["corpus_ref"]):
                        unknown += part["state"] in {"submitting", "needs_reconciliation"}
                    spent = fresh["public"]["spent_budget"]
                    spent["seconds"] += max(0, time.monotonic() - self.last)
                    fresh["public"]["remaining_budget"]["seconds"] = max(
                        0, fresh["policy"]["budget"]["seconds"] - spent["seconds"]
                    )
                    fresh["public"].update(
                        state="cancelled" if fresh.get("cancel_requested") else "needs_reconciliation",
                        uncertain_sources=unknown,
                        sanitized_error=(
                            "Official refresh retained for observation; no uncertain submission will be replayed"
                        ),
                        allowed_actions=["observe"],
                    )
                    if isinstance(exc, OfficialRateLimited):
                        fresh["retry_at"] = exc.retry_at
                    fresh["error_type"] = type(exc).__name__
                    await self._write_operation(fresh)
            if isinstance(exc, asyncio.CancelledError):
                raise
        finally:
            async with self.manager.store._unit():
                fresh = await self.manager._get("operation", request_id)
                if fresh and fresh.get("owner") == self.owner:
                    fresh.pop("owner", None)
                    fresh.pop("lease_until", None)
                    await self._write_operation(fresh)
