# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
# https://www.apache.org/licenses/LICENSE-2.0
"""Build local illustration assets and version local CSS/JS by their content."""

import hashlib
import json
import re
import subprocess
from pathlib import Path

from mkdocs.structure.files import File

ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "build/docs-scene"
_hashes = {}


def on_config(config):
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
