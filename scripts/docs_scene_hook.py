# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
# https://www.apache.org/licenses/LICENSE-2.0
"""Build local illustration assets and version local CSS/JS by their content."""

import hashlib
import html
import json
import re
import struct
import subprocess
from pathlib import Path

from mkdocs.structure.files import File
from mkdocs.utils import get_relative_url

ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "build/docs-scene"
_hashes = {}
_walkthrough = {}


def _load_walkthrough(config, root=ROOT):
    path = Path(config["docs_dir"]) / "assets/screenshots/walkthrough-manifest.json"
    if not path.exists():
        return {}
    manifest = json.loads(path.read_text())
    if manifest.get("schema_version") != 1 or not manifest.get("fixture_only"):
        raise ValueError("Unsupported walkthrough capture manifest")
    for source, expected in manifest["frontend"].items():
        if hashlib.sha256((root / source).read_bytes()).hexdigest() != expected:
            raise ValueError("Walkthrough frontend changed; recapture " + source)
    capture = root / "studio/tests/docs-walkthrough.spec.ts"
    if hashlib.sha256(capture.read_bytes()).hexdigest() != manifest["capture_test_sha256"]:
        raise ValueError("Walkthrough capture test changed; recapture its source")
    source_digest = hashlib.sha256(
        json.dumps(manifest["frontend"], sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    for name, record in manifest["captures"].items():
        if record.get("frontend_digest", source_digest) != source_digest:
            raise ValueError("Walkthrough screenshot has stale frontend: " + name)
        if record.get("studio_version", manifest.get("studio_version")) != manifest.get("studio_version"):
            raise ValueError("Walkthrough screenshot has stale Studio version: " + name)
        png = (Path(config["docs_dir"]) / record["path"]).read_bytes()
        if hashlib.sha256(png).hexdigest() != record["sha256"]:
            raise ValueError("Walkthrough screenshot changed: " + name)
        width, height = struct.unpack(">II", png[16:24])
        if {"width": width, "height": height} != record["viewport"]:
            raise ValueError("Walkthrough screenshot viewport differs: " + name)
    return manifest


def on_page_markdown(markdown, page, config, files=None):
    del config, files
    captures = _walkthrough.get("captures", {})

    def image(match):
        alt, path = match.groups()
        name = Path(path).name
        if name not in captures:
            return match.group(0)
        dark = name.replace("-light.png", "-dark.png")
        links = []
        for filename, theme in [(name, "light"), (dark, "dark")]:
            record = captures[filename]
            url = html.escape(get_relative_url(record["path"], page.url), quote=True)
            label = html.escape(alt, quote=True)
            size = record["viewport"]
            links.append(
                f'<a class="forge-shot__link theme-{theme}" href="{url}" '
                f'aria-label="Open full screenshot: {label}"><img src="{url}" alt="{label}" '
                f'width="{size["width"]}" height="{size["height"]}" loading="lazy"></a>'
            )
        return "".join(links)

    # Keep theme pairs inline so screenshot expansion also preserves table cells.
    return re.sub(r"!\[([^\]]*)\]\((assets/screenshots/[^)]+)\)", image, markdown)


def on_config(config):
    global _walkthrough
    _walkthrough = _load_walkthrough(config)

    subprocess.run(["node", str(ROOT / "scripts/build-docs-scene.mjs")], check=True, cwd=ROOT)
    config["extra_css"] = list(config["extra_css"]) + ["assets/docs-scene/scene.css"]
    config["extra_javascript"] = list(config["extra_javascript"]) + ["assets/docs-scene/scene.js"]
    _hashes.clear()
    for url in config["extra_css"] + config["extra_javascript"]:
        if not isinstance(url, str) or "://" in url:
            continue
        path = OUTPUT / Path(url).name if url.startswith("assets/docs-scene/") else Path(config["docs_dir"]) / url
        if path.is_file():
            _hashes[url] = hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    return config


def on_files(files, config):
    for name in json.loads((OUTPUT / "manifest.json").read_text())["files"]:
        path = OUTPUT / name
        file = File("assets/docs-scene/" + path.name, str(OUTPUT), config["site_dir"], config["use_directory_urls"])
        file.abs_src_path = str(path)
        files.append(file)
    return files


def on_post_page(output, page, config):
    del page, config
    for url, digest in _hashes.items():
        output = re.sub(r'((?:href|src)="[^"?]*' + re.escape(url) + r')"', r"\1?v=" + digest + '"', output)
    return output
