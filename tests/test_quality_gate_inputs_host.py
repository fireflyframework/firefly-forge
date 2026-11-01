# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Tool and rule prerequisites come from the selected component, never a new approval."""

import hashlib
import json

import pytest

from firefly_forge.quality_gate_models import QualityGateContractV1


def config(root, *, version="1.2.3"):
    root.mkdir(parents=True, exist_ok=True)
    rules = "[mypy]\nstrict = True\n"
    (root / "quality.ini").write_text(rules)
    (root / "uv.lock").write_text('version = 1\n[[package]]\nname = "mypy"\nversion = "' + version + '"\n')
    return QualityGateContractV1.model_validate(
        {
            "tool": {
                "package": "mypy",
                "name": "mypy",
                "ecosystem": "python",
                "lock_file": "uv.lock",
                "probe": {"argv": ["python", "-B", "-I", "-m", "mypy", "--version"]},
                "approved_version": "1.2.3",
                "version_prefix": "mypy ",
            },
            "category": "types",
            "configuration_paths": ["quality.ini"],
            "approved_configuration": {"quality.ini": hashlib.sha256(rules.encode()).hexdigest()},
        }
    )


async def test_prerequisites_use_the_component_lock_and_configuration_not_repository_root(tmp_path):
    from firefly_forge.quality_gate_inputs import prepare_quality_project_inputs

    config(tmp_path, version="0.0.1")
    component = tmp_path / "apps" / "api"
    spec = config(component)
    prepared = await prepare_quality_project_inputs(component, spec)
    assert prepared.version == "1.2.3"
    assert prepared.lock_digest == hashlib.sha256((component / "uv.lock").read_bytes()).hexdigest()
    assert prepared.configuration == spec.approved_configuration
    await prepared.assert_current()


async def test_weakened_config_is_refused_even_after_a_fresh_input_capture(tmp_path):
    from firefly_forge.quality_gate_inputs import QualityRequirementsError, prepare_quality_project_inputs

    spec = config(tmp_path)
    prepared = await prepare_quality_project_inputs(tmp_path, spec)
    (tmp_path / "quality.ini").write_text("[mypy]\nignore_errors = True\n")
    with pytest.raises(QualityRequirementsError, match="approved"):
        await prepared.assert_current()
    with pytest.raises(QualityRequirementsError, match="approved"):
        await prepare_quality_project_inputs(tmp_path, spec)


async def test_lock_update_does_not_update_the_approved_tool_version(tmp_path):
    from firefly_forge.quality_gate_inputs import QualityRequirementsError, prepare_quality_project_inputs

    spec = config(tmp_path)
    prepared = await prepare_quality_project_inputs(tmp_path, spec)
    (tmp_path / "uv.lock").write_text('version = 1\n[[package]]\nname = "mypy"\nversion = "0.1"\n')
    with pytest.raises(QualityRequirementsError, match="approved"):
        await prepare_quality_project_inputs(tmp_path, spec)
    with pytest.raises(QualityRequirementsError):
        await prepared.assert_current()


async def test_prerequisite_paths_cannot_follow_a_link_into_another_project(tmp_path):
    from firefly_forge.quality_gate_inputs import QualityRequirementsError, prepare_quality_project_inputs

    component = tmp_path / "api"
    spec = config(component)
    (tmp_path / "other.ini").write_text("private metadata")
    (component / "quality.ini").unlink()
    (component / "quality.ini").symlink_to(tmp_path / "other.ini")
    with pytest.raises(QualityRequirementsError):
        await prepare_quality_project_inputs(component, spec)


@pytest.mark.parametrize("ecosystem", ["python", "composer", "node"])
def test_locked_version_uses_one_unambiguous_real_package_entry(ecosystem):
    from firefly_forge.quality_gate_inputs import locked_quality_version

    package = {"python": "mypy", "composer": "phpstan/phpstan", "node": "typescript"}[ecosystem]
    raw = {
        "python": '[[package]]\nname="mypy"\nversion="1.2.3"\n',
        "composer": json.dumps({"packages": [{"name": package, "version": "1.2.3"}]}),
        "node": json.dumps({"lockfileVersion": 3, "packages": {"node_modules/" + package: {"version": "1.2.3"}}}),
    }[ecosystem]
    assert locked_quality_version(ecosystem, package, raw) == "1.2.3"


@pytest.mark.parametrize(
    "raw",
    [
        '[[package]]\nname="mypy"\nversion="1.2.3"\n[[package]]\nname="mypy"\nversion="0.1"\n',
        '[[package]]\nname="other-package"\nversion="1.2.3"\n',
    ],
)
def test_ambiguous_or_missing_tool_lock_entries_never_select_a_convenient_version(raw):
    from firefly_forge.quality_gate_inputs import QualityRequirementsError, locked_quality_version

    with pytest.raises(QualityRequirementsError):
        locked_quality_version("python", "mypy", raw)


async def test_complete_large_lock_has_no_inherited_one_megabyte_tool_response_cap(tmp_path):
    from firefly_forge.quality_gate_inputs import QualityRequirementsError, prepare_quality_project_inputs
    from firefly_forge.source_catalog import SourceCatalogBudget

    packages = {
        "node_modules/dependency-" + str(i): {
            "version": "1.2.3",
            "resolved": "https://registry.invalid/dependency-" + str(i),
            "integrity": "sha512-" + "a" * 100,
        }
        for i in range(6000)
    }
    packages["node_modules/typescript"] = {"version": "1.2.3"}
    raw = json.dumps({"lockfileVersion": 3, "packages": packages}).encode()
    assert len(raw) > 1_000_000
    (tmp_path / "package-lock.json").write_bytes(raw)
    spec = QualityGateContractV1.model_validate(
        {
            "tool": {
                "package": "typescript",
                "name": "TypeScript",
                "ecosystem": "node",
                "lock_file": "package-lock.json",
                "probe": {"argv": ["node", "node_modules/typescript/bin/tsc", "--version"]},
                "approved_version": "1.2.3",
                "version_prefix": "Version ",
            },
        }
    )
    prepared = await prepare_quality_project_inputs(tmp_path, spec)
    try:
        assert prepared.lock_digest == hashlib.sha256(raw).hexdigest()
        assert prepared.version == "1.2.3"
        prepared.fence()
    finally:
        prepared.close()
    with pytest.raises(QualityRequirementsError, match="configured"):
        await prepare_quality_project_inputs(tmp_path, spec, budget=SourceCatalogBudget(max_source_bytes=len(raw) - 1))


async def test_composer_stable_prefix_is_canonical_without_losing_raw_provenance(tmp_path):
    from firefly_forge.quality_gate_inputs import prepare_quality_project_inputs

    raw = json.dumps({"packages": [{"name": "phpstan/phpstan", "version": "v1.2.3"}]})
    (tmp_path / "composer.lock").write_text(raw)
    spec = QualityGateContractV1.model_validate(
        {
            "tool": {
                "package": "phpstan/phpstan",
                "name": "PHPStan",
                "ecosystem": "composer",
                "lock_file": "composer.lock",
                "probe": {"argv": ["php", "vendor/bin/phpstan", "--version"]},
                "approved_version": "1.2.3",
                "version_prefix": "PHPStan - PHP Static Analysis Tool ",
            },
        }
    )
    prepared = await prepare_quality_project_inputs(tmp_path, spec)
    try:
        assert prepared.version == "1.2.3" and prepared.raw_version == "v1.2.3"
        assert prepared.lock_digest == hashlib.sha256(raw.encode()).hexdigest()
    finally:
        prepared.close()


@pytest.mark.parametrize("version", ["dev-main", "1.x-dev", "^1.2", "vv1.2.3", "1.2.3 as 9.9.9"])
def test_composer_aliases_and_ranges_are_not_exact_approved_tool_versions(version):
    from firefly_forge.quality_gate_inputs import QualityRequirementsError, locked_quality_version

    with pytest.raises(QualityRequirementsError):
        locked_quality_version(
            "composer", "phpstan/phpstan", json.dumps({"packages": [{"name": "phpstan/phpstan", "version": version}]})
        )
