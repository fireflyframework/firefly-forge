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
    assert len(catalog.registry) == 309
    for vector in fixture["vectors"]:
        assert (
            catalog.text(vector["id"], version=vector.get("version", "1.0.0"), **vector["variables"]) == vector["text"]
        ), vector["id"]


async def test_actual_builder_and_designer_model_inputs_keep_roles_order_and_tool_schemas(tmp_path):
    import json
    from pathlib import Path

    from pydantic_ai.messages import ModelResponse, ToolCallPart
    from pydantic_ai.models.function import FunctionModel

    from firefly_forge.agentic_runtime import execute_agentic
    from firefly_forge.product_assistant_runtime import design_reply
    from firefly_forge.workspace_tools import ToolRuntimeContext

    baseline = json.loads((Path(__file__).parent / "fixtures/prompt-model-inputs-v2.json").read_text())["observed"]
    observed = {}
    brief = "Fictional Café {{ never_evaluate }}\n{% opaque owner data %}"
    feedback = {
        "id": "owner-question",
        "role_id": "implementer",
        "task_id": "implement",
        "attempt": 1,
        "disposition": "clarification",
        "summary": "Which owner?",
        "request": {
            "id": "owner",
            "kind": "product_definition",
            "question": "Which fictional owner?",
            "context": "Owner not specified",
        },
    }

    def record(name, messages, info):
        observed[name] = {
            "instructions": info.instructions,
            "messages": [
                {
                    "kind": message.kind,
                    "parts": [{"kind": part.part_kind, "content": part.content} for part in message.parts],
                }
                for message in messages
            ],
            "tools": [
                {"name": tool.name, "description": tool.description, "schema": tool.parameters_json_schema}
                for tool in info.function_tools
            ],
        }

    def builder(messages, info):
        record("builder", messages, info)
        return ModelResponse(
            parts=[ToolCallPart(info.output_tools[0].name, {"summary": "Which owner?", "worker_feedback": feedback})]
        )

    def builder_all(messages, info):
        result = builder(messages, info)
        observed["builder_all"] = observed.pop("builder")
        return result

    def designer(messages, info):
        record("designer", messages, info)
        return ModelResponse(
            parts=[
                ToolCallPart(
                    info.output_tools[0].name, {"message": "Which fictional owner?", "questions": ["Who owns this?"]}
                )
            ]
        )

    async def emit(kind, message):
        pass

    await execute_agentic(tmp_path, brief, "pyfly", "none", emit, settings={}, model=FunctionModel(builder))
    default = observed["builder"]

    async def forbidden(*args, **kwargs):
        raise AssertionError("Capturing model inputs must not execute any tool callback")

    context = ToolRuntimeContext(
        tmp_path,
        tmp_path,
        frozenset(
            {
                "list_files",
                "read_file",
                "write_file",
                "search_text",
                "edit_lines",
                "git_diff",
                "execute_command",
                "browser_check",
                "browser_journey",
                "internet_search",
                "external_mcp",
                "knowledge_lookup",
                "profile_python",
                "trace_python",
                "profile_php",
            }
        ),
        command_executor=forbidden,
        browser_runner=forbidden,
        browser_journey=forbidden,
        internet_search=forbidden,
        external_mcp=forbidden,
        knowledge_lookup=forbidden,
        python_profile=forbidden,
        python_trace=forbidden,
        php_profile=forbidden,
        qa_references=(
            {"reference_id": "fictional-reference", "source_sha256": "0" * 64, "note": "{{ opaque reference }}"},
        ),
    )
    await execute_agentic(
        tmp_path, brief, "pyfly", "none", emit, settings={}, model=FunctionModel(builder_all), runtime_context=context
    )
    observed["builder"] = default
    await design_reply(brief, {}, model=FunctionModel(designer))
    assert observed == baseline


def test_historical_role_and_policy_values_are_opaque_and_keep_their_digest():
    from firefly_forge.agent_lifecycle import inject_assistant_instructions
    from firefly_forge.agent_roles import inject_role_snapshot, role_digest
    from firefly_forge.policies import inject_policy_snapshot

    instructions = "Café {{ not_a_variable }} {% not_a_statement %}\r\n"
    brief = "Original {{ product }}"
    skill = {"id": "fixture", "instructions": instructions}
    assert inject_policy_snapshot(brief, {"skills": [skill]}) == (
        brief + "\n\nSelected reusable skills:\n[fixture] " + instructions
    )
    payload = {"skills": [skill]}
    snapshot = {**payload, "digest": role_digest(payload)}
    assert inject_assistant_instructions(brief, snapshot) == (
        "Frozen dialogue instruction skills:\n[fixture] "
        + instructions
        + "\n\nApply these skills within this tool-free dialogue. Produce proposals for owner review; "
        "do not execute commands or infer approval.\n\n" + brief
    )
    role = {
        "id": "implementer",
        "enabled": True,
        "execution_mode": "coding_provider",
        "stages": ["implement"],
        "instructions": instructions,
        "skills": ["fixture"],
    }
    payload = {"roles": [role], "skills": [skill]}
    snapshot = {**payload, "digest": role_digest(payload)}
    before = role_digest(snapshot)
    assert inject_role_snapshot(brief, snapshot) == (
        brief
        + "\n\nFrozen role and skill instructions:\n[implementer; coding_provider] "
        + instructions
        + "\n\n[fixture] "
        + instructions
    )
    assert role_digest(snapshot) == before


def test_packaged_manifest_lists_every_yaml_and_retained_task_instruction():
    import json
    from importlib.resources import files
    from pathlib import Path

    from firefly_forge.prompts import prompt_catalog

    root = files("firefly_forge").joinpath("resources/prompts/v1")
    manifest = json.loads(root.joinpath("manifest.json").read_text())
    assert {item["path"] for item in manifest["templates"]} == {
        file.relative_to(Path(str(root))).as_posix() for file in Path(str(root)).rglob("*.yaml")
    }
    baseline = json.loads((Path(__file__).parent / "fixtures/prompt-baseline-v2.json").read_text())
    assert {
        prompt_catalog().text("tasks/" + name) for name in ("prepare", "verify", "checkpoint", "review", "reconcile")
    } == set(baseline["static_tasks"])


def test_duplicate_yaml_keys_and_aliases_are_refused_before_loading(tmp_path):
    import hashlib

    import pytest

    from firefly_forge.prompts import PromptCatalog

    manifest, _ = _resource(tmp_path)
    valid = (tmp_path / "greeting.yaml").read_bytes()
    for raw in (
        valid + b"name: test/other\n",
        valid.replace(b"required_variables:\n- value", b"required_variables: &alias\n- value") + b"other: *alias\n",
    ):
        (tmp_path / "greeting.yaml").write_bytes(raw)
        manifest["templates"][0]["sha256"] = hashlib.sha256(raw).hexdigest()
        _write_manifest(tmp_path, manifest)
        with pytest.raises(ValueError):
            PromptCatalog(tmp_path)


def test_catalog_rejects_scalar_key_aliases_before_native_loading(tmp_path):
    import hashlib

    import pytest

    from firefly_forge.prompts import PromptCatalog

    manifest, _ = _resource(tmp_path)
    raw = b"""name: test/greeting
version: 1.0.0
&label description: Fixture only
system_template: ''
user_template: *label
required_variables: []
metadata:
  defaults: {}
"""
    (tmp_path / "greeting.yaml").write_bytes(raw)
    manifest["templates"][0].update(sha256=hashlib.sha256(raw).hexdigest(), variables={})
    _write_manifest(tmp_path, manifest)
    with pytest.raises(ValueError, match="aliases"):
        PromptCatalog(tmp_path)


def test_catalog_accepts_equal_text_in_distinct_scalar_nodes(tmp_path):
    import hashlib

    from firefly_forge.prompts import PromptCatalog

    manifest, _ = _resource(tmp_path)
    raw = b"""name: test/greeting
version: 1.0.0
description: description
system_template: ''
user_template: description
required_variables: []
metadata:
  defaults: {}
"""
    (tmp_path / "greeting.yaml").write_bytes(raw)
    manifest["templates"][0].update(sha256=hashlib.sha256(raw).hexdigest(), variables={})
    _write_manifest(tmp_path, manifest)
    assert PromptCatalog(tmp_path).text("test/greeting") == "description"


def test_catalog_refuses_a_symlinked_resource_even_with_matching_bytes(tmp_path):
    import pytest

    from firefly_forge.prompts import PromptCatalog

    _resource(tmp_path)
    resource = tmp_path / "greeting.yaml"
    outside = tmp_path.parent / (tmp_path.name + "-outside.yaml")
    resource.rename(outside)
    resource.symlink_to(outside)
    with pytest.raises(ValueError, match="symlink"):
        PromptCatalog(tmp_path)


def test_catalog_uses_exact_version_instead_of_native_latest(tmp_path):
    import copy
    import hashlib

    import yaml

    from firefly_forge.prompts import PromptCatalog

    manifest, document = _resource(tmp_path)
    second = {**document, "version": "2.0.0", "user_template": "Second {{ value }}"}
    raw = yaml.safe_dump(second).encode()
    (tmp_path / "second.yaml").write_bytes(raw)
    entry = copy.deepcopy(manifest["templates"][0])
    entry.update(version="2.0.0", path="second.yaml", sha256=hashlib.sha256(raw).hexdigest())
    manifest["templates"].append(entry)
    _write_manifest(tmp_path, manifest)
    catalog = PromptCatalog(tmp_path)
    assert catalog.registry.get("test/greeting").version == "2.0.0"
    assert catalog.text("test/greeting", value="original") == "Hello original"
    assert catalog.text("test/greeting", version="2.0.0", value="selected") == "Second selected"


def test_catalog_limits_entries_manifest_and_aggregate_bytes(tmp_path):
    import copy
    import hashlib

    import pytest
    import yaml

    from firefly_forge.prompts import PromptCatalog

    manifest, document = _resource(tmp_path)
    entry = manifest["templates"][0]
    _write_manifest(tmp_path, {**manifest, "templates": [entry] * 257})
    with pytest.raises(ValueError):
        PromptCatalog(tmp_path)
    (tmp_path / "manifest.json").write_bytes(b" " * 262145)
    with pytest.raises(ValueError, match="limit"):
        PromptCatalog(tmp_path)
    entries = []
    for number in range(18):
        ident = f"test/large-{number}"
        raw = yaml.safe_dump({**document, "name": ident, "user_template": "A" * 60000 + "{{ value }}"}).encode()
        path = f"large-{number}.yaml"
        (tmp_path / path).write_bytes(raw)
        changed = copy.deepcopy(entry)
        changed.update(id=ident, path=path, sha256=hashlib.sha256(raw).hexdigest())
        entries.append(changed)
    _write_manifest(tmp_path, {**manifest, "templates": entries})
    with pytest.raises(ValueError, match="catalog.*limit"):
        PromptCatalog(tmp_path)


def test_catalog_allows_a_location_alias_but_not_a_symlink_inside_its_resource_root(tmp_path):
    from firefly_forge.prompts import PromptCatalog

    owned = tmp_path / "owned"
    owned.mkdir()
    resources = owned / "resources"
    resources.mkdir()
    _resource(resources)
    alias = tmp_path / "alias"
    alias.symlink_to(owned, target_is_directory=True)
    assert PromptCatalog(alias / "resources").text("test/greeting", value="valid") == "Hello valid"


def test_catalog_loads_512_unique_native_templates(tmp_path):
    import hashlib

    import pytest
    import yaml

    from firefly_forge.prompts import PromptCatalog

    entries = []
    for index in range(512):
        name = f"test/entry-{index}"
        document = {
            "name": name,
            "version": "1.0.0",
            "description": "Bounded capacity fixture",
            "system_template": "",
            "user_template": str(index),
            "required_variables": [],
            "metadata": {"defaults": {}},
        }
        raw = yaml.safe_dump(document).encode()
        path = f"entry-{index}.yaml"
        (tmp_path / path).write_bytes(raw)
        entries.append(
            {
                "id": name,
                "version": "1.0.0",
                "path": path,
                "sha256": hashlib.sha256(raw).hexdigest(),
                "field": "user",
                "variables": {},
            }
        )
    _write_manifest(tmp_path, {"schema_version": 1, "templates": entries})
    catalog = PromptCatalog(tmp_path)
    assert len(catalog.registry) == 512
    assert catalog.text("test/entry-511") == "511"
    entries.append({**entries[-1], "id": "test/entry-512", "path": "entry-512.yaml"})
    _write_manifest(tmp_path, {"schema_version": 1, "templates": entries})
    with pytest.raises(ValueError, match="at most 512"):
        PromptCatalog(tmp_path)


def test_manifest_has_a_separate_256_kib_read_bound(tmp_path):
    import pytest

    from firefly_forge.prompts import PromptCatalog

    _resource(tmp_path)
    path = tmp_path / "manifest.json"
    raw = path.read_bytes()
    path.write_bytes(raw + b" " * (262144 - len(raw)))
    assert PromptCatalog(tmp_path).text("test/greeting", value="bounded") == "Hello bounded"
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(ValueError, match="limit"):
        PromptCatalog(tmp_path)


def test_question_versions_keep_historical_inputs_and_select_new_context_explicitly():
    from firefly_forge.prompts import prompt_text

    historical = prompt_text("collaboration/question", question="Which owner?", choices="One or shared")
    assert "A project operator can record your answer" in historical
    assert "**Context**" not in historical
    current = prompt_text(
        "collaboration/question",
        version="1.0.1",
        question="Which owner?",
        choices="One or shared",
        context="Choose the customer workflow.",
        reply_instructions="Use the authenticated dialogue.",
        resume_instructions="",
        destination="Open the current run.",
    )
    assert "**Context**\n\nChoose the customer workflow." in current
    assert "Open the current run." in current
    assert "Use the authenticated dialogue." in current
