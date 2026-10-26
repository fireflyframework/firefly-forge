# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Project cardinality must not become a hidden execution or recursion budget."""

import pytest

from firefly_forge.product_architecture import ProductArchitecture
from firefly_forge.project_models import ProjectDefinition


def project(count):
    return {
        "name": "Enterprise product",
        "description": "Services delivered across a large component graph.",
        "topology": "monorepo",
        "owners": ["Product team"],
        "components": [
            {
                "id": f"component-{i}",
                "name": f"Component {i}",
                "repository_id": "a" * 32,
                "path": f"services/component-{i}",
                "responsibility": "Own this service contract",
                "depends_on": [],
            }
            for i in range(count)
        ],
    }


def test_many_components_and_owners_round_trip_without_an_entity_count_ceiling():
    value = project(5000)
    value["owners"] = [f"Owner {i}" for i in range(80)]
    parsed = ProjectDefinition.model_validate(value)
    restored = ProjectDefinition.model_validate_json(parsed.model_dump_json())
    assert len(restored.components) == 5000 and len(restored.owners) == 80


def test_dependency_chain_is_not_limited_by_python_recursion_depth():
    value = project(1500)
    for i, component in enumerate(value["components"][:-1]):
        component["depends_on"] = [f"component-{i + 1}"]
    assert len(ProjectDefinition.model_validate(value).components) == 1500


def test_large_dependency_fanout_remains_validated():
    value = project(120)
    value["components"][0]["depends_on"] = [f"component-{i}" for i in range(1, 120)]
    assert len(ProjectDefinition.model_validate(value).components[0].depends_on) == 119
    value["components"][0]["depends_on"].append("missing-service")
    with pytest.raises(ValueError, match="reference this project's components"):
        ProjectDefinition.model_validate(value)


def test_cycle_at_the_end_of_a_deep_chain_remains_a_domain_error():
    value = project(1500)
    for i, component in enumerate(value["components"]):
        component["depends_on"] = [f"component-{(i + 1) % 1500}"]
    with pytest.raises(ValueError, match="cycles"):
        ProjectDefinition.model_validate(value)


@pytest.mark.parametrize("paths", [[".", "api"], ["api", "api/v1"], ["a", "a-else", "a/child"], ["api", "api"]])
def test_nonoverlap_checks_keep_root_and_nonadjacent_lexical_parent_protection(paths):
    value = project(len(paths))
    for component, path in zip(value["components"], paths, strict=True):
        component["path"] = path
    with pytest.raises(ValueError, match="nonoverlapping"):
        ProjectDefinition.model_validate(value)


def test_common_string_prefixes_are_distinct_component_roots():
    value = project(3)
    for component, path in zip(value["components"], ["a-else", "a/one", "a/two"], strict=True):
        component["path"] = path
    assert len(ProjectDefinition.model_validate(value).components) == 3


def test_architecture_can_describe_more_than_a_fixed_number_of_pieces():
    value = {
        "backend": "pyfly",
        "frontend": "none",
        "components": [
            {"name": f"Service {i}", "layer": "domain", "responsibility": "Own this service"} for i in range(100)
        ],
        "decisions": [f"Decision {i}" for i in range(80)],
        "nonfunctional_requirements": [f"Requirement {i}" for i in range(120)],
    }
    parsed = ProductArchitecture.model_validate(value)
    assert len(parsed.components) == 100 and len(parsed.decisions) == 80
    assert len(parsed.nonfunctional_requirements) == 120
