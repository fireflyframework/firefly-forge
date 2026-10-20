# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Native template contract; all values are fictional and no provider is contacted."""

import importlib


def test_packaged_catalog_uses_native_agentic_templates():
    from fireflyframework_agentic.prompts import PromptRegistry, PromptTemplate

    available = importlib.util.find_spec("firefly_forge.prompts")
    assert available is not None, "Forge needs its validated native packaged prompt catalog"
    from firefly_forge.prompts import prompt_catalog

    catalog = prompt_catalog()
    assert isinstance(catalog.registry, PromptRegistry)
    template = catalog.registry.get("implementation/request", "1.0.0")
    assert isinstance(template, PromptTemplate)
    assert catalog.text("implementation/request", brief="Café {{ never_evaluate }}") == (
        "PRODUCT BRIEF:\nCafé {{ never_evaluate }}"
    )


def _resource(tmp_path, *, template="Hello {{ value }}", defaults=None):
    import hashlib
    import json

    import yaml

    document = {
        "name": "test/greeting",
        "version": "1.0.0",
        "description": "Fictional contract fixture",
        "system_template": "",
        "user_template": template,
        "required_variables": [] if defaults else ["value"],
        "metadata": {"defaults": defaults or {}},
    }
    raw = yaml.safe_dump(document, allow_unicode=True, sort_keys=False).encode()
    (tmp_path / "greeting.yaml").write_bytes(raw)
    manifest = {
        "schema_version": 1,
        "templates": [
            {
                "id": "test/greeting",
                "version": "1.0.0",
                "path": "greeting.yaml",
                "sha256": hashlib.sha256(raw).hexdigest(),
                "field": "user",
                "variables": {"value": "string"},
            }
        ],
    }
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    return manifest, document


def _write_manifest(path, manifest):
    import json

    (path / "manifest.json").write_text(json.dumps(manifest))


def test_native_render_has_strict_variables_and_opaque_values(tmp_path):
    import pytest

    from firefly_forge.prompts import PromptCatalog

    _resource(tmp_path)
    catalog = PromptCatalog(tmp_path)
    value = "Café {{ 6 * 7 }} {% include 'private' %} <script>\n"
    assert catalog.text("test/greeting", value=value) == "Hello " + value
    for variables in ({}, {"value": "ok", "extra": "bad"}, {"value": True}, {"value": {"x": "bad"}}):
        with pytest.raises(ValueError):
            catalog.text("test/greeting", **variables)
    for name in ("../greeting", "test/missing", "/greeting.yaml"):
        with pytest.raises(ValueError):
            catalog.text(name, value="ok")


def test_defaults_are_explicit_strings(tmp_path):
    from firefly_forge.prompts import PromptCatalog

    _resource(tmp_path, defaults={"value": "declared default"})
    assert PromptCatalog(tmp_path).text("test/greeting") == "Hello declared default"


def test_catalog_rejects_bad_manifest_before_native_registration(tmp_path):
    import copy

    import pytest

    from firefly_forge.prompts import PromptCatalog

    manifest, _ = _resource(tmp_path)
    variants = []
    duplicate = copy.deepcopy(manifest)
    duplicate["templates"].append(duplicate["templates"][0])
    variants.append(duplicate)
    for key, value in (
        ("path", "../greeting.yaml"),
        ("path", "/greeting.yaml"),
        ("sha256", "0" * 64),
        ("version", "latest"),
        ("field", "anything"),
    ):
        changed = copy.deepcopy(manifest)
        changed["templates"][0][key] = value
        variants.append(changed)
    variants.append({**manifest, "schema_version": True})
    for changed in variants:
        _write_manifest(tmp_path, changed)
        with pytest.raises(ValueError):
            PromptCatalog(tmp_path)


def test_catalog_rejects_authoring_typos_and_executable_jinja(tmp_path):
    import hashlib

    import pytest
    import yaml

    from firefly_forge.prompts import PromptCatalog

    manifest, document = _resource(tmp_path)
    for change in (
        {"user_template": "Hello {{ typo }}"},
        {"user_template": "{% include value %}"},
        {"user_template": "{{ value.upper() }}"},
        {"user_template": "{{ value | upper }}"},
        {"extra": "ignored is unsafe"},
        {"required_variables": ["value", "value"]},
        {"metadata": {"defaults": {"value": False}}},
        {"version": 1},
        {"name": "different/name"},
    ):
        raw = yaml.safe_dump({**document, **change}).encode()
        (tmp_path / "greeting.yaml").write_bytes(raw)
        manifest["templates"][0]["sha256"] = hashlib.sha256(raw).hexdigest()
        _write_manifest(tmp_path, manifest)
        with pytest.raises(ValueError):
            PromptCatalog(tmp_path)


def test_catalog_limits_resource_bytes(tmp_path):
    import hashlib

    import pytest

    from firefly_forge.prompts import PromptCatalog

    manifest, _ = _resource(tmp_path)
    raw = b"#" * 65537
    (tmp_path / "greeting.yaml").write_bytes(raw)
    manifest["templates"][0]["sha256"] = hashlib.sha256(raw).hexdigest()
    _write_manifest(tmp_path, manifest)
    with pytest.raises(ValueError, match="limit"):
        PromptCatalog(tmp_path)


def test_reviewed_defaults_and_composed_profiles_stay_literal():
    import json
    from pathlib import Path

    from firefly_forge.agent_roles import _default_document
    from firefly_forge.profiles import _BACKENDS, _FRONTENDS, build_instruction, profile_catalog
    from firefly_forge.qa import qa_instructions
    from firefly_forge.skills import inject_skills, skill_catalog

    baseline = json.loads((Path(__file__).parent / "fixtures/prompt-baseline-v2.json").read_text())
    assert profile_catalog() == baseline["profile_catalog"]
    assert skill_catalog() == baseline["skill_catalog"]
    assert _default_document().model_dump(mode="json") == baseline["role_document"]
    assert qa_instructions() == baseline["qa"]
    assert {
        f"{kind}/{item['id']}": item["instruction"]
        for kind, entries in [("backend", _BACKENDS), ("frontend", _FRONTENDS)]
        for item in entries
    } == baseline["profiles"]
    for case in baseline["composed_profiles"]:
        assert build_instruction(case["brief"], case["backend"], case["frontend"]) == case["text"]
    for case in baseline["skills"]:
        assert inject_skills(case["brief"], case["selected"]) == case["text"]


def test_every_packaged_resource_matches_its_reviewed_text_vector():
    import json
    from pathlib import Path

    from firefly_forge.prompts import prompt_catalog

    fixture = json.loads((Path(__file__).parent / "fixtures/prompt-template-vectors-v2.json").read_text())
    catalog = prompt_catalog()
    assert len(fixture["vectors"]) == 77
    assert len(catalog.registry) == 258
