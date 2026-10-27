# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Official immutable provenance using controlled HTTP; no real upstream requests."""

import hashlib
import json
from urllib.parse import unquote

import httpx
import pytest


def git_oid(kind, content):
    return hashlib.sha1(kind.encode() + b" " + str(len(content)).encode() + b"\0" + content).hexdigest()


def official_fixture(framework="pyfly", *, extra_files=None):
    if framework not in {"pyfly", "larafly"}:
        raise ValueError("Choose an official fixture framework")
    version = "26.10.11" if framework == "pyfly" else "26.10.4"
    repository = "fireflyframework-pyfly" if framework == "pyfly" else "fireflyframework-php"
    blobs, trees = {}, {}

    def tree(values):
        entries = []
        raw = b""
        for name, value in sorted(
            values.items(), key=lambda pair: pair[0] + ("/" if isinstance(pair[1], dict) else "")
        ):
            if isinstance(value, dict):
                oid = tree(value)
                mode, kind = "040000", "tree"
                entry = {"path": name, "mode": mode, "type": kind, "sha": oid}
            else:
                oid = git_oid("blob", value)
                blobs[oid] = value
                mode, kind = "100644", "blob"
                entry = {"path": name, "mode": mode, "type": kind, "sha": oid, "size": len(value)}
            raw += mode.lstrip("0").encode() + b" " + name.encode() + b"\0" + bytes.fromhex(oid)
            entries.append(entry)
        oid = git_oid("tree", raw)
        trees[oid] = {"sha": oid, "truncated": False, "tree": entries}
        return oid

    files = {
        "LICENSE": b"Apache License, Version 2.0\n",
        "pyproject.toml": b'[project]\nname="pyfly"\nversion="26.10.11"\nrequires-python=">=3.12"\n',
        "docs": {"guide.md": b"# Current service guide\n"},
        "src": {"pyfly": {"service.py": b"class CurrentService:\n    pass\n"}},
        "examples": {"demo.py": b"print('official example')\n"},
    }
    if framework == "larafly":
        files = {
            "LICENSE": b"Apache License, Version 2.0\n",
            "composer.json": json.dumps(
                {
                    "name": "fireflyframework/larafly",
                    "version": "26.10.4",
                    "require": {"php": "^8.5.12", "illuminate/support": "^13.38"},
                }
            ).encode(),
            "docs": {"guide.md": b"# Current larafly service guide\n"},
            "src": {"Service.php": b"<?php namespace Firefly; class CurrentService {}\n"},
            "examples": {"demo.php": b"<?php echo 'official example';\n"},
        }
    if extra_files:
        files.update(extra_files)
    root = tree(files)
    commit, tag = "a" * 40, "b" * 40
    requests = []

    def response(request):
        requests.append(request)
        path = unquote(request.url.path)
        if path.endswith("/releases/latest"):
            return httpx.Response(
                200,
                headers={"set-cookie": "do_not_forward=public-origin; Path=/"},
                json={
                    "id": 422971471,
                    "tag_name": "v" + version,
                    "draft": False,
                    "prerelease": False,
                    "published_at": "2026-10-24T20:43:09Z",
                    "target_commitish": "main",
                    "html_url": "https://github.com/fireflyframework/" + repository + "/releases/tag/v" + version,
                },
            )
        if path.endswith("/git/ref/tags/v" + version):
            return httpx.Response(200, json={"object": {"type": "tag", "sha": tag}})
        if path.endswith("/git/tags/" + tag):
            return httpx.Response(200, json={"sha": tag, "object": {"type": "commit", "sha": commit}})
        if path.endswith("/git/commits/" + commit):
            return httpx.Response(200, json={"sha": commit, "tree": {"sha": root}})
        if "/git/trees/" in path:
            oid = path.rsplit("/", 1)[1]
            if request.url.params.get("recursive") == "1" and not trees[oid]["truncated"]:
                flattened = []

                def walk(current, prefix=""):
                    for entry in trees[current]["tree"]:
                        flattened.append({**entry, "path": prefix + entry["path"]})
                        if entry["type"] == "tree":
                            walk(entry["sha"], prefix + entry["path"] + "/")

                walk(oid)
                return httpx.Response(200, json={"sha": oid, "truncated": False, "tree": flattened})
            return httpx.Response(200, json=trees[oid])
        if request.url.host == "raw.githubusercontent.com":
            current = files
            for part in path.split("/")[4:]:
                current = current[part]
            return httpx.Response(200, content=current)
        raise AssertionError("Unexpected public request " + str(request.url))

    return httpx.MockTransport(response), requests, trees, root, blobs


async def test_stable_release_follows_immutable_tag_and_license_without_forwarding_credentials(monkeypatch):
    from firefly_forge.framework_knowledge_upstream import OfficialFrameworkClient

    transport, requests, _, _, _ = official_fixture()
    monkeypatch.setenv("GITHUB_TOKEN", "synthetic-not-forwarded")
    async with OfficialFrameworkClient(transport=transport) as client:
        release = await client.discover("pyfly")
    assert release.framework_version == "26.10.11" and release.commit == "a" * 40
    assert release.license_sha256 == hashlib.sha256(b"Apache License, Version 2.0\n").hexdigest()
    assert requests and all("authorization" not in r.headers and "cookie" not in r.headers for r in requests)
    assert all(r.url.host in {"api.github.com", "raw.githubusercontent.com"} for r in requests)


async def test_complete_tree_walk_includes_docs_examples_and_api_source():
    from firefly_forge.framework_knowledge_upstream import OfficialFrameworkClient

    transport, _, _, _, _ = official_fixture()
    async with OfficialFrameworkClient(transport=transport) as client:
        release = await client.discover("pyfly")
        entries = [entry async for entry in client.files(release)]
    assert {entry.path: entry.kind for entry in entries} == {
        "LICENSE": "license",
        "pyproject.toml": "configuration",
        "docs/guide.md": "document",
        "examples/demo.py": "example",
        "src/pyfly/service.py": "api",
    }


async def test_truncated_or_mutated_tree_is_not_complete_source_coverage():
    from firefly_forge.framework_knowledge_upstream import OfficialFrameworkClient

    transport, _, trees, root, _ = official_fixture()
    async with OfficialFrameworkClient(transport=transport) as client:
        release = await client.discover("pyfly")
    trees[root]["truncated"] = True
    async with OfficialFrameworkClient(transport=transport) as client:
        with pytest.raises(ValueError, match="complete|truncated"):
            _ = [entry async for entry in client.files(release)]
    trees[root]["truncated"] = False
    trees[root]["tree"][0]["path"] = "changed"
    async with OfficialFrameworkClient(transport=transport) as client:
        with pytest.raises(ValueError, match="tree|identity"):
            _ = [entry async for entry in client.files(release)]


async def test_redirect_never_reaches_another_origin():
    from firefly_forge.framework_knowledge_upstream import OfficialFrameworkClient

    calls = []

    def redirect(request):
        calls.append(request.url.host)
        return httpx.Response(302, headers={"location": "https://elsewhere.example/private"})

    async with OfficialFrameworkClient(transport=httpx.MockTransport(redirect)) as client:
        with pytest.raises(ValueError, match="official|upstream|redirect"):
            await client.discover("pyfly")
    assert calls == ["api.github.com"]


async def test_recursive_inventory_uses_one_api_tree_for_all_directories():
    from firefly_forge.framework_knowledge_upstream import OfficialFrameworkClient

    original, requests, trees, root, _ = official_fixture()

    def response(request):
        if request.url.params.get("recursive") == "1":
            requests.append(request)
            flattened = []

            def walk(oid, prefix=""):
                for entry in trees[oid]["tree"]:
                    flattened.append({**entry, "path": prefix + entry["path"]})
                    if entry["type"] == "tree":
                        walk(entry["sha"], prefix + entry["path"] + "/")

            walk(root)
            return httpx.Response(200, json={"sha": root, "truncated": False, "tree": flattened})
        return original.handler(request)

    async with OfficialFrameworkClient(transport=httpx.MockTransport(response)) as client:
        release = await client.discover("pyfly")
        entries = [entry async for entry in client.files(release)]
    tree_calls = [r for r in requests if "/git/trees/" in r.url.path]
    assert len(entries) == 5 and len(tree_calls) == 1
    assert tree_calls[0].url.params["recursive"] == "1"


async def test_exact_release_is_discovered_beyond_first_page():
    from firefly_forge.framework_knowledge_upstream import OfficialFrameworkClient

    original, requests, _, _, _ = official_fixture()

    def response(request):
        if request.url.path.endswith("/releases"):
            requests.append(request)
            if request.url.params["page"] == "1":
                return httpx.Response(
                    200, json=[{"tag_name": f"v25.1.{i}", "draft": False, "prerelease": False} for i in range(100)]
                )
            return httpx.Response(
                200,
                json=[
                    {
                        "id": 1,
                        "tag_name": "v26.10.11",
                        "draft": False,
                        "prerelease": False,
                        "published_at": "2026-10-24T20:43:09Z",
                    }
                ],
            )
        return original.handler(request)

    async with OfficialFrameworkClient(transport=httpx.MockTransport(response)) as client:
        release = await client.discover("pyfly", "26.10.11")
    assert release.framework_version == "26.10.11"
    assert [r.url.params["page"] for r in requests if r.url.path.endswith("/releases")] == ["1", "2"]


async def test_rate_limit_exposes_cooldown_without_repeated_request():
    from firefly_forge.framework_knowledge_upstream import OfficialFrameworkClient, OfficialRateLimited

    calls = []

    def limited(request):
        calls.append(request)
        return httpx.Response(403, headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1792998000"})

    async with OfficialFrameworkClient(transport=httpx.MockTransport(limited)) as client:
        with pytest.raises(OfficialRateLimited) as failure:
            await client.discover("pyfly")
    assert failure.value.retry_at == "2026-10-26T07:00:00+00:00"
    assert len(calls) == 1


def test_official_snippets_stubs_and_unknown_text_extensions_are_not_silently_omitted():
    from firefly_forge.framework_knowledge_upstream import source_kind

    assert source_kind("packages/cli/resources/stubs/controller.stub") == "example"
    assert source_kind("src/pyfly/service.pyi") == "api"
    assert source_kind("examples/custom.sample") == "example"
    assert source_kind("docs/Makefile") == "configuration"
    assert source_kind("uv.lock") == "configuration"
    assert source_kind("assets/logo.png") is None


async def test_larafly_fixture_preserves_php_runtime_and_complete_reference_families():
    import json

    from firefly_forge.framework_knowledge_upstream import OfficialFrameworkClient

    transport, _, _, _, _ = official_fixture("larafly")
    async with OfficialFrameworkClient(transport=transport) as client:
        release = await client.discover("larafly")
        entries = [entry async for entry in client.files(release)]
        manifest = next(e for e in entries if e.path == "composer.json")
        body = b"".join([chunk async for chunk in client.stream_blob(release, manifest)])
    data = json.loads(body)
    assert release.framework_version == "26.10.4"
    assert release.repository == "fireflyframework/fireflyframework-php"
    assert len(entries) == 5 and {e.kind for e in entries} == {"license", "configuration", "document", "example", "api"}
    assert data["require"] == {"php": "^8.5.12", "illuminate/support": "^13.38"}


async def test_verified_tree_cache_reuses_inventory_across_client_restarts_and_rejects_corruption():
    import copy

    from firefly_forge.framework_knowledge_upstream import OfficialFrameworkClient

    transport, requests, trees, root, _ = official_fixture()
    cached = {}

    async def load(framework, oid):
        return copy.deepcopy(cached.get((framework, oid)))

    async def save(framework, oid, entries):
        cached[framework, oid] = copy.deepcopy(entries)

    async with OfficialFrameworkClient(transport=transport, load_tree=load, save_tree=save) as client:
        release = await client.discover("pyfly")
        first = [entry async for entry in client.files(release)]
    before = len([r for r in requests if "/git/trees/" in r.url.path])
    async with OfficialFrameworkClient(transport=transport, load_tree=load, save_tree=save) as restarted:
        second = [entry async for entry in restarted.files(release)]
    assert first == second and len(cached) == len(trees)
    assert len([r for r in requests if "/git/trees/" in r.url.path]) == before
    cached["pyfly", root][0]["sha"] = "f" * 40
    async with OfficialFrameworkClient(transport=transport, load_tree=load, save_tree=save) as corrupted:
        with pytest.raises(ValueError, match="identity"):
            _ = [entry async for entry in corrupted.files(release)]
