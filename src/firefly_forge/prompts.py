# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Validated packaged resources rendered by native Firefly Agentic templates."""

from __future__ import annotations

import hashlib
import json
from functools import cache
from importlib.resources import as_file, files
from importlib.resources.abc import Traversable
from pathlib import Path, PurePosixPath
from typing import Any, Literal

import yaml  # type: ignore[import-untyped]
from fireflyframework_agentic.prompts import PromptLoader, PromptRegistry
from jinja2 import Environment, meta, nodes
from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

_MAX_FILE = 65536
_MAX_CATALOG = 1048576


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class _Entry(_Strict):
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]*(/[a-z][a-z0-9_-]*){1,5}$", max_length=160)
    version: str = Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$", max_length=32)
    path: str = Field(min_length=1, max_length=200)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    field: Literal["system", "user"]
    variables: dict[str, Literal["string"]] = Field(max_length=32)

    @model_validator(mode="after")
    def confined_path(self) -> _Entry:
        path = PurePosixPath(self.path)
        if (
            path.is_absolute()
            or path.as_posix() != self.path
            or ".." in path.parts
            or "\\" in self.path
            or path.suffix != ".yaml"
        ):
            raise ValueError("Prompt resource path must be a confined YAML path")
        return self


class _Manifest(_Strict):
    schema_version: StrictInt
    templates: list[_Entry] = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def unique_templates(self) -> _Manifest:
        keys = [(entry.id, entry.version) for entry in self.templates]
        paths = [entry.path for entry in self.templates]
        if self.schema_version != 1 or len(set(keys)) != len(keys) or len(set(paths)) != len(paths):
            raise ValueError("Prompt manifest version, identities and paths must be unique")
        return self


class _Metadata(_Strict):
    defaults: dict[str, str] = Field(default_factory=dict, max_length=32)


class _Document(_Strict):
    name: str
    version: str
    description: str
    system_template: str
    user_template: str
    required_variables: list[str] = Field(max_length=32)
    metadata: _Metadata


def _unique_json(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate prompt manifest key")
        result[key] = value
    return result


def _read(resource: Traversable, root: Traversable) -> bytes:
    if isinstance(resource, Path) and isinstance(root, Path):
        current = root
        for part in ("", *resource.relative_to(root).parts):
            current = current / part
            if current.is_symlink():
                raise ValueError("Prompt resources cannot use symlinks")
    with resource.open("rb") as stream:
        data = stream.read(_MAX_FILE + 1)
    if len(data) > _MAX_FILE:
        raise ValueError("Prompt resource exceeds its byte limit")
    return data


def _document(raw: bytes) -> _Document:
    tree = yaml.compose(raw)
    if tree is None:
        raise ValueError("Prompt resource is empty")
    # SafeLoader otherwise silently replaces duplicate mapping keys.
    pending = [tree]
    seen: set[int] = set()
    while pending:
        node = pending.pop()
        if id(node) in seen:
            raise ValueError("Prompt YAML aliases are not supported")
        seen.add(id(node))
        if isinstance(node, yaml.MappingNode):
            keys = [key.value for key, _ in node.value if isinstance(key, yaml.ScalarNode)]
            if len(keys) != len(node.value) or len(set(keys)) != len(keys):
                raise ValueError("Prompt YAML keys must be unique strings")
            pending.extend(value for _, value in node.value)
        elif isinstance(node, yaml.SequenceNode):
            pending.extend(node.value)
    return _Document.model_validate(yaml.safe_load(raw))


class PromptCatalog:
    """A private native registry for a bounded, digest-bound packaged catalog."""

    def __init__(self, root: Traversable) -> None:
        raw_manifest = _read(root.joinpath("manifest.json"), root)
        manifest = _Manifest.model_validate(json.loads(raw_manifest, object_pairs_hook=_unique_json))
        self.digest = hashlib.sha256(raw_manifest).hexdigest()
        self.registry = PromptRegistry()
        self._entries: dict[tuple[str, str], tuple[_Entry, _Metadata]] = {}
        total = len(raw_manifest)
        environment = Environment()
        for entry in manifest.templates:
            resource = root.joinpath(entry.path)
            raw = _read(resource, root)
            total += len(raw)
            if total > _MAX_CATALOG:
                raise ValueError("Prompt catalog exceeds its byte limit")
            if hashlib.sha256(raw).hexdigest() != entry.sha256:
                raise ValueError("Prompt resource digest does not match its manifest")
            document = _document(raw)
            required = document.required_variables
            defaults = document.metadata.defaults
            if (
                (document.name, document.version) != (entry.id, entry.version)
                or len(set(required)) != len(required)
                or set(required) & defaults.keys()
                or set(required) | defaults.keys() != entry.variables.keys()
            ):
                raise ValueError("Prompt identity or variable declaration does not match its manifest")
            used: set[str] = set()
            for source in (document.system_template, document.user_template):
                parsed = environment.parse(source)
                if any(
                    not isinstance(node, (nodes.Output, nodes.TemplateData, nodes.Name))
                    or isinstance(node, nodes.Name)
                    and node.ctx != "load"
                    for node in parsed.find_all(nodes.Node)
                ):
                    raise ValueError("Prompt templates support plain variable insertion only")
                used.update(meta.find_undeclared_variables(parsed))
            if used != entry.variables.keys():
                raise ValueError("Prompt Jinja variables do not match their declaration")
            if getattr(document, "user_template" if entry.field == "system" else "system_template"):
                raise ValueError("Prompt resource has an unused message field")
            with as_file(resource) as path:
                template = PromptLoader.from_file(path)
                if hashlib.sha256(path.read_bytes()).hexdigest() != entry.sha256:
                    raise ValueError("Prompt resource changed while loading")
            self.registry.register(template)
            self._entries[(entry.id, entry.version)] = (entry, document.metadata)

    def text(self, name: str, *, version: str = "1.0.0", **variables: str) -> str:
        selection = self._entries.get((name, version))
        if selection is None:
            raise ValueError("Unknown packaged prompt identity or version")
        entry, metadata = selection
        values = {**metadata.defaults, **variables}
        if values.keys() != entry.variables.keys() or any(type(value) is not str for value in values.values()):
            raise ValueError("Prompt variables must match the declared string contract")
        rendered = self.registry.get(name, version).render(**values)
        return str(getattr(rendered, entry.field))


@cache
def prompt_catalog() -> PromptCatalog:
    return PromptCatalog(files("firefly_forge").joinpath("resources/prompts/v1"))


def prompt_text(name: str, **variables: str) -> str:
    return prompt_catalog().text(name, **variables)
