# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Bind downloaded CI distributions to the publishing checkout and rebuilt Studio."""

from __future__ import annotations

import argparse
import base64
import configparser
import csv
import hashlib
import io
import stat
import subprocess
import tarfile
import tomllib
import zipfile
from collections import Counter
from email.parser import BytesParser
from pathlib import Path, PurePosixPath
from typing import Any

from packaging.markers import Marker
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name


def _safe(name: str) -> None:
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or "\\" in name or path.as_posix() != name:
        raise ValueError("Distribution contains an unsafe path")


def _metadata(data: bytes, project: dict[str, Any], readme: bytes) -> None:
    headers = BytesParser().parsebytes(data)
    expected = {
        "Name": project["name"],
        "Version": project["version"],
        "Requires-Python": project["requires-python"],
        "License-Expression": project["license"],
    }
    if any(headers.get_all(key) != [value] for key, value in expected.items()):
        raise ValueError("Distribution metadata differs from source")
    requirements = [Requirement(item) for item in project["dependencies"]]
    optional = project.get("optional-dependencies", {})
    extras = [canonicalize_name(extra, validate=True) for extra in optional]
    if len(set(extras)) != len(extras) or Counter(headers.get_all("Provides-Extra", [])) != Counter(extras):
        raise ValueError("Distribution extras differ from source")
    for extra, dependencies in optional.items():
        selector = f"extra == {canonicalize_name(extra)!r}"
        for dependency in dependencies:
            requirement = Requirement(dependency)
            requirement.marker = Marker(f"({requirement.marker}) and {selector}" if requirement.marker else selector)
            requirements.append(requirement)
    if Counter(Requirement(item) for item in headers.get_all("Requires-Dist", [])) != Counter(requirements):
        raise ValueError("Distribution dependencies differ from source")
    if data.partition(b"\n\n")[2].rstrip(b"\n") != readme.rstrip(b"\n"):
        raise ValueError("Distribution description differs from README")


def verify_payloads(directory: Path, root: Path, *, tag: str | None = None) -> None:
    project = tomllib.loads((root / "pyproject.toml").read_text())["project"]
    if tag is not None and tag != "v" + project["version"]:
        raise ValueError("Release tag differs from source version")
    wheels, sources = list(directory.glob("*.whl")), list(directory.glob("firefly_forge-*.tar.gz"))
    if len(wheels) != 1 or len(sources) != 1:
        raise ValueError("Expected exactly one wheel and source distribution")
    names = subprocess.check_output(["git", "-C", str(root), "ls-files", "-z"]).decode().split("\0")
    tracked = {name: (root / name).read_bytes() for name in names if name}
    studio = {}
    for path in (root / "studio" / "dist").rglob("*"):
        if path.is_symlink():
            raise ValueError("Rebuilt Studio contains a symbolic link")
        if path.is_file():
            studio[path.relative_to(root / "studio" / "dist").as_posix()] = path.read_bytes()
    if "index.html" not in studio:
        raise ValueError("Rebuild Studio before verifying release payloads")
    with zipfile.ZipFile(wheels[0]) as archive:
        entries = archive.infolist()
        if len({item.filename for item in entries}) != len(entries):
            raise ValueError("Wheel has duplicate members")
        for item in entries:
            _safe(item.filename)
            if item.is_dir() or stat.S_IFMT(item.external_attr >> 16) not in {0, stat.S_IFREG}:
                raise ValueError("Wheel contains a nonregular member")
        wheel = {item.filename: archive.read(item) for item in entries}
    prefix = f"firefly_forge-{project['version']}.dist-info/"
    payload = {
        **{name.removeprefix("src/"): data for name, data in tracked.items() if name.startswith("src/firefly_forge/")},
        **{"firefly_forge/static/" + name: data for name, data in studio.items()},
        **{
            "firefly_forge/fonts/manrope/" + name.removeprefix("assets/fonts/manrope/"): data
            for name, data in tracked.items()
            if name.startswith("assets/fonts/manrope/")
        },
    }
    metadata_names = {
        prefix + name
        for name in ("METADATA", "WHEEL", "RECORD", "entry_points.txt", "licenses/LICENSE", "licenses/NOTICE")
    }
    if set(wheel) != set(payload) | metadata_names or any(wheel.get(name) != data for name, data in payload.items()):
        raise ValueError("Wheel runtime, fonts or Studio differ from source")
    for name in ("LICENSE", "NOTICE"):
        if wheel[prefix + "licenses/" + name] != tracked[name]:
            raise ValueError("Wheel legal attribution differs from source")
    _metadata(wheel[prefix + "METADATA"], project, tracked["README.md"])
    points = configparser.ConfigParser()
    points.read_string(wheel[prefix + "entry_points.txt"].decode())
    if points.sections() != ["console_scripts"] or dict(points["console_scripts"]) != project["scripts"]:
        raise ValueError("Wheel entrypoint differs from source")
    info = BytesParser().parsebytes(wheel[prefix + "WHEEL"])
    if info.get_all("Root-Is-Purelib") != ["true"] or info.get_all("Tag") != ["py3-none-any"]:
        raise ValueError("Unexpected wheel platform metadata")
    rows = list(csv.reader(io.StringIO(wheel[prefix + "RECORD"].decode())))
    if len(rows) != len(wheel) or any(len(row) != 3 for row in rows) or {row[0] for row in rows} != set(wheel):
        raise ValueError("Wheel RECORD inventory differs")
    for name, digest, size in rows:
        if name == prefix + "RECORD":
            if digest or size:
                raise ValueError("Wheel RECORD cannot certify itself")
        else:
            wanted = "sha256=" + base64.urlsafe_b64encode(hashlib.sha256(wheel[name]).digest()).decode().rstrip("=")
            if digest != wanted or size != str(len(wheel[name])):
                raise ValueError("Wheel RECORD digest or size differs")
    source = {}
    with tarfile.open(sources[0]) as archive:
        for member in archive:
            _safe(member.name)
            parts = member.name.split("/", 1)
            if (
                not member.isfile()
                or len(parts) != 2
                or parts[0] != "firefly_forge-" + project["version"]
                or parts[1] in source
            ):
                raise ValueError("Source distribution has an unexpected member")
            handle = archive.extractfile(member)
            if handle is None:
                raise ValueError("Source member cannot be read")
            source[parts[1]] = handle.read()
    expected = {**tracked, **{"studio/dist/" + name: data for name, data in studio.items()}}
    if set(source) != set(expected) | {"PKG-INFO"} or any(source.get(name) != data for name, data in expected.items()):
        raise ValueError("Source distribution differs from the publishing checkout")
    _metadata(source["PKG-INFO"], project, tracked["README.md"])


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--tag")
    args = parser.parse_args()
    verify_payloads(args.directory, Path(__file__).resolve().parents[1], tag=args.tag)
