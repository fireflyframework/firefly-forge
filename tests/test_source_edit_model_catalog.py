# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""A pinned client presentation must never become mutable model capability metadata."""

import hashlib
import importlib
import json

import pytest


def catalog_module():
    try:
        module = importlib.import_module("firefly_forge.source_edit_model_catalog")
    except ModuleNotFoundError:
        module = None
    assert module is not None, "Restricted source execution requires a package-owned model catalog"
    return module


def test_derived_catalog_changes_only_the_explicit_client_tool_presentation():
    module = catalog_module()
    path = module.model_catalog_path("gpt-6-astra")
    assert path.read_bytes() == path.with_name("upstream-model.json").read_bytes().replace(
        b'"tool_mode": "code_mode_only"', b'"tool_mode": "direct"'
    )
    source = json.loads(path.with_name("upstream-model.json").read_text())
    derived = json.loads(path.read_text())
    before, after = source["models"][0], derived["models"][0]
    assert source.keys() == derived.keys() == {"models"}
    assert len(source["models"]) == len(derived["models"]) == 1
    assert {name for name in before.keys() | after.keys() if before.get(name) != after.get(name)} == {"tool_mode"}
    assert before["tool_mode"] == "code_mode_only" and after["tool_mode"] == "direct"
    assert after["shell_type"] == before["shell_type"] == "shell_command"
    instructions = after["model_messages"]["instructions_template"].encode()
    assert len(instructions) == 21428
    assert (
        hashlib.sha256(instructions).hexdigest() == "35bd51b5f577cb7b24cd5f4629e49e37cb724ab57754ce6f8f202001635bab8a"
    )


@pytest.mark.parametrize("kind", ["content", "symlink", "hardlink", "missing"])
def test_catalog_drift_or_alias_is_rejected(kind, tmp_path, monkeypatch):
    module = catalog_module()
    expected = module.model_catalog_path("gpt-6-astra").read_bytes()
    path, original = tmp_path / "catalog.json", tmp_path / "original.json"
    original.write_bytes(expected)
    if kind == "content":
        path.write_text("{}")
    elif kind == "symlink":
        path.symlink_to(original)
    elif kind == "hardlink":
        path.hardlink_to(original)
    monkeypatch.setattr(module, "CATALOG_PATH", path)
    with pytest.raises(ValueError, match="catalog"):
        module.model_catalog_path("gpt-6-astra")


def test_unqualified_model_cannot_select_the_pinned_catalog():
    module = catalog_module()
    with pytest.raises(ValueError, match="catalog"):
        module.model_catalog_path("future-model")
