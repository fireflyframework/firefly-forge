# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Read approved tool/rule inputs through the existing confined workspace reader."""

from __future__ import annotations

import hashlib
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from firefly_forge.product_provenance import content_digest as digest
from firefly_forge.quality_gate_models import QualityGateContractV1
from firefly_forge.quality_gates import _json
from firefly_forge.workspace_tools import WorkspaceTools


class QualityRequirementsError(ValueError):
    pass


def locked_quality_version(ecosystem: str, package: str, content: str) -> str:
    try:
        if ecosystem == "python":

            def canonical(name: str) -> str:
                return re.sub(r"[-_.]+", "-", name).lower()

            entries = tomllib.loads(content).get("package", [])
            versions = [item["version"] for item in entries if canonical(item["name"]) == canonical(package)]
        elif ecosystem == "composer":
            document = _json(content)
            entries = document.get("packages", []) + document.get("packages-dev", [])
            versions = [item["version"] for item in entries if item["name"] == package]
        elif ecosystem == "node":
            document = _json(content)
            item = document["packages"]["node_modules/" + package]
            if item.get("link"):
                raise ValueError("Linked tool packages need an independently attested toolchain")
            versions = [item["version"]]
        else:
            raise ValueError("Unsupported quality-tool lock format")
        if len(versions) != 1 or not isinstance(versions[0], str) or not versions[0].strip():
            raise ValueError("One exact tool version is required")
        return versions[0]
    except (ValueError, TypeError, KeyError, AttributeError) as error:
        raise QualityRequirementsError("Configure one unambiguous approved tool entry in the component lock") from error


@dataclass(frozen=True)
class PreparedQualityProjectInputs:
    component: Path
    contract: QualityGateContractV1
    contract_digest: str
    version: str
    lock_digest: str
    configuration: dict[str, str]

    @property
    def configuration_digest(self) -> str:
        return digest(self.configuration)

    async def assert_current(self) -> None:
        if digest(self.contract.model_dump(mode="json")) != self.contract_digest:
            raise QualityRequirementsError("The approved quality contract changed")
        actual = await prepare_quality_project_inputs(self.component, self.contract)
        if (
            actual.lock_digest != self.lock_digest
            or actual.version != self.version
            or actual.configuration != self.configuration
        ):
            raise QualityRequirementsError("Approved quality inputs changed during verification")


async def prepare_quality_project_inputs(
    component: Path, contract: QualityGateContractV1
) -> PreparedQualityProjectInputs:
    contract = QualityGateContractV1.model_validate(contract.model_dump(mode="json"))
    if component.is_symlink() or not component.is_dir():
        raise QualityRequirementsError("Quality checks require their real selected component directory")
    if contract.tool.approved_version is None:
        raise QualityRequirementsError("Configure an approved quality-tool version before verification")
    tools = WorkspaceTools(component, repository_root=component)
    try:
        lock = await tools.read_file(contract.tool.lock_file)
        version = locked_quality_version(contract.tool.ecosystem, contract.tool.package, lock)
        if version != contract.tool.approved_version:
            raise QualityRequirementsError("The component lock differs from the approved quality-tool version")
        configuration = {}
        for path in contract.configuration_paths:
            content = await tools.read_file(path)
            configuration[path] = hashlib.sha256(content.encode()).hexdigest()
        if configuration != contract.approved_configuration:
            raise QualityRequirementsError("Restore the approved quality-rule configuration before verification")
    except (OSError, ValueError) as error:
        if isinstance(error, QualityRequirementsError):
            raise
        raise QualityRequirementsError("Prepare the approved tool lock and rule files in this component") from error
    return PreparedQualityProjectInputs(
        component,
        contract,
        digest(contract.model_dump(mode="json")),
        version,
        hashlib.sha256(lock.encode()).hexdigest(),
        configuration,
    )
