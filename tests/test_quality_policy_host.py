# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Project quality defaults are versioned configuration, never agent waivers."""

import copy

import pytest

from firefly_forge.journal import digest
from firefly_forge.policies import PolicyCatalog, policy_commands


@pytest.mark.parametrize(
    "backend,frontend,required,forbidden",
    [
        ("pyfly", "none", {"ruff", "mypy"}, {"laravel/pint", "phpstan/phpstan", "typescript", "eslint"}),
        ("larafly", "none", {"laravel/pint", "phpstan/phpstan"}, {"ruff", "mypy", "typescript", "eslint"}),
        ("none", "react", {"typescript", "eslint"}, {"ruff", "mypy", "laravel/pint", "phpstan/phpstan"}),
        ("pyfly", "react", {"ruff", "mypy", "typescript", "eslint"}, {"laravel/pint", "phpstan/phpstan"}),
    ],
)
def test_baselines_only_require_tools_for_the_selected_component(backend, frontend, required, forbidden):
    from firefly_forge.quality_policies import ProjectQualityPolicyV1, baseline_quality_policies

    policy = ProjectQualityPolicyV1(tool_versions={name: "1.2.3" for name in required})
    skills = baseline_quality_policies(policy, backend, frontend)
    packages = {command["quality"]["tool"]["package"] for skill in skills for command in skill["commands"]}
    assert required <= packages and not packages.intersection(forbidden)
    assert all(skill["kind"] == "quality_gate" for skill in skills)
    assert all(command["quality"]["tool"]["approved_version"] == "1.2.3" for s in skills for command in s["commands"])


def test_missing_approved_tool_versions_remain_explicit_unready_prerequisites():
    from firefly_forge.quality_policies import ProjectQualityPolicyV1, baseline_quality_policies

    skills = baseline_quality_policies(ProjectQualityPolicyV1(), "pyfly", "none")
    assert skills
    assert all(command["quality"]["tool"]["approved_version"] is None for s in skills for command in s["commands"])


def test_quality_prerequisites_are_counted_in_the_effective_command_list_from_inception():
    from firefly_forge.quality_policies import ProjectQualityPolicyV1, QualityCommand, baseline_quality_policies

    skills = baseline_quality_policies(
        ProjectQualityPolicyV1(tool_versions={"ruff": "1.2.3", "mypy": "1.2.3"}), "pyfly", "none"
    )
    commands = policy_commands({"skills": skills}, "pyfly")
    checks = [c for c in commands if isinstance(c, QualityCommand) and c.phase == "check"]
    probes = [c for c in commands if isinstance(c, QualityCommand) and c.phase == "prerequisite"]
    assert len(commands) == len(checks) + len(probes) == 2 * len(checks)
    assert all(a.gate_id == b.gate_id for a, b in zip(probes, checks, strict=True))
    assert all(set(c.model_dump()) == {"argv", "timeout"} for c in commands)
    assert all(probe.argv[:4] == ["python", "-B", "-I", "-m"] for probe in probes)


def test_rerendering_baseline_does_not_approve_agent_weakened_rules():
    from firefly_forge.quality_policies import ProjectQualityPolicyV1, baseline_quality_policies

    policy = ProjectQualityPolicyV1(tool_versions={"ruff": "1.2.3", "mypy": "1.2.3"})
    skills = baseline_quality_policies(policy, "pyfly", "none")
    first = copy.deepcopy(skills)
    lint = next(c for skill in skills for c in skill["commands"] if c["quality"]["category"] == "lint")
    assert "--isolated" in lint["argv"] and "--ignore-noqa" in lint["argv"]
    assert "--no-respect-gitignore" in lint["argv"]
    types = next(c for skill in skills for c in skill["commands"] if c["quality"]["category"] == "types")
    assert types["quality"]["approved_configuration"]
    lint["quality"]["approved_configuration"]["attacker.toml"] = "a" * 64
    assert baseline_quality_policies(policy, "pyfly", "none") == first


def test_run_skill_omission_cannot_remove_project_required_baselines(tmp_path):
    from firefly_forge.quality_policies import ProjectQualityPolicyV1, apply_project_quality

    catalog = PolicyCatalog(tmp_path)
    optional = catalog.selection([])
    result = apply_project_quality(optional, ProjectQualityPolicyV1(), "pyfly", "none", catalog)
    assert result["skills"] and all(s["kind"] == "quality_gate" for s in result["skills"])
    assert result["project_quality_policy"] == ProjectQualityPolicyV1().model_dump(mode="json")
    assert result["digest"] == digest({k: v for k, v in result.items() if k != "digest"})
    assert optional["skills"] == []


def test_explicit_project_baseline_opt_out_is_distinct_from_absent_legacy_policy(tmp_path):
    from firefly_forge.quality_policies import ProjectQualityPolicyV1, apply_project_quality

    catalog = PolicyCatalog(tmp_path)
    legacy = catalog.selection([])
    assert apply_project_quality(legacy, None, "pyfly", "none", catalog) == legacy
    explicit = apply_project_quality(legacy, ProjectQualityPolicyV1(baseline=None), "pyfly", "none", catalog)
    assert explicit["skills"] == [] and explicit["project_quality_policy"]["baseline"] is None


def test_baseline_data_is_validated_by_the_incumbent_catalog():
    from firefly_forge.quality_policies import ProjectQualityPolicyV1, baseline_quality_policies

    skills = baseline_quality_policies(ProjectQualityPolicyV1(), "larafly", "react")
    assert PolicyCatalog.validate({"version": "quality-baseline-v1", "skills": skills})["skills"]


@pytest.mark.parametrize("timeout", [None, 20000])
def test_new_quality_commands_inherit_project_runner_duration_without_a_new_ceiling(timeout):
    from firefly_forge.quality_policies import ProjectQualityPolicyV1, baseline_quality_policies

    skills = baseline_quality_policies(ProjectQualityPolicyV1(command_timeout_seconds=timeout), "pyfly", "none")
    commands = policy_commands({"skills": skills}, "pyfly")
    assert commands and all(command.timeout == timeout for command in commands)
    assert PolicyCatalog.validate({"version": "quality-baseline-v1", "skills": skills})


def test_legacy_policy_command_timeout_contract_is_preserved():
    from firefly_forge.policies import PolicyCommand

    assert PolicyCommand(argv=["legacy-check"], backends=["pyfly"]).timeout == 300
    for timeout in (None, 20000):
        with pytest.raises(ValueError):
            PolicyCommand(argv=["legacy-check"], timeout=timeout, backends=["pyfly"])
