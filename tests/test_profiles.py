# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
import pytest

from firefly_forge.profiles import build_instruction, profile_catalog, verification_commands


def test_profile_catalog_describes_real_supported_frameworks():
    catalog = profile_catalog()
    assert {p["id"] for p in catalog["backends"]} >= {"pyfly", "java", "larafly", "rust", "dotnet"}
    assert [p["id"] for p in catalog["backends"]][:2] == ["pyfly", "larafly"]
    assert all("live" in p["validation"] for p in catalog["backends"] if p["id"] != "none")
    assert {p["id"] for p in catalog["frontends"]} >= {"angular", "react", "nextjs", "vue", "none"}
    angular = next(p for p in catalog["frontends"] if p["id"] == "angular")
    assert "22" in angular["version"]
    assert "ng-hub-ui" in angular["detail"]


def test_profile_catalog_separates_live_acceptance_from_inspected_contracts():
    catalog = profile_catalog()
    backends = {profile["id"]: profile for profile in catalog["backends"]}
    frontends = {profile["id"]: profile for profile in catalog["frontends"]}
    assert backends["pyfly"]["validation_state"] == "live_fixture_verified"
    assert backends["larafly"]["validation_state"] == "live_fixture_verified"
    assert frontends["react"]["validation_state"] == "live_fixture_verified"
    assert all(backends[name]["validation_state"] == "contract_inspected" for name in ["java", "rust", "dotnet"])
    assert frontends["angular"]["validation_state"] == "contract_inspected"


def test_instructions_preserve_arbitrary_brief_and_source_constraints():
    prompt = build_instruction("Build an inventory portal with barcode scanning.", "pyfly", "angular")
    assert "barcode scanning" in prompt
    assert "FireflyAgent" not in prompt
    assert "pyfly" in prompt.lower()
    assert "releases/download/v26.10.01/pyfly-26.10.1-py3-none-any.whl" in prompt
    assert "Pydantic>=2.13.3" in prompt
    assert "Uvicorn>=0.46.0" in prompt
    assert "hub-*" in prompt
    assert "Do not commit" in prompt
    assert "tests" in prompt


def test_unknown_profile_fails_before_generation():
    with pytest.raises(ValueError, match="Unknown backend"):
        build_instruction("hello", "unsupported", "react")


def test_verification_commands_use_project_scripts_and_package_manager(tmp_path):
    (tmp_path / "package.json").write_text(
        '{"scripts":{"build":"vite build","test":"vitest run","lint":"eslint .","typecheck":"tsc --noEmit"}}'
    )
    (tmp_path / "pnpm-lock.yaml").write_text("lockfileVersion: 9")
    commands = verification_commands(tmp_path, "none", "react")
    assert ["pnpm", "run", "build"] in commands
    assert ["pnpm", "run", "test"] in commands
    assert ["pnpm", "run", "lint"] in commands
    assert ["pnpm", "run", "typecheck"] in commands


def test_no_verification_configuration_is_not_a_passing_gate(tmp_path):
    with pytest.raises(ValueError, match="verification"):
        verification_commands(tmp_path, "none", "none")
